"""Rule ① MA(200) trend filter: hand-checked zones, confirmation, hysteresis.

All fixtures use a small ``ma`` so the moving average and zones are computable by hand;
no float anywhere.
"""

from decimal import Decimal

from portfolio_dash.strategy.rules import trend_filter as TF
from portfolio_dash.strategy.rules.params import TrendFilterParams


def _s(vals: list[float]) -> list[Decimal]:
    return [Decimal(str(v)) for v in vals]


_P3 = TrendFilterParams(ma=3, band=Decimal("0.02"), confirm_days=2)


def test_above_confirmed_known_answer() -> None:
    # MA3 at last = (100+110+111)/3 = 107; 111 > 107*1.02 -> above.
    # Prior session MA3 = (100+100+110)/3 = 103.33; 110 > 105.4 -> above too.
    # days_in_zone = 2 == confirm_days -> confirmed.
    rs = TF.evaluate(_s([100, 100, 100, 110, 111]), _P3)
    assert rs is not None
    assert rs.state == "above_confirmed"
    assert rs.score == Decimal("1")
    assert rs.evidence["days_in_zone"] == 2
    assert rs.evidence["confirmed"] is True
    assert rs.evidence["ma200"] == (Decimal("100") + Decimal("110") + Decimal("111")) / 3
    assert rs.window_days == 3


def test_above_unconfirmed_scores_zero() -> None:
    # Only the last session is above (days_in_zone = 1 < 2) -> neutral score.
    rs = TF.evaluate(_s([100, 100, 100, 100, 110]), _P3)
    assert rs is not None
    assert rs.state == "above_unconfirmed"
    assert rs.score == Decimal("0")
    assert rs.evidence["days_in_zone"] == 1


def test_below_confirmed_known_answer() -> None:
    rs = TF.evaluate(_s([100, 100, 100, 90, 89]), _P3)
    assert rs is not None
    assert rs.state == "below_confirmed"
    assert rs.score == Decimal("-1")
    assert rs.evidence["days_in_zone"] == 2


def test_in_band_is_neutral() -> None:
    # 100.5 vs MA 100 is +0.5% < 2% band -> in_band, score 0.
    rs = TF.evaluate(_s([100, 100, 100, 100, 100.5]), _P3)
    assert rs is not None
    assert rs.state == "in_band"
    assert rs.score == Decimal("0")


def test_insufficient_data_returns_none() -> None:
    assert TF.evaluate(_s([100, 100]), _P3) is None  # < ma
    assert TF.evaluate([], _P3) is None


def test_all_equal_series_is_in_band() -> None:
    rs = TF.evaluate(_s([50] * 20), _P3)
    assert rs is not None and rs.state == "in_band" and rs.score == Decimal("0")


def _whipsaw_state_changes(band: str, confirm: int) -> int:
    # 200 flat sessions then a ±1.5% sawtooth around ~100 (inside a 2% band).
    closes = _s([100.0] * 200 + [101.5 if i % 2 == 0 else 98.5 for i in range(60)])
    params = TrendFilterParams(ma=200, band=Decimal(band), confirm_days=confirm)
    states: list[str] = []
    for end in range(201, len(closes) + 1):
        rs = TF.evaluate(closes[:end], params)
        assert rs is not None
        states.append(rs.state)
    return sum(1 for a, b in zip(states, states[1:], strict=False) if a != b)


def test_hysteresis_band_suppresses_whipsaw() -> None:
    # The ±2% band swallows the ±1.5% sawtooth (0 flips); without a band every session
    # flips above/below. The band demonstrably reduces state changes.
    with_band = _whipsaw_state_changes("0.02", 2)
    without_band = _whipsaw_state_changes("0", 1)
    assert with_band == 0
    assert without_band > 0
    assert with_band < without_band
