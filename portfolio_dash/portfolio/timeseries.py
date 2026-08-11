"""Daily replay trend: market value + cumulative net invested per day.

Pure function over in-memory inputs (no DB handle): the combiner bulk-loads price/FX
history once and passes it in. Valuation uses the carry-forward convention (latest
stored value on-or-before the day); a day a held symbol has no price at all — or on
which the replay could not apply a corporate action — is flagged ``incomplete``
(never guessed). Any ledger flow whose date has no on-or-before FX makes the whole
series unavailable (consistent with the XIRR rule).
"""

from bisect import bisect_right
from datetime import date, timedelta
from decimal import Decimal

from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.portfolio.dashboard_models import TrendPoint, TrendSeries
from portfolio_dash.portfolio.price_basis import price_in
from portfolio_dash.shared.corporate_actions import ActionIndex
from portfolio_dash.shared.enums import Currency
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


def daily_value_series(
    bundle: LedgerBundle,
    price_history: PriceHistory,
    fx_history: FxHistory,
    reporting: Currency,
    *,
    end: date,
) -> TrendSeries:
    """Replay the ledgers day by day from the first event to ``end``.

    Returns ``available=False`` (empty points) when there are no ledger events, or
    when any flow date lacks an on-or-before FX rate for its needed pair.
    """
    event_dates = (
        [t.trade_date for t in bundle.transactions]
        + [d.date for d in bundle.dividends]
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

    def quote_ccy(symbol: str) -> Currency:
        inst = bundle.instruments.get(symbol)
        if inst is None:
            raise KeyError(f"unknown instrument: {symbol}")
        return inst.quote_ccy

    # Net-invested flow deltas (signs mirror the XIRR conventions, negated):
    # opening +cost, buy +gross(incl. fees+tax), sell -net, cash dividend -net.
    flows: list[tuple[date, Currency, Decimal]] = []
    for o in bundle.opening:
        flows.append((o.build_date, quote_ccy(o.symbol), o.original_cost_total))
    for t in bundle.transactions:
        gross = t.quantity * t.price
        if t.side is Side.BUY:
            flows.append((t.trade_date, quote_ccy(t.symbol), gross + t.fees + t.tax))
        else:
            flows.append((t.trade_date, quote_ccy(t.symbol), -(gross - t.fees - t.tax)))
    for dv in bundle.dividends:
        if dv.type in CASH_DIVIDEND_TYPES:  # CASH (TW) + NET (MY) — one definition
            flows.append((dv.date, quote_ccy(dv.symbol), -dv.net))

    # Convert each flow at its own date's carry-forward FX; bail honestly if any
    # flow cannot be converted (no on-or-before rate).
    converted: list[tuple[date, Decimal]] = []
    for d, ccy, amount in flows:
        rate = _fx_at(fx_history, d, ccy, reporting)
        if rate is None:
            return TrendSeries(points=[], reporting_currency=reporting, available=False)
        converted.append((d, convert(amount, rate)))

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
