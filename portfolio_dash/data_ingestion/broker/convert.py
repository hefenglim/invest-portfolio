"""Broker events → this app's own import-template ROWS. The conversion itself.

Split out of ``scripts/schwab_convert.py`` on 2026-08-14, unchanged, because the offline CLI
is no longer the only caller: the web import door (``api/routers/broker_import.py``) runs the
same conversion so the owner does not have to open a terminal to load a statement. Two copies
of "how a broker event becomes a ledger row" is the drift §6.0's ONE OWNER rule exists to
prevent — and here the two copies would disagree about money.

What stayed in the script: reading files, writing files, printing, and the privacy masking
that only matters when there is a terminal. What moved here: everything that decides what a
row says. That line is also the ``mypy --strict`` line — the package is checked and covered,
a script is covered by whoever last ran it.

Nothing in this module touches a database, a connection, or a ledger. It maps parsed events
onto rows of text, and the rows go through the ORDINARY import preview/commit path like any
hand-made CSV — including every validation, the duplicate detection and the undo batch. A
converter with its own write path would be a second way into the ledger.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from portfolio_dash.data_ingestion.broker.grouping import (
    ActionPair,
    DividendEvent,
    GroupedImport,
    apply_aliases,
    group_events,
    infer_cusip_aliases,
    pair_actions,
    prehistory_shares,
)
from portfolio_dash.data_ingestion.broker.ir import (
    OPTION_EXPIRY_NO_OPENING,
    OPTION_KINDS,
    OPTION_NONSTANDARD_ADJUST,
    EventKind,
    RawEvent,
    UnmappedRow,
    chrono_key,
)
from portfolio_dash.data_ingestion.broker.reconcile import ReconcileReport, reconcile
from portfolio_dash.data_ingestion.import_templates import template_columns
from portfolio_dash.shared.options import (
    OptionTerms,
    contracts_to_shares,
    from_broker_option,
    nonstandard_adjustment_reason,
    option_display_name,
    option_symbol,
    shares_to_contracts,
)

_ZERO = Decimal(0)

#: Share counts are written at 8 dp, trailing zeros dropped. Every count the converter writes
#: is built from printed quantities — a reinvest too since 2026-10-11 (``grouping.fold_dividends``),
#: whose figure has at most 4 dp — so the quantize changes nothing a statement prints; it is a
#: ceiling on false precision, not a rounding step.
_SHARE_PLACES = Decimal("0.00000001")

#: Broker event kinds that become a cash movement, and the ledger kind each becomes.
CASH_KIND: dict[EventKind, str] = {
    EventKind.DEPOSIT: "DEPOSIT",
    EventKind.WITHDRAWAL: "WITHDRAW",
    EventKind.INTEREST_INCOME: "INTEREST",
    EventKind.INTEREST_EXPENSE: "INTEREST_EXPENSE",
    EventKind.FEE: "BROKER_FEE",
}

#: Ledger-bound kinds this converter cannot express, and why. Enumerated rather than defaulted:
#: a kind that falls off the end of a mapping is a row that leaves the ledger silently, which
#: is the failure this whole package exists to prevent. :func:`account_for_output` asserts
#: that every routed event is either written or named here.
UNCONVERTIBLE: dict[EventKind, str] = {
    EventKind.CASH_IN_LIEU: (
        "cash for a fractional share has no ledger row of its own — it is neither a trade nor "
        "a cash movement. Enter it manually as a small sale, or as a cash movement if the "
        "position it came from is already closed"
    ),
}

_BUY_SIDE = {EventKind.BUY, EventKind.BUY_COVER}


@dataclass(frozen=True)
class RowDetail:
    """One converted row as the page shows it beside its checkbox (DEF-028).

    The figures are the ones the owner checks against the statement — source rows, date,
    type, symbol, shares, price, amount — as STRINGS, exactly as they are written into the
    CSV. ``cells`` is the very list :func:`render_kind` writes, so a page that re-renders a
    ticked subset through the template header produces byte-identical rows, and never has
    to parse CSV text back into fields (the F-03 lesson: the frontend must not re-parse
    what the server already parsed).
    """

    refs: tuple[str, ...]
    date: str
    type: str
    symbol: str
    shares: str
    price: str
    amount: str
    currency: str
    note: str
    cells: list[str]


@dataclass(frozen=True)
class DroppedRow:
    """A source row that becomes NO row of its own, and where it went (DEF-028).

    Fifteen rows in, nine rows out, and the six that went elsewhere used to be a number the
    owner had to reconcile by hand: a journal pair dropped, a cancel taking its buy with it,
    three dividend legs folded into one — each is named here with its destination. ``into``
    is ``"<kind>:<row index>"`` when the row's money lives on inside another row, and empty
    when it left the conversion for good.
    """

    refs: tuple[str, ...]
    why: str
    date: str
    symbol: str
    kinds: tuple[str, ...]
    into: str
    detail: str


@dataclass
class Conversion:
    """The converted rows, keyed by import kind, plus what could not be converted."""

    rows: dict[str, list[list[str]]]
    unconvertible: list[tuple[RawEvent, str]]
    aliases_inferred: dict[str, str]
    aliases_ambiguous: dict[str, set[str]]
    actions_needing_input: list[ActionPair]
    openings: dict[str, Decimal]
    #: Every source row this conversion accounted for — written into a CSV or named as
    #: unconvertible. Checked against the routed events by :func:`account_for_output`.
    written_refs: set[str]
    #: The four importable kinds, row by row, with the figures a page shows. ``rows[k]``
    #: is exactly ``[d.cells for d in details[k]]`` — one list, two views.
    details: dict[str, list[RowDetail]]
    #: Every source row that produced no row of its own, with its destination.
    dropped: list[DroppedRow]
    #: How many transactions rows came from option legs (options spec §8) — the count half of
    #: :func:`account_for_output` adds them to the equity trades.
    option_trade_rows: int = 0


# --------------------------------------------------------------------------- formatting


def _money(value: Decimal) -> str:
    """A money string with no exponent and no trailing-zero drift."""
    return f"{value:f}"


def _shares(value: Decimal) -> str:
    quantized = value.quantize(_SHARE_PLACES, rounding=ROUND_HALF_UP).normalize()
    # ``normalize`` renders 100 as 1E+2; the ledger stores TEXT and reads it back as a number,
    # but a human opening the CSV should not have to.
    return f"{quantized:f}"


# --------------------------------------------------------------------------- conversion


#: Prefix stamped on the ``note`` of a row the reconciler saw twice across source files.
_DUPLICATE_MARK = "可能重複（兩份匯出的日期區間重疊）"


#: The sort key of one transactions-CSV row: the date the ROW carries, then 0 for a trade /
#: 1 for an expiry (spec §8: an expiry is written after that day's trades), then the source
#: chronology. Equity and option trades interleave by :func:`chrono_key` exactly as before.
_RowKey = tuple[date, int, tuple[date, str, int, int]]


@dataclass
class _OptionConversion:
    """What the option legs of one request become (options spec §8)."""

    #: ``(sort key, row)`` — merged into the transactions CSV in date / chronology order.
    trades: list[tuple[_RowKey, RowDetail]]
    actions: list[RowDetail]
    dropped: list[DroppedRow]
    #: Every option source row that reached a row (trades, expiries, adjustment legs).
    written: set[str]


_OPTION_SIDE: dict[EventKind, tuple[str, bool]] = {
    # (ledger side, declared short) — selling to OPEN is the declared short sale (R1).
    EventKind.OPT_SELL_OPEN: ("SELL", True),
    EventKind.OPT_SELL_CLOSE: ("SELL", False),
    EventKind.OPT_BUY_OPEN: ("BUY", False),
    EventKind.OPT_BUY_CLOSE: ("BUY", False),
}


def _unmapped(e: RawEvent, reason: str, detail: str) -> UnmappedRow:
    return UnmappedRow(source_file=e.source_file, line_no=e.line_no, action=e.kind.value,
                       description=e.description, reason=reason, detail=detail)


def _contract_of(e: RawEvent) -> tuple[str, OptionTerms]:
    """The OCC symbol and terms of an option leg — or the refusal: a contract string that is
    not a standard one (a digit root after an adjustment, a malformed strike) is a
    non-standard deliverable v1 does not model (OPT-D11)."""
    terms = from_broker_option(e.option_symbol)
    if terms is None:
        raise _unmapped(e, OPTION_NONSTANDARD_ADJUST,
                        f"not a standard contract: {e.option_symbol!r}")
    return option_symbol(terms), terms


def _option_units(shares: Decimal) -> str:
    """「2 口（200 股當量）」 — the report fields only; the CSV cell holds the share-equivalents."""
    return (f"{_shares(shares_to_contracts(shares))} 口"
            f"（{_shares(shares)} 股當量）")


def _convert_options(  # noqa: C901, PLR0912 - one branch per option row shape (spec §8)
    options: list[RawEvent], account: str, currency: str, suspect: set[str],
    known_positions: Mapping[str, Decimal],
) -> _OptionConversion:
    """The option legs → transactions rows, ``OPTION_ADJUST`` rows and named drops.

    * a trade leg → a trade row on the OCC contract: share-equivalents
      (``contracts_to_shares(Quantity)``) and the per-share premium in ``cells`` (so
      quantity × price is the statement's own Amount before fees), the display name and
      「2 口（200 股當量）」 in the report fields; fees verbatim, tax 0;
    * ``Expired`` / TD's removal journal → a price-0 ``expired`` row dated the contract's
      EXPIRY, its side from the running signed position (``known_positions`` + every earlier
      leg of the request); same-day expiry rows of one contract merged into one row, written
      after that day's trades; no position → ``UnmappedRow(option_expiry_no_opening)``;
    * an adjustment journal pair → one ``OPTION_ADJUST`` with ``ratio_to = |to| ÷ |from|``
      when that is a whole number and OPT-D11 holds, else
      ``UnmappedRow(option_nonstandard_adjust)``;
    * any other option row (a platform transfer that did not cancel) → dropped, named.

    The running position walks the legs in the order they happened, an adjustment first on
    its day, an expiry last — the replay's own order.
    """
    out = _OptionConversion(trades=[], actions=[], dropped=[], written=set())
    position: dict[str, Decimal] = {k: v for k, v in known_positions.items()}
    trades = [e for e in options if e.kind in _OPTION_SIDE]
    expiries = [e for e in options if e.kind is EventKind.OPT_EXPIRED]
    adjusts = [e for e in options if e.kind is EventKind.OPT_ADJUST]
    for e in options:
        if e.kind not in _OPTION_SIDE and e.kind not in (
                EventKind.OPT_EXPIRED, EventKind.OPT_ADJUST):
            out.dropped.append(DroppedRow(
                refs=(e.ref,), why="option_transfer", date=e.trade_date.isoformat(),
                symbol=e.option_symbol, kinds=(e.kind.value,), into="",
                detail="選擇權在券商平台之間移轉（未互相抵銷），不入帳；移轉不改變部位",
            ))
    # --- adjustment pairs, per day: (date, expiry, right) groups of exactly two legs. Two
    # legs pair whatever their underlyings, because a merger RENAMES the underlying (the
    # owner's real VACQ → RKLB put: one pair, two roots). More than two legs split by
    # underlying (R2 m4): same-day adjustments of two underlyings sharing an expiry and a
    # right are two pairs, not one group of four legs. ---
    by_day: dict[tuple[date, date, str], list[tuple[RawEvent, str, OptionTerms]]] = {}
    for e in adjusts:
        symbol, terms = _contract_of(e)
        by_day.setdefault((e.trade_date, terms.expiry, terms.right), []).append(
            (e, symbol, terms))
    groups: dict[tuple[date, str, date, str], list[tuple[RawEvent, str, OptionTerms]]] = {}
    for (day, expiry, right), legs in by_day.items():
        if len(legs) == 2:
            groups[(day, "", expiry, right)] = legs
            continue
        for leg in legs:
            groups.setdefault((day, leg[2].underlying, expiry, right), []).append(leg)
    # --- expiries, merged per (contract, expiry) ---
    merged: dict[tuple[str, date], list[tuple[RawEvent, OptionTerms]]] = {}
    for e in expiries:
        symbol, terms = _contract_of(e)
        merged.setdefault((symbol, terms.expiry), []).append((e, terms))
    # --- one walk in the replay's order ---
    walk: list[tuple[tuple[date, int, tuple[date, str, int, int]], str, object]] = []
    for key, legs in groups.items():
        first = min((leg[0] for leg in legs), key=chrono_key)
        walk.append(((key[0], 0, chrono_key(first)), "adjust", legs))
    for e in trades:
        walk.append(((e.trade_date, 1, chrono_key(e)), "trade", e))
    for (symbol, expiry), rows in merged.items():
        first = min((r[0] for r in rows), key=chrono_key)
        walk.append(((expiry, 2, chrono_key(first)), "expiry", (symbol, rows)))
    walk.sort(key=lambda w: w[0])
    for (day, _rank, chrono), what, payload in walk:
        if what == "adjust":
            assert isinstance(payload, list)
            row = _adjust_row(payload, account, position)
            out.actions.append(row)
            out.written.update(row.refs)
        elif what == "trade":
            assert isinstance(payload, RawEvent)
            e = payload
            symbol, terms = _contract_of(e)
            side, short = _OPTION_SIDE[e.kind]
            shares = contracts_to_shares(abs(e.quantity))
            position[symbol] = position.get(symbol, _ZERO) + (
                shares if side == "BUY" else -shares)
            duplicate = e.ref in suspect
            out.trades.append(((day, 0, chrono), RowDetail(
                refs=(e.ref,), date=e.trade_date.isoformat(), type=side,
                symbol=option_display_name(terms), shares=_option_units(shares),
                price=_money(e.price), amount=_money(abs(e.amount)), currency=currency,
                note=_DUPLICATE_MARK if duplicate else "",
                cells=[
                    account, symbol, side, e.trade_date.isoformat(), _shares(shares),
                    _money(e.price), _money(abs(e.fees)), "0", "0", "1" if short else "0",
                    f"{_DUPLICATE_MARK} {e.ref}" if duplicate else e.ref, "",
                ],
            )))
            out.written.add(e.ref)
        else:
            assert isinstance(payload, tuple)
            symbol, rows = payload
            held = position.get(symbol, _ZERO)
            first_event = min((r[0] for r in rows), key=chrono_key)
            if held == _ZERO:
                raise _unmapped(first_event, OPTION_EXPIRY_NO_OPENING,
                                f"no open position in {symbol} to expire")
            shares = sum((contracts_to_shares(abs(r[0].quantity)) for r in rows), _ZERO)
            side = "BUY" if held < _ZERO else "SELL"
            position[symbol] = held + (shares if side == "BUY" else -shares)
            refs = tuple(sorted((r[0].ref for r in rows), key=lambda ref: ref))
            terms = rows[0][1]
            out.trades.append(((day, 1, chrono), RowDetail(
                refs=refs, date=day.isoformat(), type=side,
                symbol=option_display_name(terms), shares=_option_units(shares),
                price="0", amount="0", currency=currency, note="到期作廢",
                cells=[
                    account, symbol, side, day.isoformat(), _shares(shares), "0", "0", "0",
                    "0", "0", " · ".join(refs), "expired",
                ],
            )))
            out.written.update(refs)
    return out


def _adjust_row(
    legs: list[tuple[RawEvent, str, OptionTerms]], account: str,
    position: dict[str, Decimal],
) -> RowDetail:
    """One adjustment journal pair → one ``OPTION_ADJUST`` row (options spec §8 / OPT-D11).

    The FROM leg is the one whose contract the request (with ``known_positions``) holds; the
    statement prints a leg per contract, so a pair is exactly two legs. ``ratio_to`` is the
    contract ratio ``|to| ÷ |from|`` — 2 → 6 is 1 → 3 — and must be a whole number with the
    strike products equal; anything else is a non-standard deliverable and stops the run."""
    first = min((leg[0] for leg in legs), key=chrono_key)
    if len(legs) != 2:
        raise _unmapped(first, OPTION_NONSTANDARD_ADJUST,
                        f"{len(legs)} adjustment legs on one day — not a 1-to-1 pair")
    held = [leg for leg in legs if position.get(leg[1], _ZERO) != _ZERO]
    if len(held) != 1:
        raise _unmapped(first, OPTION_NONSTANDARD_ADJUST,
                        "cannot tell the adjusted contract from its successor")
    src = held[0]
    dst = legs[1] if legs[0] is src else legs[0]
    out_qty, in_qty = abs(src[0].quantity), abs(dst[0].quantity)
    if out_qty == _ZERO or in_qty == _ZERO or in_qty % out_qty != _ZERO:
        raise _unmapped(first, OPTION_NONSTANDARD_ADJUST,
                        f"contract ratio {in_qty}/{out_qty} is not a whole number")
    ratio = in_qty / out_qty
    why = nonstandard_adjustment_reason(src[2], dst[2], ratio, Decimal(1))
    if why is not None:
        raise _unmapped(first, OPTION_NONSTANDARD_ADJUST, why)
    moved = position.pop(src[1]) * ratio
    position[dst[1]] = position.get(dst[1], _ZERO) + moved
    refs = tuple(leg[0].ref for leg in sorted(legs, key=lambda leg: chrono_key(leg[0])))
    ratio_text = _shares(ratio)
    return RowDetail(
        refs=refs, date=first.trade_date.isoformat(), type="OPTION_ADJUST",
        symbol=option_display_name(dst[2]), shares="", price="", amount="", currency="",
        note=(f"{option_display_name(src[2])} → {option_display_name(dst[2])}，"
              f"每 1 口 → {ratio_text} 口"),
        cells=[account, first.trade_date.isoformat(), "OPTION_ADJUST", src[1], dst[1],
               ratio_text, "1", "", " · ".join(refs)],
    )


def _transaction_rows(
    grouped: GroupedImport, account: str, currency: str, suspect: set[str]
) -> list[RowDetail]:
    """Trades. Fees are the BROKER's own numbers, never recomputed.

    ``csv_import`` only auto-fills a fee or tax it was not given, so writing both columns keeps
    the ledger reconcilable against the statement to the cent. Recomputing them from the
    account's fee rules would produce a defensible number that does not match the money that
    actually left the account, which is the wrong kind of correct.

    ⚠ In the order the trades HAPPENED (:func:`chrono_key`), which is also the order the
    page commits them and the order the replay books them (write id ascending since
    2026-09-23). A Schwab export prints newest first, so the old ``(trade_date, line_no)``
    sort wrote a same-day buy-then-sell as sell-then-buy — an oversell on a position the
    same file had just opened.
    """
    return [row for _key, row in _keyed_trade_rows(grouped, account, currency, suspect)]


def _keyed_trade_rows(
    grouped: GroupedImport, account: str, currency: str, suspect: set[str]
) -> list[tuple[_RowKey, RowDetail]]:
    """:func:`_transaction_rows` with each row's sort key, so the option rows can be merged in
    (:func:`convert`)."""
    out: list[tuple[_RowKey, RowDetail]] = []
    for e in sorted(grouped.trades, key=chrono_key):
        side = "BUY" if e.kind in _BUY_SIDE else "SELL"
        duplicate = e.ref in suspect
        out.append(((e.trade_date, 0, chrono_key(e)), RowDetail(
            refs=(e.ref,), date=e.trade_date.isoformat(), type=side, symbol=e.symbol,
            shares=_shares(abs(e.quantity)), price=_money(e.price),
            amount=_money(abs(e.amount)), currency=currency,
            note=_DUPLICATE_MARK if duplicate else "",
            cells=[
                account, e.symbol, side, e.trade_date.isoformat(),
                _shares(abs(e.quantity)), _money(e.price),
                _money(abs(e.fees)), "0",
                "0", "1" if e.kind is EventKind.SELL_SHORT else "0",
                f"{_DUPLICATE_MARK} {e.ref}" if duplicate else e.ref,
                "",   # close_reason (options spec §8): blank on every equity trade
            ],
        )))
    return out


def _dividend_rows(
    dividends: list[DividendEvent], account: str, currency: str
) -> list[RowDetail]:
    """One row per folded distribution. ``DRIP`` when it reinvested, ``CASH`` when it paid out.

    ``CASH`` on a ``drip_us`` account is ordinary, not exceptional (P1b): the same US position
    pays cash in some quarters and reinvests in others, and the model accepts both.
    """
    out: list[RowDetail] = []
    for d in sorted(dividends, key=lambda x: (x.trade_date, x.symbol)):
        reinvested = d.reinvest_shares is not None and d.reinvest_price is not None
        shares = _shares(d.reinvest_shares) if d.reinvest_shares is not None else ""
        price = _money(d.reinvest_price) if d.reinvest_price is not None else ""
        out.append(RowDetail(
            refs=d.refs, date=d.trade_date.isoformat(),
            type="DRIP" if reinvested else "CASH", symbol=d.symbol,
            shares=shares, price=price, amount=_money(d.net), currency=currency,
            note=f"總額 {_money(d.gross)}，預扣 {_money(d.withholding)}",
            cells=[
                account, d.symbol, d.trade_date.isoformat(),
                "DRIP" if reinvested else "CASH",
                _money(d.gross), _money(d.withholding), _money(d.net),
                shares, price,
                # R6 ``ex_date``: a broker statement's distribution line carries the payment
                # date, not the ex-date, so this stays blank — the same 「supplied verbatim,
                # never recomputed」 discipline this converter is built on. Blank replays
                # exactly as it did before the column existed.
                "",
            ],
        ))
    # The header comes from ``template_columns``, so a row SHORTER than the column list
    # misaligns every field after the gap instead of failing. Adding ex_date to
    # DIVIDEND_COLUMNS without the blank above would have done exactly that, silently.
    width = len(template_columns("dividends"))
    assert all(len(r.cells) == width for r in out), (
        f"dividend row width must match the {width} template columns"
    )
    return out


def _cash_rows(
    grouped: GroupedImport, account: str, currency: str, suspect: set[str]
) -> tuple[list[RowDetail], list[tuple[RawEvent, str]]]:
    """Cash movements. The ledger takes a MAGNITUDE plus a kind; the kind carries the sign.

    A row that appeared identically in two source files is written, and its note says so. It
    is not dropped — two deposits of the same amount on one day is a thing that happens, and
    the file cannot tell the two cases apart. It is not left silent either: the report is read
    once, whereas the ``note`` column is in front of whoever opens the CSV, which is where the
    decision actually gets made. (``import_batches`` catches a repeat of the whole FILE; it
    cannot see two copies of one row inside a single import.)

    Chronological (:func:`chrono_key`) for the same reason as the trades: the cash ledger's
    withdraw guard walks the day's movements in write order.
    """
    out: list[RowDetail] = []
    unconvertible: list[tuple[RawEvent, str]] = []
    for e in sorted(grouped.cash, key=chrono_key):
        kind = CASH_KIND.get(e.kind)
        if kind is None:
            unconvertible.append((e, UNCONVERTIBLE.get(e.kind, "no mapping to a ledger row")))
            continue
        duplicate = e.ref in suspect
        out.append(RowDetail(
            refs=(e.ref,), date=e.trade_date.isoformat(), type=kind, symbol="",
            shares="", price="", amount=_money(abs(e.amount)), currency=currency,
            note=_DUPLICATE_MARK if duplicate else "",
            cells=[
                account, e.trade_date.isoformat(), kind, currency,
                _money(abs(e.amount)), "",
                f"{_DUPLICATE_MARK} {e.ref}" if duplicate else e.ref,
            ],
        ))
    return out, unconvertible


def _action_rows(pairs: list[ActionPair], account: str) -> tuple[
    list[RowDetail], list[list[str]], list[ActionPair]
]:
    """Split the paired actions into importable rows and a worksheet.

    A pair whose ratio the file does not determine goes to the worksheet with both ratio
    columns blank. They are REQUIRED columns, so the worksheet cannot be imported until the
    owner fills them — which is the point, not a snag: D14 refuses a decimal ratio because a
    rounded one replays into the 賣超 cascade this feature exists to prevent, and a ratio
    invented by the converter is the same defect with a friendlier face.
    """
    ready: list[RowDetail] = []
    worksheet: list[list[str]] = []
    pending: list[ActionPair] = []
    for p in sorted(pairs, key=lambda x: (x.trade_date, x.to_symbol)):
        row = [
            account, p.trade_date.isoformat(), p.kind.value,
            p.from_symbol, p.to_symbol,
            "" if p.ratio_to is None else str(p.ratio_to),
            "" if p.ratio_from is None else str(p.ratio_from),
            "", " · ".join(p.refs),
        ]
        if p.ratio_to is None or p.ratio_from is None or not p.from_symbol:
            row[-1] = f"{p.needs} [{' · '.join(p.refs)}]"
            worksheet.append(row)
            pending.append(p)
        else:
            rename = f"{p.from_symbol} → {p.to_symbol}，" if p.from_symbol != p.to_symbol else ""
            ready.append(RowDetail(
                refs=tuple(p.refs), date=p.trade_date.isoformat(), type=p.kind.value,
                symbol=p.to_symbol, shares="", price="", amount="", currency="",
                note=f"{rename}每 {p.ratio_from} 股 → {p.ratio_to} 股",
                cells=row,
            ))
    return ready, worksheet, pending


def _dropped_rows(
    grouped: GroupedImport,
    details: dict[str, list[RowDetail]],
    unconvertible: list[tuple[RawEvent, str]],
    pending: list[ActionPair],
) -> list[DroppedRow]:
    """Every source row that reaches no row of its own, with where it went (DEF-028).

    Built from the SOURCE objects — the suppressed groups, the folded legs, the dividend
    refs — never by subtracting the written refs from the input, because a set difference
    can only say a row is missing, not what happened to it. Every row of the input must
    appear either in ``details`` or here; :func:`account_for_output`'s ref check and the
    endpoint's conservation test hold the two together.
    """
    out: list[DroppedRow] = []
    for g in grouped.suppressed:
        out.append(DroppedRow(
            refs=g.refs, why="suppressed", date=g.key[0].isoformat(), symbol=g.key[1],
            kinds=tuple(k.value for k in g.kinds), into="",
            detail=f"互相抵銷的群組（金額合計 {g.amount_sum}，股數合計 {g.quantity_sum}）",
        ))
    for i, d in enumerate(details["dividends"]):
        if len(d.refs) > 1:
            out.append(DroppedRow(
                refs=d.refs, why="merged_dividend", date=d.date, symbol=d.symbol,
                kinds=(), into=f"dividends:{i}",
                detail=f"{len(d.refs)} 列配息分錄合併為一筆股利",
            ))
    # An absorbed interest-withholding row lives on inside the day's one interest credit.
    credit_rows = {
        (c.date, c.type): i for i, c in enumerate(details["cash"]) if c.type == "INTEREST"
    }
    for e in grouped.folded:
        target = credit_rows.get((e.trade_date.isoformat(), "INTEREST"))
        out.append(DroppedRow(
            refs=(e.ref,), why="folded_interest_tax", date=e.trade_date.isoformat(),
            symbol="", kinds=(e.kind.value,),
            into=f"cash:{target}" if target is not None else "",
            detail="利息預扣稅已併入同日的利息收入（記淨額）",
        ))
    # Option legs are not dropped here any more (options spec §8): each one becomes a row or
    # is named by ``_convert_options`` (a platform transfer that did not cancel).
    for e in grouped.unrouted:
        out.append(DroppedRow(
            refs=(e.ref,), why="unrouted_row", date=e.trade_date.isoformat(),
            symbol=e.symbol or e.option_symbol, kinds=(e.kind.value,), into="",
            detail="已分類但不屬於任何帳本，不匯入",
        ))
    for e, why in unconvertible:
        out.append(DroppedRow(
            refs=(e.ref,), why="unconvertible", date=e.trade_date.isoformat(),
            symbol=e.symbol, kinds=(e.kind.value,), into="", detail=why,
        ))
    for idx, p in enumerate(pending):
        out.append(DroppedRow(
            refs=tuple(p.refs), why="action_needs_input", date=p.trade_date.isoformat(),
            symbol=p.to_symbol, kinds=(p.kind.value,), into=f"actions_needing_input:{idx}",
            detail="公司行動的比例檔案未載明，列在「需要你補的資料」",
        ))
    return out


def _opening_rows(
    openings: dict[str, Decimal], account: str, build_date: str
) -> list[list[str]]:
    """The pre-history worksheet: shares where they are known, cost always blank.

    ``original_cost_total`` is left empty rather than zeroed. D37 rejects a cost of 0 outright,
    so a zero would look like data and import as an error; a blank looks like a question.
    """
    return [
        [account, symbol, _shares(shares) if shares > _ZERO else "", "", build_date, ""]
        for symbol, shares in sorted(openings.items())
    ]


def account_for_output(grouped: GroupedImport, conv: Conversion) -> list[str]:
    """Every routed event is written or named. Returns the complaints, empty when sound.

    ``reconcile`` proves nothing was lost between the FILE and the grouped events. This proves
    nothing was lost between the grouped events and the CSVs — a second seam with its own way
    of dropping a row: a kind missing from ``CASH_KIND`` is simply not written, and the counts
    at the bottom of the report still look plausible. (It found three real ones on the first
    run — a broker withholds tax on credit INTEREST too, and those rows matched no cash kind.)

    By ``ref``, never by count, for the same reason :func:`grouping.account_for` is: counts
    balance while rows swap places. A count-based version of this function was written first
    and was quietly wrong — an unpaired corporate-action leg lands in ``unconvertible`` AND in
    ``grouped.actions``, so it was counted on both sides and cancelled its own complaint.
    """
    expected = {
        *(e.ref for e in grouped.trades),
        *(r for d in grouped.dividends for r in d.refs),
        *(e.ref for e in grouped.cash),
        *(e.ref for e in grouped.actions),
        # Options spec §8: every option LEG (trade, expiry, adjustment) is written; only a
        # platform transfer is dropped, and it is named in ``dropped``.
        *(e.ref for e in grouped.options if e.kind in OPTION_KINDS),
    }
    problems: list[str] = []
    missing = sorted(expected - conv.written_refs)
    if missing:
        problems.append(
            f"{len(missing)} routed rows reached no output and were not named as "
            f"unconvertible — first: {missing[:5]}"
        )
    invented = sorted(conv.written_refs - expected)
    if invented:
        problems.append(f"output cites rows that were not routed: {invented[:5]}")

    # The ref check proves ROUTING is complete; it is built from the source objects, so it
    # would still pass if a row builder filtered something out on the way to the CSV. These
    # two are the other half: both builders are straight 1:1 comprehensions, so a count that
    # disagrees means one of them grew a condition.
    for label, rows, source in (
        ("transactions", conv.rows["transactions"],
         len(grouped.trades) + conv.option_trade_rows),
        ("dividends", conv.rows["dividends"], len(grouped.dividends)),
    ):
        if len(rows) != source:
            problems.append(
                f"{source} routed {label} produced {len(rows)} rows — the builder dropped some"
            )
    return problems


def convert(
    events: list[RawEvent], account: str, currency: str, *,
    known_positions: Mapping[str, Decimal],
) -> tuple[Conversion, GroupedImport, ReconcileReport]:
    """Parse-to-rows, with the reconciler run over the grouped events on the way through.

    *known_positions* (options spec §8, OPT-D19 — REQUIRED): the ledger's signed position in
    each option contract of *account*, in share-equivalents, keyed by OCC symbol, through the
    day before the request's earliest row. The converter stays pure: the caller reads the
    ledger (``api/routers/broker_import.py``), and an expiry's side then comes from that
    position plus every earlier leg of the request — statements may be imported separately.
    The offline CLI has no ledger and passes ``{}``. Raises :class:`UnmappedRow` with a
    ``reason`` for an option row it cannot convert (spec §8)."""
    inferred, ambiguous = infer_cusip_aliases(events)
    events = apply_aliases(events, inferred)
    grouped = group_events(events)
    report = reconcile(events, grouped)

    pairs, unpaired = pair_actions(events, grouped.actions)
    ready, worksheet, pending = _action_rows(pairs, account)
    suspect = {ref for i in report.advisory if i.code == "overlap_duplicate" for ref in i.refs}
    options = _convert_options(grouped.options, account, currency, suspect, known_positions)
    cash, unconvertible = _cash_rows(grouped, account, currency, suspect)
    unconvertible += [(e, "corporate-action leg that could not be paired") for e in unpaired]

    openings = prehistory_shares(events)
    earliest = min((e.trade_date for e in events), default=None)
    # The DAY BEFORE the first event, never the same day: an opening position exists before
    # the ledger starts, and dating it onto a day that also carries transactions makes the
    # replay's same-date ordering decide whether the opening covers a sell or arrives after it.
    build = (earliest - timedelta(days=1)).isoformat() if earliest is not None else ""

    keyed = [*_keyed_trade_rows(grouped, account, currency, suspect), *options.trades]
    keyed.sort(key=lambda kr: kr[0])
    details: dict[str, list[RowDetail]] = {
        "transactions": [row for _key, row in keyed],
        "dividends": _dividend_rows(grouped.dividends, account, currency),
        "cash": cash,
        "fx": [],
        "corporate_actions": [*ready, *options.actions],
    }
    conv = Conversion(
        rows={
            **{kind: [d.cells for d in rows] for kind, rows in details.items()},
            "_actions_worksheet": worksheet,
            "_openings_worksheet": _opening_rows(openings, account, build),
        },
        unconvertible=unconvertible,
        aliases_inferred=inferred,
        aliases_ambiguous=ambiguous,
        actions_needing_input=pending,
        openings=openings,
        # Taken from the SOURCE objects, not parsed back out of the note column: a check that
        # reads the artifact it is verifying agrees with itself (the same mistake the
        # reconciler's share check made and had to have removed).
        written_refs={
            *(e.ref for e in grouped.trades),
            *(r for d in grouped.dividends for r in d.refs),
            *(e.ref for e in grouped.cash if e.kind in CASH_KIND),
            *(r for p in pairs for r in p.refs),
            *(e.ref for e, _ in unconvertible),
            *options.written,
        },
        details=details,
        dropped=[*_dropped_rows(grouped, details, unconvertible, pending), *options.dropped],
        option_trade_rows=len(options.trades),
    )
    return conv, grouped, report


# --------------------------------------------------------------- output shapes


FILENAMES: dict[str, str] = {
    "transactions": "import_transactions.csv",
    "dividends": "import_dividends.csv",
    "cash": "import_cash.csv",
    "fx": "import_fx.csv",
    "corporate_actions": "import_corporate_actions.csv",
    "_actions_worksheet": "corporate_actions_TO_COMPLETE.csv",
    "_openings_worksheet": "openings_TO_COMPLETE.csv",
}

TEMPLATE_KIND: dict[str, str] = {
    "_actions_worksheet": "corporate_actions",
    "_openings_worksheet": "openings",
}


def render_kind(kind: str, rows: list[list[str]]) -> str:
    """One import CSV: the template's own plain header, then the rows. CRLF, no BOM.

    The header is the PLAIN column list, not the annotated one the download endpoint renders —
    the import seam canonicalizes annotations away, but a file a human may hand-edit should not
    carry ``（選填）`` in its header.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(template_columns(TEMPLATE_KIND.get(kind, kind)))
    writer.writerows(rows)
    return buf.getvalue()
