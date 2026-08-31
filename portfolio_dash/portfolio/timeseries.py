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
both sides), and ``api/routers/ledgers.py::transactions`` (its own ``t.quantity * t.price``).
They are named here because a guard that stops at a module boundary is exactly the shape of
defect this one was: fixed once, believed general.
"""

from bisect import bisect_right
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal

from portfolio_dash.portfolio.benchmark_counterfactual import ReportingFlow
from portfolio_dash.portfolio.cost_basis import (
    UnbookableLedgerError,
    _uncomputable_message,
    build_book,
)
from portfolio_dash.portfolio.dashboard_models import TrendPoint, TrendSeries
from portfolio_dash.portfolio.price_basis import price_in

# ONE definition of both, shared with the metric that already takes these kinds: an oracle
# of B that re-derived either would be free to disagree with XIRR about which flows exist.
from portfolio_dash.portfolio.returns import XIRR_CASH_KINDS, CashMovementFlow
from portfolio_dash.shared.cash_kinds import movement_sign
from portfolio_dash.shared.corporate_actions import ActionIndex
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.fx import convert
from portfolio_dash.shared.models.enums import CASH_DIVIDEND_TYPES, Side
from portfolio_dash.shared.models.ledger import LedgerBundle

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


def build_reporting_flows(
    bundle: LedgerBundle,
    fx_history: FxHistory,
    reporting: Currency,
    *,
    cash_movements: Sequence[CashMovementFlow],
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
    route it to that market's benchmark; the trend simply ignores that field.

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
    """
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
            gross = t.quantity * t.price
            if t.side is Side.BUY:
                raw.append((t.trade_date, t.symbol, gross + t.fees + t.tax, t))
            else:
                raw.append((t.trade_date, t.symbol, -(gross - t.fees - t.tax), t))
        for dv in bundle.dividends:
            if dv.type in CASH_DIVIDEND_TYPES:  # CASH (TW) + NET (MY) — one definition
                booking = dv
                raw.append((dv.date, dv.symbol, -dv.net, dv))

        out: list[ReportingFlow] = []
        for d, symbol, amount, source in raw:
            booking = source
            rate = _fx_at(fx_history, d, quote_ccy(symbol), reporting)
            if rate is None:
                return None
            out.append(ReportingFlow(d, market_of(symbol), convert(amount, rate), symbol))
        # AI-D48. Each movement carries its OWN currency (there is no symbol to look one up
        # from) and its sign comes from the shared table, so a kind can never be a credit here
        # and a debit in the pool balance.
        for mv in cash_movements:
            booking = mv
            if mv.kind.upper() not in XIRR_CASH_KINDS:
                continue
            rate = _fx_at(fx_history, mv.date, mv.ccy, reporting)
            if rate is None:
                return None
            out.append(ReportingFlow(
                mv.date, None, convert(-movement_sign(mv.kind) * mv.amount, rate), ""))
        return out
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
            if mv.kind.upper() not in XIRR_CASH_KINDS:
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


def daily_value_series(
    bundle: LedgerBundle,
    price_history: PriceHistory,
    fx_history: FxHistory,
    reporting: Currency,
    *,
    end: date,
    cash_movements: Sequence[CashMovementFlow],
) -> TrendSeries:
    """Replay the ledgers day by day from the first event to ``end``.

    Returns ``available=False`` (empty points) when there are no ledger events, or
    when any flow date lacks an on-or-before FX rate for its needed pair.

    Raises :class:`~portfolio_dash.portfolio.cost_basis.UnbookableLedgerError` (never a raw
    ``ArithmeticError``) when a day cannot be valued at all — see :func:`build_reporting_flows`.
    """
    # R-1: every arithmetic site in this module refuses the same way. The operands HERE are a
    # stored PRICE and a share count (``price * h.shares``), not one ledger line, so the
    # sentence deliberately names NO row — pointing at the last row read would send the owner
    # to edit something the replay booked without complaint (the discipline
    # ``_uncomputable_message`` states). A fault raised by ``build_reporting_flows`` below is
    # ALREADY an ``UnbookableLedgerError`` (a ``ValueError``) and passes straight through this
    # arm with its row-naming sentence intact — a second wrap must not flatten it.
    try:
        # R6: `effective_date`, matching `LedgerBundle.through` and `build_book`'s ordering.
        # Latent rather than live — a 配股's ex-date cannot precede the position that earns it,
        # so no real ledger starts on one — but three date filters spelling the rule three ways
        # is how two of them drift, and this was the third.
        event_dates = (
            [t.trade_date for t in bundle.transactions]
            + [d.effective_date for d in bundle.dividends]
            + [o.build_date for o in bundle.opening]
        )
        if not event_dates:
            return TrendSeries(points=[], reporting_currency=reporting, available=False)
        start = min(event_dates)

        # ONE ActionIndex for the whole replay (trap #21) — never one per day and never one per
        # lookup. Built from the FULL action list rather than per-day: `split_factor`'s window
        # is `(pd, day]`, so an action dated after `day` is excluded by the window itself and a
        # per-day rebuild would be the same answer at hundreds of times the cost.
        actions = ActionIndex.build(bundle.actions)

        reporting_flows = build_reporting_flows(bundle, fx_history, reporting,
                                                cash_movements=cash_movements)
        if reporting_flows is None:
            return TrendSeries(points=[], reporting_currency=reporting, available=False)
        converted: list[tuple[date, Decimal]] = [(f.on, f.amount) for f in reporting_flows]

        points: list[TrendPoint] = []
        day = start
        while day <= end:
            # allow_oversell (2026-07-02): an acked-oversold ledger must NEVER 500 the
            # dashboard through the trend replay either — mirror the main book's
            # degradation. An oversold (negative-share) day has no honest value, so it
            # marks the point incomplete instead of contributing a fabricated number.
            book = build_book(bundle.through(day), allow_oversell=True)
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
            for h in book.holdings:
                if h.shares == _ZERO:
                    continue
                # `unbookable_action` belongs here for a STRONGER reason than the other two:
                # a skipped corporate action leaves `shares` in PRE-action terms while
                # `price_history` is global and already POST-action, so `price * shares` is
                # not merely incomplete, it is wrong by the action's whole ratio (a 3-for-1
                # understates the position threefold). Omitting it let that product into the
                # trend and net-worth series as though valid, and unflagged.
                if h.oversold or h.unbookable_dividend or h.unbookable_action:
                    incomplete = True  # 賣超 / 待釐清 day — value undefined
                    continue
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
                rate = _fx_at(fx_history, day, h.quote_ccy, reporting)
                if rate is None:
                    incomplete = True
                    continue
                total += convert(price * h.shares, rate)
            net_invested = _ZERO
            for d, amt in converted:
                if d <= day:
                    net_invested += amt
            points.append(TrendPoint(date=day, total_value=total,
                                     net_invested=net_invested, incomplete=incomplete))
            day += timedelta(days=1)

        return TrendSeries(points=points, reporting_currency=reporting, available=True)
    except ArithmeticError as exc:
        raise UnbookableLedgerError(_uncomputable_message(None)) from exc
