"""Returns: per-currency total return + blended reporting total, and reporting XIRR."""

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from functools import cached_property
from typing import Protocol

from pyxirr import InvalidPaymentsError
from pyxirr import xirr as _xirr

from portfolio_dash.portfolio.cash import UNBOOKED_DIVIDEND_REASON
from portfolio_dash.portfolio.cost_basis import build_book, orphan_dividend_message
from portfolio_dash.portfolio.results import (
    Book,
    CurrencyReturn,
    Holding,
    ReturnSummary,
)
from portfolio_dash.shared.cash_kinds import CashKind, movement_sign
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.fx import convert
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import CASH_DIVIDEND_TYPES, Side
from portfolio_dash.shared.models.ledger import (
    Dividend,
    LedgerBundle,
    OpeningInventory,
    Transaction,
)
from portfolio_dash.shared.money import settled_notional

_ZERO = Decimal("0")
FxRate = Callable[[Currency, Currency], Decimal]


def total_return(
    book: Book,
    valued_holdings: list[Holding],
    current_fx: FxRate,
    reporting: Currency,
) -> ReturnSummary:
    """Per-currency realized+unrealized and rate (vs gross invested); blended at spot.

    Expects ``valued_holdings`` already passed through ``value_holdings`` (a holding with
    ``unrealized_pnl is None`` — stale or never valued — is skipped). Note: a stale
    holding's unrealized is excluded from the numerator while its cost stays in
    ``gross_invested`` (denominator), so the simple rate UNDERSTATES returns when stale
    positions are present. The rate is a secondary glance metric; XIRR is the rigorous one.
    """
    unrealized: dict[Currency, Decimal] = {}
    for h in valued_holdings:
        if h.unrealized_pnl is not None:
            unrealized[h.quote_ccy] = unrealized.get(h.quote_ccy, _ZERO) + h.unrealized_pnl

    # Sorted, not raw set order: set iteration over Currency is hash-seed dependent ACROSS
    # PROCESSES, so an unsorted loop gives `by_currency` (and therefore the dashboard's
    # 各幣別報酬 chips, which render Object.keys order) a different order on every restart —
    # and churns the golden snapshot on every regeneration. Same determinism fix already
    # applied to the FX freshness lists in dashboard.py. Values are unaffected.
    ccys = sorted(
        set(book.gross_invested) | set(book.realized.by_currency) | set(unrealized),
        key=lambda c: c.value,
    )
    by_ccy: dict[Currency, CurrencyReturn] = {}
    reporting_total = _ZERO
    for ccy in ccys:
        realized_c = book.realized.by_currency.get(ccy, _ZERO)
        unreal_c = unrealized.get(ccy, _ZERO)
        gross_c = book.gross_invested.get(ccy, _ZERO)
        total_c = realized_c + unreal_c
        by_ccy[ccy] = CurrencyReturn(
            realized=realized_c,
            unrealized=unreal_c,
            total_return=total_c,
            gross_invested=gross_c,
            rate=(total_c / gross_c) if gross_c != _ZERO else None,
        )
        reporting_total += convert(total_c, current_fx(ccy, reporting))

    return ReturnSummary(
        by_currency=by_ccy,
        reporting_currency=reporting,
        reporting_total_return=reporting_total,
    )


DateFxRate = Callable[[date, Currency, Currency], Decimal]


@dataclass(frozen=True)
class XirrOutcome:
    """The reporting-currency XIRR plus the flow-window span it was measured over.

    ``rate`` is the annualized money-weighted return (``None`` when not computable).
    ``window_days`` is ``(as_of - earliest input-flow date).days`` — the observation
    window is a property of the cashflow series, so it is populated even when ``rate``
    is ``None``; it is ``None`` only when there are no input flows at all. The UI uses a
    short window (< 365) as a low-confidence hint (XIRR annualizes, so a sub-year window
    makes the figure volatile).
    """

    rate: Decimal | None
    window_days: int | None


#: The cash-movement kinds that are part of the RETURN (AI-D42, owner ruling 2026-08-24).
#:
#: ⚠ This SUPERSEDES the owner's earlier ruling ``D1 = A`` (2026-08-13), which made "cash
#: movements never enter XIRR" an explicit rule; the superseded text and the reason for the
#: change are kept in ``docs/accounting-formula-manual.md`` §4.4.7 limitation 2.
#:
#: The line is the COST OF TRADING AND FINANCING — not ``cash_kinds``' ``credit`` axis and not
#: its ``fx_acquisition`` axis, neither of which separates these from a deposit:
#:
#: * ``REBATE`` — a refund of commission already capitalised into cost basis. FE-D1's
#:   charge-first model refunds 77% next month, which is 0.229% of capital per round trip.
#: * ``INTEREST_EXPENSE`` — the cost of financing the positions.
#: * ``BROKER_FEE`` — a cost of trading that no transaction row carries.
#:
#: EXCLUDED, deliberately: ``DEPOSIT`` / ``WITHDRAW`` / ``OPENING`` are capital movements, and
#: admitting them would quietly redefine XIRR from "the return on money put into securities"
#: into an account return. ``INTEREST`` (earned on idle cash) is excluded too — the principal
#: earning it never entered this metric's denominator, so crediting its yield to the numerator
#: is asymmetric. D12's 重組費 is booked as a ``WITHDRAW``, so that standing limitation stands.
XIRR_CASH_KINDS: frozenset[str] = frozenset(
    {CashKind.REBATE.value, CashKind.INTEREST_EXPENSE.value, CashKind.BROKER_FEE.value}
)


class CashMovementFlow(Protocol):
    """The three fields this metric needs off a cash-movement row.

    A Protocol, not the stored model: ``portfolio/`` must not import ``data_ingestion``
    (architecture.md), and the shape is genuinely all this needs.
    """

    @property
    def date(self) -> date: ...
    @property
    def kind(self) -> str: ...
    @property
    def ccy(self) -> Currency: ...
    @property
    def amount(self) -> Decimal: ...


def is_return_movement(mv: CashMovementFlow) -> bool:
    """Whether *mv* is one of the :data:`XIRR_CASH_KINDS` — the ONE predicate every reader of
    those kinds admits a movement by: XIRR here, and in ``timeseries`` the trend's flows (so
    ``net_invested``, B and the benchmark counterfactual's stream), 交易與融資成本 and the
    trend's FX pair set (``trend_fx_currencies``). A reader that re-derived it could convert a
    movement it loaded no rate for, or load a rate for a movement it never converts."""
    return mv.kind.upper() in XIRR_CASH_KINDS


class DividendIdentity(Protocol):
    """The six fields :func:`dividend_key` reads.

    A Protocol rather than the ledger model because ⑪ (owner ruling 2026-10-05) matches the
    STORED rows the cash pool is built from (``data_ingestion.store.StoredDividend``, whose
    ``type`` is the raw TEXT) against the replay's refusals (ledger ``Dividend``s, whose
    ``type`` is the ``DividendType`` StrEnum — equal to, and hashing like, its TEXT).
    """

    @property
    def account_id(self) -> str: ...
    @property
    def symbol(self) -> str: ...
    @property
    def date(self) -> date: ...
    @property
    def type(self) -> str: ...
    @property
    def gross(self) -> Decimal: ...
    @property
    def net(self) -> Decimal: ...


def dividend_key(dv: DividendIdentity) -> tuple[object, ...]:
    """Value identity for a dividend row, so a REFUSED one can be matched and skipped.

    By value rather than by object identity: the caller may have re-loaded the bundle between
    ``build_book`` and here. Two byte-identical dividend rows on the same position and date
    would both be refused by the replay anyway (they hit the same branch), so set semantics
    are correct — this can never drop a bookable row that merely resembles a refused one.

    Public since R6 (2026-10-04): the dashboard's dividend summary skips the same refused rows
    XIRR does, through this same key, so the two cannot disagree about which payout was booked.
    """
    return (dv.account_id, dv.symbol, dv.date, dv.type, str(dv.gross), str(dv.net))


def booked_dividends[D: DividendIdentity](
    rows: Iterable[D], refused: Iterable[DividendIdentity]
) -> list[D]:
    """The rows of *rows* the replay BOOKED — every one whose :func:`dividend_key` is not
    among *refused* (``Book.refused_dividends``), in their own order.

    ⑪ (owner ruling 2026-10-05): **a dividend the replay refuses is excluded from EVERY cash
    figure**, so one payout has one answer. The replay refuses three kinds — no position by
    the effective date (CR-02), an open declared short (the short PAYS it), a ticker an
    EXCHANGE vacated (E24) — and 總報酬, XIRR and 股利總覽 already left them out while the
    cash pool, the net-worth series, the FX pool and B (the trend's ``net_invested``) still
    added them. This is the ONE place that turns the replay's answer into "the dividends that
    book"; every one of those readers takes its dividends through it (or through
    :func:`unbooked_dividends`, its complement, for a list that shows the refused rows), and
    none re-derives a refusal rule.
    """
    keys = {dividend_key(dv) for dv in refused}
    return [r for r in rows if dividend_key(r) not in keys]


def unbooked_dividends[D: DividendIdentity](
    rows: Iterable[D], refused: Iterable[DividendIdentity]
) -> list[D]:
    """The complement of :func:`booked_dividends`: the rows of *rows* the replay REFUSED.

    For a surface that LISTS dividend rows (the 現金收支明細 statement, its CSV and its printed
    report): a refused row is still listed — a payout must never vanish from a list the owner
    reconciles against a broker statement — but it moves no balance (⑪)."""
    keys = {dividend_key(dv) for dv in refused}
    return [r for r in rows if dividend_key(r) in keys]


@dataclass(frozen=True)
class DividendRefusals:
    """The replay's answer to "which dividends book" — with its OWN reason for each refusal.

    ⑪ / X1 (owner rulings 2026-10-05): a refused payout counts in no balance, no flow and no
    total, and every surface that LISTS dividend rows lists it marked 「未入帳（待釐清）」 with
    the reason. One object carries both halves, so a reader cannot take the exclusion from one
    replay and the marks from another: balances and flows read :meth:`booked`, listings read
    :meth:`why` (``None`` on a booked row) — the same :func:`dividend_key` membership either way.

    ``reasons`` maps a refused row's key to ``(cause, sentence)``: ``"orphan"`` with CR-02's
    :func:`~portfolio.cost_basis.orphan_dividend_message`, or a K3
    :class:`~portfolio.results.UnbookableDividend`'s ``cause`` (``"short"`` / ``"vacated"``)
    with its ``reason`` — the strict path's sentence, verbatim, so every surface explains the
    refusal in the replay's words and none re-derives it.
    """

    refused: tuple[Dividend, ...] = ()
    reasons: Mapping[tuple[object, ...], tuple[str, str]] = field(default_factory=dict)

    @cached_property
    def _keys(self) -> frozenset[tuple[object, ...]]:
        return frozenset(dividend_key(dv) for dv in self.refused)

    def booked[D: DividendIdentity](self, rows: Iterable[D]) -> list[D]:
        """:func:`booked_dividends` over this refusal."""
        return [r for r in rows if dividend_key(r) not in self._keys]

    def unbooked[D: DividendIdentity](self, rows: Iterable[D]) -> list[D]:
        """:func:`unbooked_dividends` over this refusal."""
        return [r for r in rows if dividend_key(r) in self._keys]

    def why(self, row: DividendIdentity) -> tuple[str, str] | None:
        """``(cause, sentence)`` when the replay refused *row*; ``None`` when it booked it.

        A refused row with no recorded reason (unreachable today: every refusal is an orphan
        or an :class:`~portfolio.results.UnbookableDividend`) reads ``"refused"`` with the
        statement's generic sentence rather than passing as booked."""
        key = dividend_key(row)
        if key not in self._keys:
            return None
        return self.reasons.get(key, ("refused", UNBOOKED_DIVIDEND_REASON))

    def reason(self, row: DividendIdentity) -> str | None:
        """The sentence half of :meth:`why` — what a listing prints beside the mark."""
        why = self.why(row)
        return None if why is None else why[1]


def dividend_refusals(book: Book | None) -> DividendRefusals:
    """The :class:`DividendRefusals` of a replay the caller already ran; none for ``None``
    (a ledger the replay could not book at all — see :func:`replay_refusals`)."""
    if book is None:
        return DividendRefusals()
    reasons: dict[tuple[object, ...], tuple[str, str]] = {
        dividend_key(d): ("orphan", orphan_dividend_message(d)) for d in book.orphan_dividends
    }
    reasons.update({dividend_key(u.dividend): (u.cause, u.reason)
                    for u in book.unbookable_dividends})
    return DividendRefusals(refused=tuple(book.refused_dividends), reasons=reasons)


def replay_dividend_refusals(bundle: LedgerBundle) -> DividendRefusals:
    """Replay *bundle* once and return its :class:`DividendRefusals` (⑪ / X1).

    For a reader that holds the ledgers but not a book — the cash API's one snapshot
    (``api/routers/cash.py::load_cash_ledgers``), the 現金收支明細 exports, the ledger tab, the
    printed ledger report, the drawer's 配息史, the AI-facing dividend rows, the trend. A
    reader that already replayed the same bundle calls :func:`dividend_refusals` on its own
    book instead and pays for no second replay.

    The replay is run exactly as ``build_dashboard`` runs it: unregistered rows dropped first
    (they cannot be booked, and ``cash_balances`` skips them too), ``allow_oversell=True`` so a
    賣超 or a refused dividend is recorded on the book rather than raised. A ledger the replay
    refuses OUTRIGHT (a DRIP row with no share count, an uncomputable value — the dashboard
    answers 422 naming the row) yields no refusal: there is none to read, so every payout
    counts and none is marked, as before ⑪, on every surface and every guard alike, because
    they all read this one answer (owner ruling X2, 2026-10-05: keep).
    """
    try:
        book = build_book(bundle.without_unregistered(), allow_oversell=True)
    except (ValueError, KeyError, ArithmeticError):
        return DividendRefusals()
    return dividend_refusals(book)


def replay_refusals(bundle: LedgerBundle) -> list[Dividend]:
    """``Book.refused_dividends`` of the dashboard-path replay of *bundle* (⑪) — the rows of
    :func:`replay_dividend_refusals`, for a reader that needs no reasons (the trend)."""
    return list(replay_dividend_refusals(bundle).refused)


def xirr_reporting(
    transactions: list[Transaction],
    dividends: list[Dividend],
    opening: list[OpeningInventory],
    holdings: list[Holding],
    instruments: dict[str, Instrument],
    fx_at: DateFxRate,
    current_prices: dict[str, Decimal],
    current_fx: FxRate,
    as_of: date,
    reporting: Currency,
    *,
    refused_dividends: list[Dividend],
    cash_movements: Sequence[CashMovementFlow],
) -> XirrOutcome:
    """Reporting-currency money-weighted XIRR + its observation window.

    Flows: buy - (gross incl. fees+tax), sell + (net), cash dividend + (net), DRIP/stock
    neutral, opening - (original_cost_total at build_date), the three trading/financing
    cash kinds signed by the shared table (see XIRR_CASH_KINDS), final market value +
    at as_of. Each flow converted at its trade-date FX; final value at current spot.

    ``refused_dividends`` and ``cash_movements`` are REQUIRED keyword arguments with no
    default, deliberately: a default would let a caller silently ship the pre-2026-08-24
    behaviour, and both omissions are invisible in the output (a plausible rate, quietly
    missing flows). Forgetting either is now a mypy error and a TypeError.

    All-or-nothing on missing prices: if ANY held symbol lacks a current price the rate
    is None (the terminal value can't be formed), unlike total_return/allocation which
    degrade partially. The rate is also None on non-convergence / non-finite results.
    ``window_days`` is still reported in every one of these degraded cases.
    """

    def ccy_of(symbol: str) -> Currency:
        inst = instruments.get(symbol)
        if inst is None:
            raise KeyError(f"unknown instrument: {symbol}")
        return inst.quote_ccy

    dates: list[date] = []
    amounts: list[float] = []

    def add(d: date, ccy: Currency, native: Decimal) -> None:
        dates.append(d)
        amounts.append(float(convert(native, fx_at(d, ccy, reporting))))

    for oi in opening:
        add(oi.build_date, ccy_of(oi.symbol), -oi.original_cost_total)
    for tx in transactions:
        ccy = ccy_of(tx.symbol)
        gross = settled_notional(tx.quantity, tx.price, ccy)  # the cash that moved
        if tx.side is Side.BUY:
            add(tx.trade_date, ccy, -(gross + tx.fees + tx.tax))
        else:
            add(tx.trade_date, ccy, gross - tx.fees - tx.tax)
    # A dividend the REPLAY refused (it landed on an open short, which this ledger has
    # no debit row for) must not be an inflow here either — otherwise one payment gets
    # three answers: excluded from 總報酬, counted by XIRR, and flagged 待釐清 on the
    # trend. Matched by VALUE, per dividend_key (review 2026-08-24).
    refused = {dividend_key(dv) for dv in refused_dividends}
    for dv in dividends:
        if dv.type in CASH_DIVIDEND_TYPES:  # CASH (TW) + NET (MY) — one definition
            if dividend_key(dv) in refused:
                continue
            add(dv.date, ccy_of(dv.symbol), dv.net)
        # DRIP / STOCK are neutral (no external cashflow)
    # AI-D42: the three trading/financing costs. A movement carries its OWN currency
    # (no symbol to look one up from) and its sign comes from the shared cash_kinds
    # table, so it can never be a credit here and a debit in the pool balance.
    for mv in cash_movements:
        if is_return_movement(mv):
            add(mv.date, mv.ccy, movement_sign(mv.kind) * mv.amount)

    # Observation window: as_of minus the earliest INPUT flow date (measured before the
    # terminal value is appended). Independent of whether the rate ultimately converges.
    window_days = (as_of - min(dates)).days if dates else None

    final = Decimal("0")
    for h in holdings:
        price = current_prices.get(h.symbol)
        if price is None:
            return XirrOutcome(rate=None, window_days=window_days)
        final += convert(price * h.shares, current_fx(h.quote_ccy, reporting))
    if final != _ZERO:
        dates.append(as_of)
        amounts.append(float(final))

    try:
        rate = _xirr(dates, amounts)
    except InvalidPaymentsError:
        # No sign change in the cashflow series (e.g. all outflows) — not computable.
        return XirrOutcome(rate=None, window_days=window_days)
    if rate is None or not math.isfinite(rate):
        # Non-finite (e.g. conflicting same-date flows yield inf) — never surface it.
        return XirrOutcome(rate=None, window_days=window_days)
    return XirrOutcome(rate=Decimal(str(rate)), window_days=window_days)
