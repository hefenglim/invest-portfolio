"""R3/⑩ counter-evidence: the ±0.5% flat band made 「持平」 almost impossible to hit.

Over a 14-day horizon at 30% annualised volatility the one-sigma move is ~7%, so a fixed
±0.5% band gives ``direction=flat`` roughly a 6% chance of being scored a hit while ``up`` and
``down`` sit near 51% each. The scoreboard was therefore training the assistant NOT to say
"I do not expect this to move" — and ``price_change`` is the only metric the official cards
emit, so that bias applied to almost every scored prediction.

AI-D25 diagnosed exactly this for the volatility metric and widened its band to ±5%; the same
reasoning was never applied to ``price_change``. The band is now scaled to the symbol's own
volatility over the actual horizon, so the three directions carry comparable base rates on a
calm stock and a wild one alike.
"""

from decimal import Decimal
from typing import Literal

from portfolio_dash.llm_insight import scoring
from portfolio_dash.llm_insight.cards import Prediction

D = Decimal


def _pred(direction: Literal["up", "down", "flat"]) -> Prediction:
    return Prediction(metric="price_change", direction=direction, horizon_days=14)


def _m(move: str, band: str | None = None) -> scoring.ActualMeasurement:
    return scoring.ActualMeasurement(
        price_change_pct=D(move),
        flat_band=None if band is None else D(band))


def test_a_flat_call_survives_a_move_well_inside_the_symbols_own_noise() -> None:
    """A 2% drift on a stock whose 14-day sigma is ~7% is not a directional move."""
    assert scoring.score_quant(_pred("flat"), _m("0.02", band="0.0354")) is True


def test_a_flat_call_still_fails_once_the_move_leaves_that_noise() -> None:
    """The band widens the target, it does not remove it."""
    assert scoring.score_quant(_pred("flat"), _m("0.09", band="0.0354")) is False


def test_the_fixed_band_remains_the_floor_for_a_very_calm_symbol() -> None:
    """A scaled band must never end up TIGHTER than the old fixed one — that would make the
    bias worse for exactly the symbols where 「持平」 is the honest call."""
    assert scoring.score_quant(_pred("flat"), _m("0.004", band="0.005")) is True


def test_a_measurement_with_no_band_falls_back_to_the_fixed_one() -> None:
    """Legacy rows and any seam that cannot compute a band keep the documented behaviour —
    never a silently different scoring rule for the same stored prediction."""
    assert scoring.score_quant(_pred("flat"), _m("0.004")) is True
    assert scoring.score_quant(_pred("flat"), _m("0.02")) is False


def test_the_band_never_touches_a_directional_call() -> None:
    """up/down are decided by SIGN. Widening the flat band must not create a dead zone that
    turns a genuine small rise into a miss."""
    assert scoring.score_quant(_pred("up"), _m("0.001", band="0.0354")) is True
    assert scoring.score_quant(_pred("down"), _m("-0.001", band="0.0354")) is True
