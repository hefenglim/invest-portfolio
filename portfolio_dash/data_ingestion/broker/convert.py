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
from dataclasses import dataclass
from datetime import timedelta
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
from portfolio_dash.data_ingestion.broker.ir import EventKind, RawEvent
from portfolio_dash.data_ingestion.broker.reconcile import ReconcileReport, reconcile
from portfolio_dash.data_ingestion.import_templates import template_columns

_ZERO = Decimal(0)

#: Share counts are written at 8 dp. A derived reinvest count is an exact quotient carrying
#: 28 significant digits, which is false precision, not accuracy — the broker states an amount
#: and a price, and eight places is past any fractional share either can express. The value
#: still differs from the printed quantity, which is the entire point of deriving it.
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


def _transaction_rows(grouped: GroupedImport, account: str, suspect: set[str]) -> list[
    list[str]
]:
    """Trades. Fees are the BROKER's own numbers, never recomputed.

    ``csv_import`` only auto-fills a fee or tax it was not given, so writing both columns keeps
    the ledger reconcilable against the statement to the cent. Recomputing them from the
    account's fee rules would produce a defensible number that does not match the money that
    actually left the account, which is the wrong kind of correct.
    """
    out: list[list[str]] = []
    for e in sorted(grouped.trades, key=lambda x: (x.trade_date, x.line_no)):
        side = "BUY" if e.kind in _BUY_SIDE else "SELL"
        out.append([
            account, e.symbol, side, e.trade_date.isoformat(),
            _shares(abs(e.quantity)), _money(e.price),
            _money(abs(e.fees)), "0",
            "0", "1" if e.kind is EventKind.SELL_SHORT else "0",
            f"{_DUPLICATE_MARK} {e.ref}" if e.ref in suspect else e.ref,
        ])
    return out


def _dividend_rows(dividends: list[DividendEvent], account: str) -> list[list[str]]:
    """One row per folded distribution. ``DRIP`` when it reinvested, ``CASH`` when it paid out.

    ``CASH`` on a ``drip_us`` account is ordinary, not exceptional (P1b): the same US position
    pays cash in some quarters and reinvests in others, and the model accepts both.
    """
    out: list[list[str]] = []
    for d in sorted(dividends, key=lambda x: (x.trade_date, x.symbol)):
        reinvested = d.reinvest_shares is not None and d.reinvest_price is not None
        out.append([
            account, d.symbol, d.trade_date.isoformat(),
            "DRIP" if reinvested else "CASH",
            _money(d.gross), _money(d.withholding), _money(d.net),
            _shares(d.reinvest_shares) if d.reinvest_shares is not None else "",
            _money(d.reinvest_price) if d.reinvest_price is not None else "",
        ])
    return out


def _cash_rows(
    grouped: GroupedImport, account: str, currency: str, suspect: set[str]
) -> tuple[list[list[str]], list[tuple[RawEvent, str]]]:
    """Cash movements. The ledger takes a MAGNITUDE plus a kind; the kind carries the sign.

    A row that appeared identically in two source files is written, and its note says so. It
    is not dropped — two deposits of the same amount on one day is a thing that happens, and
    the file cannot tell the two cases apart. It is not left silent either: the report is read
    once, whereas the ``note`` column is in front of whoever opens the CSV, which is where the
    decision actually gets made. (``import_batches`` catches a repeat of the whole FILE; it
    cannot see two copies of one row inside a single import.)
    """
    out: list[list[str]] = []
    unconvertible: list[tuple[RawEvent, str]] = []
    for e in sorted(grouped.cash, key=lambda x: (x.trade_date, x.line_no)):
        kind = CASH_KIND.get(e.kind)
        if kind is None:
            unconvertible.append((e, UNCONVERTIBLE.get(e.kind, "no mapping to a ledger row")))
            continue
        note = f"{_DUPLICATE_MARK} {e.ref}" if e.ref in suspect else e.ref
        out.append([
            account, e.trade_date.isoformat(), kind, currency,
            _money(abs(e.amount)), "", note,
        ])
    return out, unconvertible


def _action_rows(pairs: list[ActionPair], account: str) -> tuple[
    list[list[str]], list[list[str]], list[ActionPair]
]:
    """Split the paired actions into importable rows and a worksheet.

    A pair whose ratio the file does not determine goes to the worksheet with both ratio
    columns blank. They are REQUIRED columns, so the worksheet cannot be imported until the
    owner fills them — which is the point, not a snag: D14 refuses a decimal ratio because a
    rounded one replays into the 賣超 cascade this feature exists to prevent, and a ratio
    invented by the converter is the same defect with a friendlier face.
    """
    ready: list[list[str]] = []
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
            ready.append(row)
    return ready, worksheet, pending


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
        ("transactions", conv.rows["transactions"], len(grouped.trades)),
        ("dividends", conv.rows["dividends"], len(grouped.dividends)),
    ):
        if len(rows) != source:
            problems.append(
                f"{source} routed {label} produced {len(rows)} rows — the builder dropped some"
            )
    return problems


def convert(events: list[RawEvent], account: str, currency: str) -> tuple[
    Conversion, GroupedImport, ReconcileReport
]:
    """Parse-to-rows, with the reconciler run over the grouped events on the way through."""
    inferred, ambiguous = infer_cusip_aliases(events)
    events = apply_aliases(events, inferred)
    grouped = group_events(events)
    report = reconcile(events, grouped)

    pairs, unpaired = pair_actions(events, grouped.actions)
    ready, worksheet, pending = _action_rows(pairs, account)
    suspect = {ref for i in report.advisory if i.code == "overlap_duplicate" for ref in i.refs}
    cash, unconvertible = _cash_rows(grouped, account, currency, suspect)
    unconvertible += [(e, "corporate-action leg that could not be paired") for e in unpaired]

    openings = prehistory_shares(events)
    earliest = min((e.trade_date for e in events), default=None)
    # The DAY BEFORE the first event, never the same day: an opening position exists before
    # the ledger starts, and dating it onto a day that also carries transactions makes the
    # replay's same-date ordering decide whether the opening covers a sell or arrives after it.
    build = (earliest - timedelta(days=1)).isoformat() if earliest is not None else ""

    conv = Conversion(
        rows={
            "transactions": _transaction_rows(grouped, account, suspect),
            "dividends": _dividend_rows(grouped.dividends, account),
            "cash": cash,
            "fx": [],
            "corporate_actions": ready,
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
        },
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
