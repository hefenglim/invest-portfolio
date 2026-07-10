"""Rule ③ 12-1 momentum: hand-computed returns + capped-linear score.

Small lookback/skip so the two price anchors are obvious. With lookback=4, skip=1 the
anchors are ``closes[-5]`` (base) and ``closes[-2]`` (recent).
"""

from decimal import Decimal

from portfolio_dash.strategy.rules import momentum as M
from portfolio_dash.strategy.rules.params import MomentumParams


def _s(vals: list[float]) -> list[Decimal]:
    return [Decimal(str(v)) for v in vals]


_P = MomentumParams(lookback_sessions=4, skip_sessions=1,
                    flat_epsilon=Decimal("0.005"), full_scale=Decimal("0.30"))


def test_positive_momentum_scaled_score() -> None:
    # base=closes[-5]=100, recent=closes[-2]=115 -> r=0.15 -> 0.15/0.30 = 0.5.
    rs = M.evaluate(_s([50, 100, 50, 50, 115, 50]), _P)
    assert rs is not None
    assert rs.state == "positive"
    assert rs.evidence["return_12_1"] == Decimal("0.15")
    assert rs.score == Decimal("0.5")
    assert rs.evidence["recent_offset_sessions"] == 1
    assert rs.evidence["base_offset_sessions"] == 4
    assert rs.window_days == 5


def test_negative_momentum() -> None:
    # recent=85, base=100 -> r=-0.15 -> score -0.5.
    rs = M.evaluate(_s([50, 100, 50, 50, 85, 50]), _P)
    assert rs is not None
    assert rs.state == "negative"
    assert rs.score == Decimal("-0.5")


def test_full_scale_caps_at_one() -> None:
    # r=0.60 -> min(1, 0.60/0.30)=1.
    rs = M.evaluate(_s([50, 100, 50, 50, 160, 50]), _P)
    assert rs is not None
    assert rs.evidence["return_12_1"] == Decimal("0.60")
    assert rs.score == Decimal("1")


def test_flat_within_epsilon() -> None:
    # r=0.003 (< 0.5% epsilon) -> flat label; score is the tiny uniform value 0.01.
    rs = M.evaluate(_s([50, 100, 50, 50, 100.3, 50]), _P)
    assert rs is not None
    assert rs.state == "flat"
    assert rs.score == Decimal("0.01")


def test_exactly_flat_zero_return() -> None:
    rs = M.evaluate(_s([50, 100, 50, 50, 100, 50]), _P)
    assert rs is not None
    assert rs.state == "flat"
    assert rs.score == Decimal("0")


def test_zero_base_returns_none() -> None:
    assert M.evaluate(_s([50, 0, 50, 50, 115, 50]), _P) is None


def test_insufficient_data_returns_none() -> None:
    assert M.evaluate(_s([100, 101, 102, 103]), _P) is None  # need lookback+1 = 5
    assert M.evaluate([], _P) is None
