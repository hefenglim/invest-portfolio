"""Daily replay trend: market value + cumulative net invested per day.

Pure function over in-memory inputs (no DB handle): the combiner bulk-loads price/FX
history once and passes it in. Valuation uses the carry-forward convention (latest
stored value on-or-before the day); a day a held symbol has no price at all is
flagged ``incomplete`` (never guessed). Any ledger flow whose date has no
on-or-before FX makes the whole series unavailable (consistent with the XIRR rule).
"""

from bisect import bisect_right
from datetime import date, timedelta
from decimal import Decimal

from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.portfolio.dashboard_models import TrendPoint, TrendSeries
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.fx import convert
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import Dividend, OpeningInventory, Transaction

_ZERO = Decimal("0")
_ONE = Decimal("1")

# Ascending (date, value) series, as bulk-loaded by the combiner.
PriceHistory = dict[str, list[tuple[date, Decimal]]]
FxHistory = dict[tuple[Currency, Currency], list[tuple[date, Decimal]]]


def _at_or_before(series: list[tuple[date, Decimal]], on: date) -> Decimal | None:
    """Latest value at-or-before ``on`` over an ascending series, else None."""
    idx = bisect_right(series, on, key=lambda item: item[0])
    if idx == 0:
        return None
    return series[idx - 1][1]


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
    transactions: list[Transaction],
    dividends: list[Dividend],
    opening: list[OpeningInventory],
    instruments: dict[str, Instrument],
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
        [t.trade_date for t in transactions]
        + [d.date for d in dividends]
        + [o.build_date for o in opening]
    )
    if not event_dates:
        return TrendSeries(points=[], reporting_currency=reporting, available=False)
    start = min(event_dates)

    def quote_ccy(symbol: str) -> Currency:
        inst = instruments.get(symbol)
        if inst is None:
            raise KeyError(f"unknown instrument: {symbol}")
        return inst.quote_ccy

    # Net-invested flow deltas (signs mirror the XIRR conventions, negated):
    # opening +cost, buy +gross(incl. fees+tax), sell -net, cash dividend -net.
    flows: list[tuple[date, Currency, Decimal]] = []
    for o in opening:
        flows.append((o.build_date, quote_ccy(o.symbol), o.original_cost_total))
    for t in transactions:
        gross = t.quantity * t.price
        if t.side is Side.BUY:
            flows.append((t.trade_date, quote_ccy(t.symbol), gross + t.fees + t.tax))
        else:
            flows.append((t.trade_date, quote_ccy(t.symbol), -(gross - t.fees - t.tax)))
    for dv in dividends:
        if dv.type is DividendType.CASH:
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
        book = build_book(
            [t for t in transactions if t.trade_date <= day],
            [d for d in dividends if d.date <= day],
            [o for o in opening if o.build_date <= day],
            instruments,
            allow_oversell=True,
        )
        total = _ZERO
        incomplete = False
        for h in book.holdings:
            if h.shares == _ZERO:
                continue
            if h.shares < _ZERO:
                incomplete = True  # 賣超 day — value undefined (待釐清)
                continue
            price = _at_or_before(price_history.get(h.symbol, []), day)
            if price is None:
                incomplete = True
                continue
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
