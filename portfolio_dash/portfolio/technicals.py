"""Pure technical-indicator math: moving averages, volatility, drawdown, price-vs-cost.

This module is the calculation core for the price/technical variables consumed by
``llm_insight`` (spec 06a). It belongs in ``portfolio/`` (not ``llm_insight/``) because
the LLM never emits numbers of record: every figure here is computed by a pure, fully
unit-tested function and only *assembled* into a prompt downstream.

Discipline (``rules/data-and-pricing.md``):

* Everything is :class:`~decimal.Decimal` — prices, returns, ratios. Standard-deviation
  uses :meth:`Decimal.sqrt` (never ``math.sqrt``), so no float ever enters the chain.
* A ``closes`` series is ``list[Decimal]`` of closing prices in chronological order
  (oldest first, newest last). The last element is treated as the current price.
* Functions return ``None`` (not a fabricated value) when there are too few points to
  compute honestly — the caller renders that as missing/unavailable.
"""

from decimal import Decimal

_ZERO = Decimal("0")


def moving_average(closes: list[Decimal], window: int) -> Decimal | None:
    """Simple moving average of the last ``window`` closes.

    Returns ``None`` when fewer than ``window`` points are available (no padding,
    no partial-window guess).
    """
    if window <= 0 or len(closes) < window:
        return None
    last = closes[-window:]
    return sum(last, _ZERO) / Decimal(window)


def ma_signals(closes: list[Decimal]) -> dict[str, Decimal | None]:
    """Current price relative to the 20/60/120-day moving averages.

    Uses the last close as the current price. ``price_vs_maN = (price - maN) / maN``;
    each ``maN`` / ``price_vs_maN`` is ``None`` when fewer than N points exist.
    """
    out: dict[str, Decimal | None] = {}
    price = closes[-1] if closes else None
    for window in (20, 60, 120):
        ma = moving_average(closes, window)
        out[f"ma{window}"] = ma
        if ma is None or ma == _ZERO or price is None:
            out[f"price_vs_ma{window}"] = None
        else:
            out[f"price_vs_ma{window}"] = (price - ma) / ma
    return out


def _simple_returns(closes: list[Decimal]) -> list[Decimal]:
    """Period-over-period simple returns ``(c[i] - c[i-1]) / c[i-1]`` (skips zero bases)."""
    returns: list[Decimal] = []
    for prev, cur in zip(closes, closes[1:], strict=False):
        if prev == _ZERO:
            continue
        returns.append((cur - prev) / prev)
    return returns


def annualized_volatility(
    closes: list[Decimal], window: int = 30, periods: int = 252
) -> Decimal | None:
    """Annualized volatility = sample stdev of the last ``window`` daily returns × √periods.

    Needs ``window + 1`` price points (to form ``window`` returns); returns ``None``
    otherwise. Sample standard deviation divides by ``n - 1``. A constant series yields
    exactly ``Decimal("0")``.
    """
    if window < 1 or len(closes) < window + 1:
        return None
    returns = _simple_returns(closes[-(window + 1):])
    n = len(returns)
    if n < 2:
        return None
    mean = sum(returns, _ZERO) / Decimal(n)
    variance = sum(((r - mean) ** 2 for r in returns), _ZERO) / Decimal(n - 1)
    stdev = variance.sqrt()
    return stdev * Decimal(periods).sqrt()


def max_drawdown(closes: list[Decimal], window: int = 90) -> Decimal | None:
    """Most-negative peak-to-trough return over the last ``window`` closes (``<= 0``).

    Walks the (windowed) series tracking the running peak; the drawdown at each point
    is ``(close - peak) / peak``. Returns the minimum (most negative) such value, or
    ``Decimal("0")`` for a monotonically non-decreasing series. ``None`` when fewer
    than 2 points exist.
    """
    if len(closes) < 2:
        return None
    series = closes[-window:] if window > 0 else closes
    if len(series) < 2:
        return None
    peak = series[0]
    worst = _ZERO
    for close in series[1:]:
        if close > peak:
            peak = close
        if peak != _ZERO:
            drawdown = (close - peak) / peak
            if drawdown < worst:
                worst = drawdown
    return worst


def price_vs_cost(
    price: Decimal, original_avg: Decimal, adjusted_avg: Decimal
) -> dict[str, Decimal | None]:
    """Current price relative to the original and adjusted average cost.

    ``price_vs_X = (price - X) / X``, computed independently per denominator. A
    non-positive cost yields ``None`` for THAT ratio only (``domain-ledger.md`` allows
    ``adjusted_avg <= 0`` on high-yield payback — never floored), so the valid ratio is
    still surfaced rather than dropping both.
    """
    return {
        "price_vs_original": (price - original_avg) / original_avg if original_avg > 0 else None,
        "price_vs_adjusted": (price - adjusted_avg) / adjusted_avg if adjusted_avg > 0 else None,
    }
