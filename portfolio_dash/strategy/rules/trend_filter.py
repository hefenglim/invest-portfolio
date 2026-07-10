"""Rule ①: MA(200) trend filter with a hysteresis band + N-day confirmation.

Price vs the 200-day simple moving average, the classic Faber/Zakamulin risk-control
filter. A raw ``±band`` zone (default ±2%) plus a ``confirm_days`` requirement
suppresses whipsaw: the state only reads *confirmed* once the raw zone has held for the
required number of consecutive sessions.

Pure Decimal; too few closes → ``None`` (never a fabricated verdict).
"""

from decimal import Decimal

from portfolio_dash.strategy.rules.params import TrendFilterParams
from portfolio_dash.strategy.rules.types import RuleState

_ZERO = Decimal("0")
_ONE = Decimal("1")


def _ma_series(closes: list[Decimal], window: int) -> list[Decimal]:
    """Rolling simple moving average at every session from ``window-1`` to the end.

    ``result[j]`` is the SMA ending at ``closes[window-1+j]`` — O(n) via a running sum.
    """
    running = sum(closes[:window], _ZERO)
    out = [running / Decimal(window)]
    for i in range(window, len(closes)):
        running += closes[i] - closes[i - window]
        out.append(running / Decimal(window))
    return out


def _zone(close: Decimal, ma: Decimal, band: Decimal) -> str:
    """Raw hysteresis zone of ``close`` relative to ``ma``: above / below / in_band."""
    upper = ma * (_ONE + band)
    lower = ma * (_ONE - band)
    if close > upper:
        return "above"
    if close < lower:
        return "below"
    return "in_band"


def evaluate(closes: list[Decimal], params: TrendFilterParams) -> RuleState | None:
    """Price-vs-MA(``params.ma``) trend state with band + confirmation.

    ``days_in_zone`` = consecutive sessions (ending at the latest close) whose raw zone
    equals the current raw zone; ``confirmed`` = ``days_in_zone >= confirm_days``.

    State: ``above_confirmed`` / ``below_confirmed`` (score ±1), or the neutral
    ``above_unconfirmed`` / ``below_unconfirmed`` / ``in_band`` (score 0). Needs at
    least ``params.ma`` closes.
    """
    window = params.ma
    if window <= 0 or len(closes) < window:
        return None

    ma_series = _ma_series(closes, window)
    # Zones aligned to ma_series: zones[j] is the zone of closes[window-1+j].
    zones = [
        _zone(closes[window - 1 + j], ma_series[j], params.band)
        for j in range(len(ma_series))
    ]
    current_zone = zones[-1]

    days_in_zone = 0
    for zone in reversed(zones):
        if zone == current_zone:
            days_in_zone += 1
        else:
            break

    confirmed = days_in_zone >= params.confirm_days

    if current_zone == "above":
        state = "above_confirmed" if confirmed else "above_unconfirmed"
        score = _ONE if confirmed else _ZERO
    elif current_zone == "below":
        state = "below_confirmed" if confirmed else "below_unconfirmed"
        score = -_ONE if confirmed else _ZERO
    else:  # in_band
        state = "in_band"
        score = _ZERO

    ma_last = ma_series[-1]
    price = closes[-1]
    price_vs_ma = (price - ma_last) / ma_last if ma_last != _ZERO else None

    evidence: dict[str, object] = {
        "ma200": ma_last,
        "price": price,
        "price_vs_ma": price_vs_ma,
        "band": params.band,
        "zone": current_zone,
        "days_in_zone": days_in_zone,
        "confirm_days": params.confirm_days,
        "confirmed": confirmed,
    }
    return RuleState(state=state, score=score, evidence=evidence, window_days=window)
