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

from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal

_ZERO = Decimal("0")


def _q(value: Decimal | None, exp: str) -> Decimal | None:
    """Quantize an LLM-facing display value (None-safe). Rounding is a display concern
    (data-and-pricing.md: quantize at display) — the raw computation stays full-precision.
    """
    return value.quantize(Decimal(exp), rounding=ROUND_HALF_UP) if value is not None else None


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


# --- Technical signals (batch ③, 2026-07-05 mini-spec) ------------------------
# Market-standard entry/exit-aid signals for the checkup card's 加碼/減碼 framework.
# All pure Decimal; each returns None (or a null sub-field) when the series is too short
# — never a fabricated value. Bundled by :func:`technical_signals` into one variable.


def rsi(closes: list[Decimal], period: int = 14) -> Decimal | None:
    """Wilder's Relative Strength Index over the whole series (0–100), or None.

    Needs ``period + 1`` closes (to form ``period`` changes). Seeds the average gain/loss
    with the first ``period`` changes, then applies Wilder's smoothing. A series with no
    losses yields 100 (all gains) / 50 (flat); no gains yields 0.
    """
    if period < 1 or len(closes) < period + 1:
        return None
    changes = [b - a for a, b in zip(closes, closes[1:], strict=False)]
    gains = [c if c > _ZERO else _ZERO for c in changes]
    losses = [-c if c < _ZERO else _ZERO for c in changes]
    avg_gain = sum(gains[:period], _ZERO) / Decimal(period)
    avg_loss = sum(losses[:period], _ZERO) / Decimal(period)
    for i in range(period, len(changes)):
        avg_gain = (avg_gain * Decimal(period - 1) + gains[i]) / Decimal(period)
        avg_loss = (avg_loss * Decimal(period - 1) + losses[i]) / Decimal(period)
    if avg_loss == _ZERO:
        return Decimal("100") if avg_gain > _ZERO else Decimal("50")
    rs = avg_gain / avg_loss
    return Decimal("100") - Decimal("100") / (Decimal("1") + rs)


def ma_cross(
    closes: list[Decimal], fast: int = 20, slow: int = 60, lookback: int = 90
) -> dict[str, object | None]:
    """Most recent fast/slow MA crossover within ``lookback`` bars + how many bars ago.

    ``cross`` is ``"golden"`` (fast crossed above slow) / ``"death"`` (below) / ``None``
    (no crossover in range). ``days_ago`` is the bar index of the newer sign of the
    flipped pair (0 = the crossover established at the latest close). Needs ``slow + 1``
    points to compare two consecutive MA pairs.
    """
    n = len(closes)
    if n < slow + 1:
        return {"cross": None, "days_ago": None}
    max_back = min(lookback, n - slow)
    signs: list[int] = []
    for k in range(max_back + 1):
        window = closes[: n - k] if k > 0 else closes
        fma = moving_average(window, fast)
        sma = moving_average(window, slow)
        if fma is None or sma is None:
            break
        signs.append(1 if fma >= sma else -1)
    for i in range(len(signs) - 1):
        if signs[i] != signs[i + 1]:
            return {"cross": "golden" if signs[i] > 0 else "death", "days_ago": i}
    return {"cross": None, "days_ago": None}


def week52_position(closes: list[Decimal], window: int = 252) -> dict[str, Decimal | int | None]:
    """Current price relative to the trailing 52-week (``window``-bar) high and low.

    ``pct_from_high`` (``<= 0``) and ``pct_from_low`` (``>= 0``) are ``(price - X) / X``.
    ``window_days`` reports the ACTUAL window used, so an under-a-year listing is honest
    (the position is computed over whatever history exists, not padded). Empty → nulls.
    """
    if not closes:
        return {"high": None, "low": None, "pct_from_high": None,
                "pct_from_low": None, "window_days": 0}
    series = closes[-window:] if window > 0 else closes
    hi, lo, price = max(series), min(series), closes[-1]
    return {
        "high": hi,
        "low": lo,
        "pct_from_high": (price - hi) / hi if hi > _ZERO else None,
        "pct_from_low": (price - lo) / lo if lo > _ZERO else None,
        "window_days": len(series),
    }


def trend_structure(closes: list[Decimal], window: int = 60) -> dict[str, object | None]:
    """Swing structure over the last ``window`` bars: uptrend / downtrend / range.

    Splits the window in half and compares the two halves' highs and lows:
    higher-high & higher-low → ``"uptrend"`` (HH-HL); lower-high & lower-low →
    ``"downtrend"`` (LH-LL); otherwise ``"range"``. Needs ≥ 4 bars.
    """
    series = closes[-window:] if window > 0 else closes
    if len(series) < 4:
        return {"structure": None, "window_days": len(series)}
    mid = len(series) // 2
    first, second = series[:mid], series[mid:]
    higher_high, higher_low = max(second) > max(first), min(second) > min(first)
    lower_high, lower_low = max(second) < max(first), min(second) < min(first)
    if higher_high and higher_low:
        structure = "uptrend"
    elif lower_high and lower_low:
        structure = "downtrend"
    else:
        structure = "range"
    return {"structure": structure, "window_days": len(series)}


def volume_signal(
    volumes: Sequence[Decimal | None], window: int = 20
) -> dict[str, object | None]:
    """Latest volume vs its ``window``-bar average + a surge flag (≥ 2× average).

    Probe-gated at the call site: only invoked when the provider actually backfilled
    volume (P1-①②). Needs ``window + 1`` bars. ``None`` entries mark sessions whose
    stored row carried no volume; a ``None`` inside the needed recent window degrades
    to the insufficient-data result instead of raising.
    """
    if len(volumes) < window + 1:
        return {"ratio_to_avg": None, "surge": None}
    recent = [v for v in volumes[-(window + 1):] if v is not None]
    if len(recent) < window + 1:
        return {"ratio_to_avg": None, "surge": None}
    avg = sum(recent[:-1], _ZERO) / Decimal(window)
    latest = recent[-1]
    if avg == _ZERO:
        return {"ratio_to_avg": None, "surge": None}
    ratio = latest / avg
    return {"ratio_to_avg": ratio, "surge": ratio >= Decimal("2")}


def technical_signals(
    closes: list[Decimal], volumes: Sequence[Decimal | None] | None = None
) -> dict[str, object]:
    """The one integrated technical-signal variable (mini-spec: one block, not five vars).

    Bundles RSI(14), the 20/60 MA crossover, 52-week position, and swing structure over
    ``closes``; adds a volume section only when ``volumes`` is fed (probe-gated). Empty
    series → ``{"unavailable": True}`` so the card renders it honestly.
    """
    if not closes:
        return {"unavailable": True}
    # Quantize the display-facing ratios so the prompt carries clean values (RSI 1 dp,
    # position ratios 4 dp) instead of 26-digit Decimal noise — cleaner + fewer tokens.
    w52 = week52_position(closes)
    w52_display: dict[str, object | None] = {
        "high": w52["high"],
        "low": w52["low"],
        "pct_from_high": _q(_as_decimal(w52["pct_from_high"]), "0.0001"),
        "pct_from_low": _q(_as_decimal(w52["pct_from_low"]), "0.0001"),
        "window_days": w52["window_days"],
    }
    out: dict[str, object] = {
        "rsi14": _q(rsi(closes, 14), "0.1"),
        "ma_cross": ma_cross(closes),
        "week52": w52_display,
        "trend": trend_structure(closes),
    }
    if volumes and any(v is not None for v in volumes):
        vol = volume_signal(volumes)
        vol["ratio_to_avg"] = _q(_as_decimal(vol["ratio_to_avg"]), "0.01")
        out["volume"] = vol
    return out


def _as_decimal(value: object) -> Decimal | None:
    """Narrow a bundled signal field back to Decimal|None for quantization (mypy-safe)."""
    return value if isinstance(value, Decimal) else None


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
