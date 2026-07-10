"""Rule ④ RSI regime + 52-week position (context rule, halved magnitude).

Reuses the audited ``portfolio.technicals.rsi`` (all-gains -> 100, all-losses -> 0), so
overbought/oversold are reached with strictly rising / falling fixtures.
"""

from decimal import Decimal

from portfolio_dash.strategy.rules import rsi_regime as R
from portfolio_dash.strategy.rules.params import RsiRegimeParams


def _s(vals: list[float]) -> list[Decimal]:
    return [Decimal(str(v)) for v in vals]


_P = RsiRegimeParams()


def test_overbought_is_mild_bearish() -> None:
    up = _s([float(i) for i in range(1, 30)])  # strictly rising -> RSI 100
    rs = R.evaluate(up, _P)
    assert rs is not None
    assert rs.state == "overbought"
    assert rs.score == Decimal("-0.5")
    assert rs.evidence["rsi14"] == Decimal("100")
    # rising to a new high each day -> at the 52w high.
    assert rs.evidence["pct_from_52w_high"] == Decimal("0")


def test_oversold_is_mild_bullish() -> None:
    down = _s([float(i) for i in range(30, 1, -1)])  # strictly falling -> RSI 0
    rs = R.evaluate(down, _P)
    assert rs is not None
    assert rs.state == "oversold"
    assert rs.score == Decimal("0.5")
    assert rs.evidence["rsi14"] == Decimal("0")


def test_neutral_midrange() -> None:
    zig = _s([10, 11] * 15)  # symmetric zig-zag -> RSI near 50
    rs = R.evaluate(zig, _P)
    assert rs is not None
    assert rs.state == "neutral"
    assert rs.score == Decimal("0")
    assert Decimal("30") < rs.evidence["rsi14"] < Decimal("70")  # type: ignore[operator]


def test_insufficient_data_returns_none() -> None:
    assert R.evaluate(_s([1, 2, 3]), _P) is None  # < period+1
    assert R.evaluate([], _P) is None


def test_window_days_reports_actual_52w_window() -> None:
    closes = _s([100.0 + (i % 7) for i in range(20)])
    rs = R.evaluate(closes, _P)
    assert rs is not None
    assert rs.window_days == 20  # honest actual window, not padded to 252
    assert rs.evidence["week52_window_days"] == 20
