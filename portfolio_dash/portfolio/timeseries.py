"""Daily replay trend: market value + cumulative net invested per day.

Pure function over in-memory inputs (no DB handle): the combiner bulk-loads price/FX
history once and passes it in. Valuation uses the carry-forward convention (latest
stored value on-or-before the day); a day a held symbol has no price at all — or on
which the replay could not apply a corporate action — is flagged ``incomplete``
(never guessed). Any ledger flow whose date has no on-or-before FX makes the whole
series unavailable (consistent with the XIRR rule).

**Every public builder here REFUSES a decimal fault rather than letting one escape**
(R-1, 2026-08-29): an ``ArithmeticError`` over ledger data is re-typed into
:class:`~portfolio_dash.portfolio.cost_basis.UnbookableLedgerError` — a ``ValueError``, so
``api/errors.py`` answers **422** with a zh sentence instead of 「系統發生未預期的錯誤」.
``build_book``'s own wrap cannot cover these lines: the dashboard replays with
``allow_oversell=True``, and that branch never multiplies an oversized SELL at all, so the
overflow arrives here unrefused. See :func:`build_reporting_flows`.

⚠ **KNOWN GAP, measured 2026-08-29 and recorded rather than left implicit.** This module is
not the last consumer of ledger quantities outside ``build_book``. The same direct-SQL row
still escapes, unrefused, from three siblings — ``forex/pools.py::foreign_cash_balance``
(reached from ``build_dashboard``'s FX summary, so a FOREIGN-currency account still answers
500 where a TWD one now answers 422), ``portfolio/cash.py::cash_balances`` (``GET /api/cash``,
both sides), and ``api/routers/ledgers.py::transactions`` (its own trade total).
They are named here because a guard that stops at a module boundary is exactly the shape of
defect this one was: fixed once, believed general.
"""

from bisect import bisect_right
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, timedelta
from decimal import ROUND_FLOOR, Decimal, getcontext

from portfolio_dash.portfolio.benchmark_counterfactual import ReportingFlow
from portfolio_dash.portfolio.cost_basis import (
    UnbookableLedgerError,
    _uncomputable_message,
    build_book,
)
from portfolio_dash.portfolio.dashboard_models import TrendPoint, TrendSeries, UnconvertibleFlow
from portfolio_dash.portfolio.price_basis import price_in
from portfolio_dash.portfolio.results import Book

# ONE definition of both, shared with the metric that already takes these kinds: an oracle
# of B that re-derived either would be free to disagree with XIRR about which flows exist.
from portfolio_dash.portfolio.returns import (
    CashMovementFlow,
    DividendIdentity,
    booked_dividends,
    is_return_movement,
    replay_refusals,
)
from portfolio_dash.shared.cash_kinds import movement_sign
from portfolio_dash.shared.corporate_actions import ActionIndex
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.fx import convert
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import CASH_DIVIDEND_TYPES, Side
from portfolio_dash.shared.models.ledger import LedgerBundle, counts_by
from portfolio_dash.shared.money import settled_notional

_ZERO = Decimal("0")
_ONE = Decimal("1")

# Ascending (date, value) series, as bulk-loaded by the combiner.
PriceHistory = dict[str, list[tuple[date, Decimal]]]
FxHistory = dict[tuple[Currency, Currency], list[tuple[date, Decimal]]]


def _entry_at_or_before(
    series: list[tuple[date, Decimal]], on: date
) -> tuple[date, Decimal] | None:
    """Latest ``(date, value)`` at-or-before ``on`` over an ascending series, else None.

    The price path needs the DATE as well as the value: §5.1(d)'s read rule re-expresses a
    carried-forward close over the window ``(pd, d]``, where ``pd`` is the date of the row
    actually returned — which is precisely the fact a value-only lookup throws away.
    """
    idx = bisect_right(series, on, key=lambda item: item[0])
    if idx == 0:
        return None
    return series[idx - 1]


def _at_or_before(series: list[tuple[date, Decimal]], on: date) -> Decimal | None:
    """Latest value at-or-before ``on`` over an ascending series, else None.

    The FX callers' view: a rate is not re-expressed by a corporate action, so they need
    the value alone. One lookup, two projections — never two ``bisect_right`` calls.
    """
    entry = _entry_at_or_before(series, on)
    return entry[1] if entry is not None else None


def _fx_at(history: FxHistory, on: date, base: Currency, quote: Currency) -> Decimal | None:
    """Carry-forward rate: identity -> direct pair -> inverted pair -> None."""
    if base == quote:
        return _ONE
    direct = history.get((base, quote))
    if direct is not None:
        rate = _at_or_before(direct, on)
        if rate is not None:
            return rate
    inverse = history.get((quote, base))
    if inverse is not None:
        rate = _at_or_before(inverse, on)
        if rate is not None:
            return _ONE / rate
    return None


class _Cursor:
    """:func:`_entry_at_or_before` over one ascending series for NON-DECREASING days, by a
    moving index (PERF spec §8.2): the same entry — the last one dated on or before the day —
    at amortised constant cost instead of a bisect from the start on every call."""

    __slots__ = ("_i", "_series")

    def __init__(self, series: list[tuple[date, Decimal]]) -> None:
        self._series = series
        self._i = -1  # index of the last entry dated on or before the latest day asked

    def at(self, on: date) -> tuple[date, Decimal] | None:
        series, i = self._series, self._i
        while i + 1 < len(series) and series[i + 1][0] <= on:
            i += 1
        self._i = i
        return series[i] if i >= 0 else None


class _FxCarry:
    """:func:`_fx_at` into one quote currency for NON-DECREASING days, by a moving index per
    stored pair: identity → direct pair → inverted pair → ``None``, with the same arithmetic
    (``_ONE / rate`` on the inverse) and the same "a pair whose series has no row on or before
    the day falls through to its inverse"."""

    def __init__(self, history: FxHistory, quote: Currency) -> None:
        self._history = history
        self._quote = quote
        self._cursors: dict[tuple[Currency, Currency], _Cursor | None] = {}

    def _cursor(self, pair: tuple[Currency, Currency]) -> _Cursor | None:
        if pair not in self._cursors:
            series = self._history.get(pair)
            self._cursors[pair] = None if series is None else _Cursor(series)
        return self._cursors[pair]

    def rate(self, on: date, base: Currency) -> Decimal | None:
        quote = self._quote
        if base == quote:
            return _ONE
        direct = self._cursor((base, quote))
        if direct is not None:
            entry = direct.at(on)
            if entry is not None:
                return entry[1]
        inverse = self._cursor((quote, base))
        if inverse is not None:
            entry = inverse.at(on)
            if entry is not None:
                return _ONE / entry[1]
        return None


def _running_sum_is_exact(amounts: Sequence[Decimal]) -> bool:
    """Whether summing *amounts* (and the reference's starting ``Decimal("0")``) is exact in
    EVERY order and for every subset, in the current context (PERF spec §4 "Decimal order").

    The order-independent bound: with *e* the smallest exponent among the amounts and the
    starting zero, every partial sum is a multiple of ``10^e`` no larger than ``Σ|amount|``;
    when that is below ``10^(prec + e)`` its coefficient has fewer than ``prec`` digits, so no
    addition rounds — and an exact Decimal sum's value AND exponent (the minimum of the
    operands') do not depend on the order. ``ROUND_FLOOR`` is refused outright: it is the one
    rounding mode under which an exact zero's SIGN depends on the operands' order. Trapping
    ``Inexact`` on the date-ordered sum alone would not prove the list-ordered one exact.
    """
    ctx = getcontext()
    if ctx.rounding == ROUND_FLOOR:
        return False
    exponents = [0]
    for amount in amounts:
        exponent = amount.as_tuple().exponent
        if not isinstance(exponent, int):  # NaN / Infinity
            return False
        exponents.append(exponent)
    e = min(exponents)
    total = 0
    for amount in amounts:
        _sign, digits, exponent = amount.as_tuple()
        assert isinstance(exponent, int)
        if len(digits) + exponent - e > ctx.prec:
            return False
        total += int("".join(map(str, digits)) or "0") * 10 ** (exponent - e)
    limit: int = 10 ** ctx.prec
    return total < limit


def _reference_net_invested(converted: Sequence[tuple[date, Decimal]], day: date) -> Decimal:
    """``net_invested`` on *day* exactly as v0.1.31 summed it: from ``Decimal("0")``, in the
    flow list's own order (not date order), every flow that counts by *day*."""
    net_invested = _ZERO
    for d, amt in converted:
        if counts_by(d, day):
            net_invested += amt
    return net_invested


class _NetInvested:
    """``net_invested`` for NON-DECREASING days, equal to :func:`_reference_net_invested`.

    A running sum in date order when :func:`_running_sum_is_exact` holds for the flow list
    (then no partial sum in either order rounds, so the two agree digit for digit and
    exponent for exponent); otherwise the reference sum itself, recomputed only on the first
    day asked and on a day some flow starts counting — between two such days the set of
    counted flows, and therefore the sum, is unchanged.
    """

    def __init__(self, converted: Sequence[tuple[date, Decimal]]) -> None:
        self._converted = converted
        self.exact = _running_sum_is_exact([amt for _d, amt in converted])
        self._ordered = sorted(converted, key=lambda flow: flow[0])
        self._next = 0
        # The fallback's clock: the latest flow day on or before the day asked.
        self._flow_days = _Cursor([(d, _ZERO) for d in sorted({d for d, _a in converted})])
        self._counted_through: date | None = None
        self._value = _ZERO
        self._started = False

    def __call__(self, day: date) -> Decimal:
        if self.exact:
            ordered = self._ordered
            while self._next < len(ordered) and counts_by(ordered[self._next][0], day):
                self._value += ordered[self._next][1]
                self._next += 1
            return self._value
        latest = self._flow_days.at(day)
        latest_day = None if latest is None else latest[0]
        if not self._started or latest_day != self._counted_through:
            self._value = _reference_net_invested(self._converted, day)
            self._counted_through = latest_day
            self._started = True
        return self._value


class _GivenBooks:
    """The book each valued day reads from caller-supplied ``(day, book)`` pairs, ascending:
    the latest one on or before the day. A pair list that skips a day some row counts from
    (*rebuild_on*) — so a stale book would be read — is refused, never served."""

    def __init__(self, books: Iterable[tuple[date, Book]], rebuild_on: set[date]) -> None:
        self._books = iter(books)
        self._next: tuple[date, Book] | None = next(self._books, None)
        self._current: tuple[date, Book] | None = None
        self._event_days = _Cursor([(d, _ZERO) for d in sorted(rebuild_on)])

    def at(self, day: date) -> Book:
        while self._next is not None and self._next[0] <= day:
            if self._current is not None and self._next[0] < self._current[0]:
                raise ValueError("books must be in ascending day order")
            self._current = self._next
            self._next = next(self._books, None)
        latest_event = self._event_days.at(day)
        if self._current is None or (latest_event is not None
                                     and self._current[0] < latest_event[0]):
            raise ValueError(f"no book for {day}: the books must include every day a row "
                             "counts from (trend_book_days)")
        return self._current[1]


#: Why the trend is unavailable for want of a rate (zh, server-owned; accountant MINOR 4, owner
#: ruling 2026-10-22): the FIRST flow with no rate on or before its own date, by its date and
#: its pair. ONE sentence, read by every surface that says the trend cannot be computed — the
#: dashboard's ``freshness.trend_unavailable_reason``, a close's ``notes.trend_unavailable``
#: and 當期報酬 reason (live and stored), and the settlement run's report.
TREND_FX_MISSING_ZH = "{day} 起缺少 {pair} 匯率（當日或之前無匯率），走勢無法計算"
#: The same when no flow can be named — never reached while a missing rate is the cause
#: (:func:`first_unconvertible_flow` names it); a caller holding every point passes no reason.
TREND_FX_MISSING_UNNAMED_ZH = "部分投入流量缺少當日或之前的匯率，走勢無法計算"
#: Its variant for a close dated BEFORE that flow, computed live (review 2026-10-22): the day
#: itself would have a trend point, but the LIVE series is all-or-nothing and is unavailable
#: as a whole — the close says so, never that its own day lacks a rate.
TREND_FX_MISSING_LIVE_ZH = "即時走勢因 {day} 起缺少 {pair} 匯率而無法計算"


def trend_unavailable_reason(flow: UnconvertibleFlow | None, *,
                             as_at: date | None = None) -> str:
    """The ONE sentence of an unavailable trend (:data:`TREND_FX_MISSING_ZH`), from
    :func:`first_unconvertible_flow`'s answer — the flow's date and pair. With *as_at* before
    that date (a live close of a day before the flow, ``api/snapshots.py``) its variant
    :data:`TREND_FX_MISSING_LIVE_ZH`, from the same date and pair."""
    if flow is None:
        return TREND_FX_MISSING_UNNAMED_ZH
    template = (TREND_FX_MISSING_LIVE_ZH if as_at is not None and as_at < flow.on
                else TREND_FX_MISSING_ZH)
    return template.format(day=flow.on.isoformat(), pair=flow.pair)


def first_unconvertible_flow(
    bundle: LedgerBundle,
    fx_history: FxHistory,
    reporting: Currency,
    *,
    cash_movements: Sequence[CashMovementFlow],
    refused_dividends: Iterable[DividendIdentity],
) -> UnconvertibleFlow | None:
    """The earliest flow of :func:`build_reporting_flows` with no rate on or before its own
    date (ties: the currency code), or ``None`` when every flow converts — the ONE owner of
    "which flow and pair cannot convert". It runs the flow builder itself (the same flows, the
    same ``is_return_movement`` predicate, the same carry-forward lookup), so it names a flow
    exactly when :func:`build_reporting_flows` returns ``None``.

    The settlement worker's first unconvertible day U is this flow's date (never before the
    trend's first day): every prefix of the ledger cut on or after it holds the flow."""
    _flows, failed = _reporting_flows(bundle, fx_history, reporting,
                                      cash_movements=cash_movements,
                                      refused_dividends=refused_dividends)
    if not failed:
        return None
    return min(failed, key=lambda f: (f.on, f.ccy.value))


def build_reporting_flows(
    bundle: LedgerBundle,
    fx_history: FxHistory,
    reporting: Currency,
    *,
    cash_movements: Sequence[CashMovementFlow],
    refused_dividends: Iterable[DividendIdentity],
) -> list[ReportingFlow] | None:
    """Every net-invested flow, in the reporting currency at its OWN trade-date FX.

    Signs mirror the XIRR conventions, negated: opening ``+cost``, buy ``+gross`` (incl.
    fees + tax), sell ``−net``, cash dividend ``−net``. Returns ``None`` when any flow's date
    has no on-or-before FX rate for its pair — the same honest bail as the trend itself, and
    the same rule XIRR uses.

    Raises :class:`~portfolio_dash.portfolio.cost_basis.UnbookableLedgerError` (never a raw
    ``ArithmeticError``) when a row's numbers cannot be computed at all — a MISSING RATE and
    an UNCOMPUTABLE ROW are different answers on purpose: the first degrades the whole series
    honestly, the second is a row the owner must go and fix, and the sentence names it.

    ⚠ This is the ONE definition of 「what counts as money put in」, and it is extracted rather
    than duplicated because :mod:`portfolio.benchmark_counterfactual` spends exactly this
    stream on an index instead. Two copies would let the portfolio and its counterfactual
    quietly disagree about which flows exist, and the whole comparison rests on them being the
    same money on the same dates. Each flow carries its ``market`` so the counterfactual can
    route it to that market's benchmark; the trend simply ignores that field. Which flow made
    it ``None`` is :func:`first_unconvertible_flow`'s answer, from the same loop.

    **Cash movements are included since AI-D48** (owner ruling 2026-08-27), on exactly the
    three kinds ``xirr_reporting`` has taken since AI-D42: ``REBATE`` / ``INTEREST_EXPENSE`` /
    ``BROKER_FEE`` — the costs of TRADING AND FINANCING. ``DEPOSIT`` / ``WITHDRAW`` /
    ``OPENING`` stay out as capital movements, and ``INTEREST`` stays out because the idle
    principal earning it never entered this figure. ``cash_movements`` is a REQUIRED keyword
    argument with no default: a default would let a caller silently ship the pre-AI-D48 number,
    which is a plausible figure quietly missing flows — invisible in the output, so it is made
    visible in the type instead (mypy error and ``TypeError``).

    Why it mattered: **A and B are printed side by side** (AI-D41) and their difference is
    labelled 「本金匯率效果」. While XIRR counted a 77% rebate and B did not, that label was
    false — the difference was 「本金匯率效果 ＋ 三類現金帳」. R4 then measured its excess
    return against B, so a broker fee B could not see read as beating the index.

    A cash movement carries **no market and no symbol** (``market=None``), and AI-D49 makes
    that load-bearing: :func:`portfolio.benchmark_counterfactual.counterfactual` leaves those
    flows out of both legs, so the costs are charged to the portfolio and never placed on the
    index. Sign: the movement's ``credit`` from the shared table, NEGATED like every other
    flow here — a fee (debit) therefore RAISES ``net_invested`` exactly as a buy-side fee
    always has, and a rebate lowers it.

    **A dividend the replay REFUSED is not a flow here** (K2, owner ruling 2026-10-05, with ⑪):
    ``refused_dividends`` is ``Book.refused_dividends`` of the replay over *bundle*, taken
    through ``returns.booked_dividends`` — the same rows XIRR skips (``xirr_reporting``'s own
    ``refused_dividends``). B (``total_value − net_invested``) was withheld only while the
    trend's last day was ``incomplete``, and two refusals leave it complete: a short covered
    after the payout landed on it, and a vacated ticker whose successor is flat — B then
    counted a payout 總報酬 and XIRR left out, and the index counterfactual was handed the
    same phantom withdrawal. Required with no default, like ``cash_movements``: a default
    would ship the old figure in silence.
    """
    flows, failed = _reporting_flows(bundle, fx_history, reporting,
                                     cash_movements=cash_movements,
                                     refused_dividends=refused_dividends)
    return None if failed else flows


def _reporting_flows(
    bundle: LedgerBundle,
    fx_history: FxHistory,
    reporting: Currency,
    *,
    cash_movements: Sequence[CashMovementFlow],
    refused_dividends: Iterable[DividendIdentity],
) -> tuple[list[ReportingFlow], list[UnconvertibleFlow]]:
    """:func:`build_reporting_flows`' loop: the converted flows, and every flow with no rate on
    or before its date. Once one flow fails, the rest are only looked up, never converted — so
    a ledger it refuses for a missing rate converts (and can raise on) exactly the rows the
    first-failure return always converted."""
    def quote_ccy(symbol: str) -> Currency:
        inst = bundle.instruments.get(symbol)
        if inst is None:
            raise KeyError(f"unknown instrument: {symbol}")
        return inst.quote_ccy

    def market_of(symbol: str) -> Market:
        inst = bundle.instruments.get(symbol)
        if inst is None:
            raise KeyError(f"unknown instrument: {symbol}")
        return inst.market

    # R-1 (2026-08-29): this is the SECOND consumer of ledger quantities, and until then the
    # only guarded one was ``build_book``. That guard structurally cannot cover this line: the
    # dashboard replays with ``allow_oversell=True``, which sends an oversized SELL down the
    # 賣超 degradation branch — a branch that performs NO multiplication, so the replay returns
    # cleanly and the ``decimal.Overflow`` lands here instead, on ``t.quantity * t.price``.
    # Measured on one direct-SQL row: ``GET /api/dashboard`` answered 422 as a BUY and **500
    # 「系統發生未預期的錯誤」** as a SELL, and ``GET /api/performance/twr`` inherited both.
    #
    # ``booking`` names the offending row exactly as ``build_book``'s own wrap does, so both
    # sides of the same trade produce the SAME sentence.
    booking: object | None = None
    try:
        # The source event travels with each raw flow (the 4th element) so the CONVERSION loop
        # below can name the row too: ``amount * rate`` is that row's money meeting a stored
        # rate, and a message that stops naming rows halfway is one the owner cannot act on.
        raw: list[tuple[date, str, Decimal, object]] = []
        for o in bundle.opening:
            booking = o
            raw.append((o.build_date, o.symbol, o.original_cost_total, o))
        for t in bundle.transactions:
            booking = t
            gross = settled_notional(t.quantity, t.price, quote_ccy(t.symbol))  # 價金
            if t.side is Side.BUY:
                raw.append((t.trade_date, t.symbol, gross + t.fees + t.tax, t))
            else:
                raw.append((t.trade_date, t.symbol, -(gross - t.fees - t.tax), t))
        for dv in booked_dividends(bundle.dividends, refused_dividends):
            if dv.type in CASH_DIVIDEND_TYPES:  # CASH (TW) + NET (MY) — one definition
                booking = dv
                raw.append((dv.date, dv.symbol, -dv.net, dv))

        out: list[ReportingFlow] = []
        failed: list[UnconvertibleFlow] = []

        def rate_on(on: date, ccy: Currency) -> Decimal | None:
            if not failed:
                return _fx_at(fx_history, on, ccy, reporting)
            # past the first failure a row is only looked up — the first-failure return never
            # reached it, so nothing here may raise: a stored rate is a rate, whatever it is
            try:
                return _fx_at(fx_history, on, ccy, reporting)
            except ArithmeticError:
                return _ONE

        for d, symbol, amount, source in raw:
            booking = source
            if failed and symbol not in bundle.instruments:
                continue  # the first-failure return never reached this row's KeyError
            ccy = quote_ccy(symbol)
            rate = rate_on(d, ccy)
            if rate is None:
                failed.append(UnconvertibleFlow(d, ccy, reporting))
                continue
            if not failed:
                out.append(ReportingFlow(d, market_of(symbol), convert(amount, rate), symbol))
        # AI-D48. Each movement carries its OWN currency (there is no symbol to look one up
        # from) and its sign comes from the shared table, so a kind can never be a credit here
        # and a debit in the pool balance.
        for mv in cash_movements:
            booking = mv
            if not is_return_movement(mv):
                continue
            rate = rate_on(mv.date, mv.ccy)
            if rate is None:
                failed.append(UnconvertibleFlow(mv.date, mv.ccy, reporting))
                continue
            if not failed:
                out.append(ReportingFlow(
                    mv.date, None, convert(-movement_sign(mv.kind) * mv.amount, rate), ""))
        return out, failed
    except ArithmeticError as exc:
        # The CLASS, not one cause — ``decimal.Overflow`` and ``decimal.DivisionByZero`` are
        # SIBLINGS of ``InvalidOperation``, not subclasses, and ``ZeroDivisionError`` is in it
        # too. ``UnbookableLedgerError`` is a ``ValueError`` on purpose, so every existing
        # ``except (ValueError, KeyError)`` degradation site and ``api/errors.py``'s 422 handler
        # both inherit this. ``from exc`` keeps the failing operation in the traceback: only the
        # sentence the owner reads is replaced, never the diagnosis.
        raise UnbookableLedgerError(_uncomputable_message(booking)) from exc


def trading_financing_cost(
    cash_movements: Sequence[CashMovementFlow],
    fx_history: FxHistory,
    reporting: Currency,
) -> Decimal | None:
    """The reporting-currency P&L effect of the three AI-D48 cash kinds. ``None`` if a rate
    is missing on any of their dates — the same all-or-nothing bail B itself takes.

    Sign is the P&L one, NOT the ``net_invested`` one: a broker fee is **negative** here and
    positive there. Needed because AI-D48 changed what ``B − A`` means. B now counts these
    costs and A never did, so their difference is no longer 「本金匯率效果」 — it is
    「本金匯率效果 ＋ 交易與融資成本」, which is precisely the mislabelling AI-D48 exists to
    remove, reproduced one field over. The decomposition is therefore THREE terms:

        B = A + 本金匯率效果 + 交易與融資成本

    and ``principal_fx_effect`` is computed by subtracting this out rather than being left as
    a residual that quietly absorbs whatever else joins B next.

    Raises :class:`~portfolio_dash.portfolio.cost_basis.UnbookableLedgerError` (never a raw
    ``ArithmeticError``) on an uncomputable movement amount — see :func:`build_reporting_flows`.
    """
    booking: object | None = None
    try:
        total = _ZERO
        for mv in cash_movements:
            booking = mv
            if not is_return_movement(mv):
                continue
            rate = _fx_at(fx_history, mv.date, mv.ccy, reporting)
            if rate is None:
                return None
            total += convert(movement_sign(mv.kind) * mv.amount, rate)
        return total
    except ArithmeticError as exc:
        # R-1, same net: a hand-edited movement amount overflows on conversion here exactly as a
        # hand-edited quantity does in the flow builder. A ``CashMovementFlow`` carries neither
        # symbol nor account, so ``_uncomputable_message`` renders the GENERAL sentence — it
        # never invents an identity it cannot read off the row.
        raise UnbookableLedgerError(_uncomputable_message(booking)) from exc


def trend_symbols(bundle: LedgerBundle) -> list[str]:
    """Every symbol the trend reads a price history for, sorted: the symbol of each trade,
    dividend and opening, and BOTH ends of every corporate action (audit F-50: a SPINOFF child
    appears in no other ledger, so omitting it loaded no price for a position the replay
    booked correctly, and every day after the spinoff read ``incomplete``). The live build
    (``dashboard.py`` step 9) and the settlement worker load the trend's histories over this
    one set. ``bundle.instruments`` holds each of them once the bundle is
    ``without_unregistered()``."""
    return sorted({t.symbol for t in bundle.transactions} | {d.symbol for d in bundle.dividends}
                  | {o.symbol for o in bundle.opening}
                  | {a.from_symbol for a in bundle.actions} | {a.to_symbol for a in bundle.actions})


def trend_fx_currencies(bundle: LedgerBundle,
                        cash_movements: Iterable[CashMovementFlow]) -> set[Currency]:
    """The currencies the trend converts into the reporting currency: its FX pair set (each
    against the reporting currency, in both directions). The live build (``dashboard.py``
    step 9, whose ``trend_fx_history`` B, 交易與融資成本 and the benchmark counterfactual read
    too) and the settlement worker both load this set, each over the ledger and the movements
    cut at the last day it values (the live build at ``as_of``, the worker at its run's
    ``through``), so over the same day range they convert the same flows at the same rates
    and agree on ``available``:

    - the quote currency of every :func:`trend_symbols` symbol — every trade, dividend and
      opening flow, and every position valued on a day;
    - the currency of every cash movement the trend takes as a flow — the movements
      :func:`build_reporting_flows` and :func:`trading_financing_cost` admit, by the same
      predicate (:func:`~portfolio_dash.portfolio.returns.is_return_movement`).

    PERF-F2: the set used to hold the quote currencies only, so a REBATE / INTEREST_EXPENSE /
    BROKER_FEE in a currency no instrument is quoted in found no rate, and the WHOLE trend was
    unavailable (and with it net worth, B, B − A, 交易與融資成本 and the benchmark) although its
    rate was stored and XIRR counted it. A movement with no rate on or before its date still
    makes the series unavailable: that is a missing rate, not a missing pair."""
    quoted = {bundle.instruments[sym].quote_ccy for sym in trend_symbols(bundle)}
    return quoted | {mv.ccy for mv in cash_movements if is_return_movement(mv)}


def trend_start(bundle: LedgerBundle) -> date | None:
    """The trend's first day: the earliest trade, dividend (``effective_date``) or opening
    date — ``None`` on a ledger with none of them (no trend). A corporate action does not
    start it: an action before the first position is replayed into the first day's book."""
    # R6: `effective_date`, matching `LedgerBundle.through` and `build_book`'s ordering.
    # Latent rather than live — a 配股's ex-date cannot precede the position that earns it,
    # so no real ledger starts on one — but three date filters spelling the rule three ways
    # is how two of them drift, and this was the third.
    event_dates = (
        [t.trade_date for t in bundle.transactions]
        + [d.effective_date for d in bundle.dividends]
        + [o.build_date for o in bundle.opening]
    )
    return min(event_dates) if event_dates else None


def trend_book_days(bundle: LedgerBundle, *, start: date, end: date) -> list[date]:
    """The days to ask :func:`~portfolio_dash.portfolio.cost_basis.iter_books` for, so that
    :func:`value_series` over ``[start, end]`` has every book it reads: the latest day on or
    before the first valued day on which some row counts, then every such day up to *end*
    (``LedgerBundle.counts_on_dates`` — the days the per-day cut changes). Empty when there is
    no trend or no day to value."""
    first = trend_start(bundle)
    if first is None:
        return []
    begin = max(start, first)
    if end < begin:
        return []
    days = sorted(bundle.counts_on_dates())
    lead = [d for d in days if d <= begin]
    return lead[-1:] + [d for d in days if begin < d <= end]


def holds_open_option(book: Book, instruments: Mapping[str, Instrument]) -> bool:
    """Whether *book* holds an open option contract — the per-day test behind
    ``TrendSeries.options_value_excluded`` (options spec §5 / R3): a non-zero holding of an
    instrument registered as an option. The settlement stores it per day (``options_open``)."""
    for h in book.holdings:
        if h.shares == _ZERO:
            continue
        inst = instruments.get(h.symbol)
        if inst is not None and inst.is_option:
            return True
    return False


def value_series(
    bundle: LedgerBundle,
    price_history: PriceHistory,
    fx_history: FxHistory,
    reporting: Currency,
    *,
    start: date,
    end: date,
    cash_movements: Sequence[CashMovementFlow],
    refused_dividends: Iterable[DividendIdentity],
    books: Iterable[tuple[date, Book]],
    options_open_before: bool,
) -> TrendSeries:
    """The trend over ``[start, end]`` from precomputed books (PERF spec §8.2).

    *books* is ``(day, book)`` ascending, each book the dashboard-path replay through its
    day — ``zip(days, iter_books(bundle, days))`` with ``days = trend_book_days(...)``; it is
    read lazily. *refused_dividends* is the whole bundle's replay refusals (the book a caller
    already holds: ``Book.refused_dividends``) — the payouts the flows leave out. A day before
    the trend's own first day is never valued, and ``available`` is decided over EVERY flow,
    whatever *start* is. Arithmetic, order and refusals are :func:`daily_value_series`'s —
    this is its precomputed-books form.

    *options_open_before* — REQUIRED, no default: whether any day from the trend's first day
    up to the day before the first valued day held an open option contract
    (:func:`holds_open_option` on that day's book; the store keeps it per day as
    ``options_open``). ``TrendSeries.options_value_excluded`` is defined over EVERY day of the
    trend (spec §4) while this function sees only the valued ones, so the caller states the
    rest and the flag is their OR — the series a later start returns carries the same flag as
    the full one. With *start* on or before the trend's first day there is no such day: pass
    ``False`` (``True`` is refused).
    """
    return daily_value_series(bundle, price_history, fx_history, reporting, end=end,
                              cash_movements=cash_movements, start=start, books=books,
                              refused_dividends=refused_dividends,
                              options_open_before=options_open_before)


def daily_value_series(
    bundle: LedgerBundle,
    price_history: PriceHistory,
    fx_history: FxHistory,
    reporting: Currency,
    *,
    end: date,
    cash_movements: Sequence[CashMovementFlow],
    start: date | None = None,
    books: Iterable[tuple[date, Book]] | None = None,
    refused_dividends: Iterable[DividendIdentity] | None = None,
    options_open_before: bool | None = None,
) -> TrendSeries:
    """Replay the ledgers day by day from the first event to ``end``.

    Returns ``available=False`` (empty points) when there are no ledger events, or
    when any flow date lacks an on-or-before FX rate for its needed pair.

    Raises :class:`~portfolio_dash.portfolio.cost_basis.UnbookableLedgerError` (never a raw
    ``ArithmeticError``) when a day cannot be valued at all — see :func:`build_reporting_flows`.

    The optional arguments are :func:`value_series`' (PERF spec §8.2). Left out, this is
    v0.1.31's function: the book rebuilt from ``bundle.through(day)`` on every day a row counts
    from, the refusals from one whole-bundle replay, every day from the first:

    * *start* — value only the days from ``max(start, trend_start(bundle))``; a point's values
      do not depend on it. A start after the trend's first day REQUIRES
      *options_open_before* (``TypeError`` without it — see :func:`value_series`), so the
      series flag stays the OR over every day;
    * *books* — ``(day, book)`` ascending, read instead of the per-day rebuild: a valued day
      reads the latest book on or before it, and a book missing for a day some row counts
      from is refused (``ValueError``) rather than reused stale;
    * *refused_dividends* — the whole bundle's replay refusals, instead of replaying for them.

    Two lookups are cheaper than before and give the same answers: FX by a moving index per
    pair (:class:`_FxCarry` — the days ascend), and ``net_invested`` from a running sum when
    :func:`_running_sum_is_exact` proves no partial sum rounds in ANY order — otherwise the
    reference per-day sum, recomputed only on a day some flow starts counting.
    """
    # R-1: every arithmetic site in this module refuses the same way. The operands HERE are a
    # stored PRICE and a share count (``price * h.shares``), not one ledger line, so the
    # sentence deliberately names NO row — pointing at the last row read would send the owner
    # to edit something the replay booked without complaint (the discipline
    # ``_uncomputable_message`` states). A fault raised by ``build_reporting_flows`` below is
    # ALREADY an ``UnbookableLedgerError`` (a ``ValueError``) and passes straight through this
    # arm with its row-naming sentence intact — a second wrap must not flatten it.
    try:
        first_day = trend_start(bundle)
        if first_day is None:
            return TrendSeries(points=[], reporting_currency=reporting, available=False)
        begin = first_day if start is None else max(start, first_day)
        if begin > first_day and options_open_before is None:
            raise TypeError("a trend starting after its first day needs options_open_before: "
                            "whether a day before the start held an open option contract")
        if begin == first_day and options_open_before:
            raise ValueError("options_open_before=True, but no day precedes the trend's "
                             "first day")

        # ONE ActionIndex for the whole replay (trap #21) — never one per day and never one per
        # lookup. Built from the FULL action list rather than per-day: `split_factor`'s window
        # is `(pd, day]`, so an action dated after `day` is excluded by the window itself and a
        # per-day rebuild would be the same answer at hundreds of times the cost.
        actions = ActionIndex.build(bundle.actions)

        # K2 (2026-10-05): the payouts the replay refused are not money put in or taken out.
        # One whole-bundle replay, taken here rather than from the caller so this function's
        # signature (and every caller's) is unchanged; it costs one replay beside the
        # per-day ones below. A refusal is decided from the events BEFORE the dividend, so the
        # whole bundle refuses exactly the rows each day's `bundle.through(day)` refuses.
        # (PERF: a caller holding the whole bundle's book passes its refusals instead.)
        reporting_flows = build_reporting_flows(
            bundle, fx_history, reporting, cash_movements=cash_movements,
            refused_dividends=(replay_refusals(bundle) if refused_dividends is None
                               else refused_dividends))
        if reporting_flows is None:
            return TrendSeries(points=[], reporting_currency=reporting, available=False)
        converted: list[tuple[date, Decimal]] = [(f.on, f.amount) for f in reporting_flows]
        net_invested_on = _NetInvested(converted)
        fx_carry = _FxCarry(fx_history, reporting)

        points: list[TrendPoint] = []
        # Options spec §5 / R3: an open contract has no daily price series (R2 values it at its
        # last trade, a point-in-time figure), so its value is left OUT of every day — without
        # marking the day incomplete — and the series says so once.
        # The days before the first valued one are the caller's to state (`value_series`).
        options_excluded = bool(options_open_before)
        # The book changes only on a day some row counts from (2026-10-13). `through(day)` is a
        # date-PREFIX cut, so between two such days it holds the same rows and `build_book`, a
        # pure function of the bundle, returns the same book: replay on those days and reuse
        # the book on the others. Measured on the owner's 6-year ledger: 2,181 replays → 461,
        # the dashboard 26.1 s → 7.7 s locally, payload byte-identical. Rebuilding every
        # calendar day was quadratic in the history length — invisible on the demo corpus,
        # minutes per page on the prod VM. The loop below only READS `book`; anything that
        # starts mutating it must rebuild every day instead.
        rebuild_on = bundle.counts_on_dates()
        given = None if books is None else _GivenBooks(books, rebuild_on)
        book: Book | None = None
        day = begin
        while day <= end:
            # allow_oversell (2026-07-02): an acked-oversold ledger must NEVER 500 the
            # dashboard through the trend replay either — mirror the main book's
            # degradation. An oversold (negative-share) day has no honest value, so it
            # marks the point incomplete instead of contributing a fabricated number.
            if given is not None:
                book = given.at(day)
            elif book is None or day in rebuild_on:
                book = build_book(bundle.through(day), allow_oversell=True)
            # The day's rate per quote currency, looked up once (PERF spec §8.2).
            rates: dict[Currency, Decimal | None] = {}
            total = _ZERO
            incomplete = False
            # Read the BOOK, and BEFORE the holdings loop, because that loop structurally
            # cannot see two of the three ways a corporate action goes unapplied: `_reject`
            # writes `unbookable_action` onto the SOURCE position, and the source is `None`
            # when it never existed (a missing buy) and a ZERO-share position when an earlier
            # action already emptied it (a duplicated EXCHANGE row) — and a zero-share holding
            # is dropped by `if h.shares == _ZERO: continue` two lines below, taking the flag
            # with it. Both refusals therefore reached the per-holding check with NOTHING
            # flagged, and the day was published `incomplete=False` over shares the replay
            # knowingly left in pre-action terms. Same blindness the XIRR gate had before
            # `dashboard.py` was moved onto this same book-level record (audit F-47 / F-49).
            #
            # DATE-SCOPED, not retroactive: `book` is rebuilt for each day from
            # `bundle.through(day)`, whose `actions` filter is `a.date <= day`, so a refusal
            # dated 6/3 cannot mark 6/2 — the days before the action stay complete.
            #
            # CONTAINMENT (D38 invariant 1): this branch is unreachable for a ledger with no
            # corporate actions. `unapplied_actions` is appended only by `_reject`, reachable
            # only from `_apply_action`, called only for an "action" event — which exists only
            # if `bundle.actions` is non-empty. So an action-free ledger takes the pre-existing
            # path exactly, with no equal-answer computation that could later drift.
            if book.unapplied_actions:
                incomplete = True  # 無法套用的公司行動 — 當日估值待釐清
            # CR-01 / CR-02 / CR-11 (2026-10-04): the same blindness, two more shapes. A 賣超
            # netted back to exactly zero shares is dropped by the holdings loop below, flag and
            # all, so the per-holding `h.oversold` test never saw it — and a dividend refused for
            # having no position has no holding at all. Both stay on the BOOK; read them here.
            # DATE-SCOPED the same way: `bundle.through(day)` only holds what has happened by
            # `day`, so the days before the oversell / the refused payout stay complete.
            # K3 (2026-10-05): the third shape — a payout refused on an open short or on a
            # ticker an EXCHANGE vacated flags its carrier, and a FLAT carrier (successor sold
            # out, short covered to 0) is dropped below with that flag. The held one was always
            # caught by `h.unbookable_dividend`; this makes the flat one agree with it.
            if book.oversold_positions or book.orphan_dividends or book.unbookable_dividends:
                incomplete = True  # 賣超 / 股利無對應持倉 / 股利無法入帳 — 當日估值待釐清
            # Options spec §4.4: a close row the replay refused is on the BOOK (it may have no
            # position at all), and B is withheld while one exists — date-scoped like the rest.
            if book.refused_closes:
                incomplete = True  # 選擇權平倉列無法入帳 — 當日估值待釐清
            for h in book.holdings:
                if h.shares == _ZERO:
                    continue
                inst = bundle.instruments.get(h.symbol)
                if inst is not None and inst.is_option:
                    options_excluded = True   # R3: excluded, NOT incomplete
                    continue
                # `unbookable_action` belongs here for a STRONGER reason than 賣超: a skipped
                # corporate action leaves `shares` in PRE-action terms while `price_history` is
                # global and already POST-action, so `price * shares` is not merely incomplete,
                # it is wrong by the action's whole ratio (a 3-for-1 understates the position
                # threefold). Omitting it let that product into the trend and net-worth series
                # as though valid, and unflagged.
                if h.oversold or h.unbookable_action or h.unbookable_close:
                    incomplete = True  # 賣超 / 股數待釐清 — value undefined, so not counted
                    continue
                # Y3 (owner ruling 2026-10-05): `unbookable_dividend` is NOT in that branch any
                # more. It means a payout was refused (an open short, a vacated ticker); the
                # SHARES are right and the price is global and current, so the value is real —
                # `Holding`'s taxonomy and `pnl.value_holdings` (which never suppresses it, so the
                # KPI total counts it) already said so. Dropping it here made this series the
                # one reader that disagreed: the trend's last point read 0 against a 530,000 KPI
                # total for the same day, and net worth fell by the position on the payout day
                # with no cash or share moving. The day is still marked — the payout is 待釐清,
                # and B is withheld on it — but the position is counted below like any other.
                if h.unbookable_dividend:
                    incomplete = True  # 股利無法入帳 — value kept (also on the book, K3 above)
                # A DECLARED short is NOT excluded (2026-07-31 ruling): it is a real priced
                # position and its NEGATIVE market value is a liability the series must carry.
                # Dropping it (the old `shares < 0` test) overstated both the trend and net worth
                # by the short's full market value while cash still held the proceeds — the two
                # halves of one trade counted asymmetrically.
                entry = _entry_at_or_before(price_history.get(h.symbol, []), day)
                if entry is None:
                    incomplete = True
                    continue
                # §5.1(d): a stored close is as traded on ITS OWN date, so a carried-forward
                # price meets a share count the replay has already re-denominated. Re-express
                # it into `day`'s terms — divide by the splits in `(pd, day]` — instead of
                # multiplying two mismatched units. The trend cannot precompute this the way
                # `dashboard.py` does: the factor depends on the valuation day, which varies
                # per point, so it is applied at lookup time.
                #
                # ⚠ NEVER mark the split day `incomplete` here (trap #8). This loop omits the
                # HOLDING, not the day, and still emits `total_value` below — so "flag it
                # instead" would drop the position's entire market value on every split date
                # (measured: a 95% net-worth cliff on a 1-for-20). An error of `ratio` replaced
                # by an error of 100% is not an improvement, and it breaks §2.1 outright.
                priced_on, as_traded = entry
                price = price_in(actions, h.symbol, as_traded,
                                 priced_on=priced_on, valued_on=day)
                # `_fx_at(fx_history, day, h.quote_ccy, reporting)`, by a moving index.
                if h.quote_ccy not in rates:
                    rates[h.quote_ccy] = fx_carry.rate(day, h.quote_ccy)
                rate = rates[h.quote_ccy]
                if rate is None:
                    incomplete = True
                    continue
                total += convert(price * h.shares, rate)
            points.append(TrendPoint(date=day, total_value=total,
                                     net_invested=net_invested_on(day), incomplete=incomplete))
            day += timedelta(days=1)

        return TrendSeries(points=points, reporting_currency=reporting, available=True,
                           options_value_excluded=options_excluded)
    except ArithmeticError as exc:
        raise UnbookableLedgerError(_uncomputable_message(None)) from exc
