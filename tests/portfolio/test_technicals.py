"""Unit tests for portfolio.technicals — pure Decimal MA / volatility / drawdown / vs-cost.

All fixtures are hand-checkable; no float anywhere. These functions are the only place
the MA / volatility / drawdown / price-vs-cost numbers are computed (the calc core); the
llm_insight layer merely assembles their results (it computes no numbers of record).
"""

from decimal import Decimal

from portfolio_dash.portfolio import technicals as T


def test_moving_average_and_none() -> None:
    assert T.moving_average([Decimal("10"), Decimal("20"), Decimal("30")], 3) == Decimal("20")
    # last `window` only: [20, 30, 40] -> 30
    assert T.moving_average(
        [Decimal("10"), Decimal("20"), Decimal("30"), Decimal("40")], 3
    ) == Decimal("30")
    assert T.moving_average([Decimal("10")], 3) is None
    assert T.moving_average([], 3) is None


def test_ma_signals_price_vs() -> None:
    closes = [Decimal(str(x)) for x in range(1, 21)]  # 1..20, last=20, ma20=10.5
    s = T.ma_signals(closes)
    assert s["ma20"] == Decimal("10.5")
    assert s["price_vs_ma20"] == (Decimal("20") - Decimal("10.5")) / Decimal("10.5")
    # not enough points for 60/120
    assert s["ma60"] is None and s["price_vs_ma60"] is None
    assert s["ma120"] is None and s["price_vs_ma120"] is None


def test_ma_signals_empty() -> None:
    s = T.ma_signals([])
    assert s["ma20"] is None and s["price_vs_ma20"] is None


def test_volatility_constant_series_zero() -> None:
    assert T.annualized_volatility([Decimal("100")] * 40) == Decimal("0")


def test_volatility_none_when_too_few() -> None:
    # default window=30 needs window+1 = 31 points
    assert T.annualized_volatility([Decimal("100")] * 30) is None


def test_volatility_hand_checked() -> None:
    # window=2, periods=4. closes 100, 110, 99 -> returns: +0.1, -0.1
    # sample stdev (n-1=1): mean 0; variance = (0.1^2 + 0.1^2)/1 = 0.02; sd = sqrt(0.02)
    # annualized = sd * sqrt(4) = sqrt(0.02) * 2
    got = T.annualized_volatility(
        [Decimal("100"), Decimal("110"), Decimal("99")], window=2, periods=4
    )
    assert got is not None
    expected = (Decimal("0.02").sqrt()) * Decimal("4").sqrt()
    assert got == expected


def test_max_drawdown_simple() -> None:
    # 100 -> 120 -> 90 : trough 90 vs running peak 120 = -0.25
    assert T.max_drawdown([Decimal("100"), Decimal("120"), Decimal("90")]) == Decimal("-0.25")


def test_max_drawdown_monotonic_up_is_zero() -> None:
    assert T.max_drawdown([Decimal("100"), Decimal("110"), Decimal("120")]) == Decimal("0")


def test_max_drawdown_none_when_too_few() -> None:
    assert T.max_drawdown([Decimal("100")]) is None
    assert T.max_drawdown([]) is None


def test_max_drawdown_window_limits_lookback() -> None:
    # full series has a deep early drawdown; window=2 only sees the last two points.
    closes = [Decimal("100"), Decimal("50"), Decimal("100"), Decimal("99")]
    assert T.max_drawdown(closes, window=2) == Decimal("-0.01")


def test_price_vs_cost() -> None:
    r = T.price_vs_cost(Decimal("612.5"), Decimal("500"), Decimal("495"))
    assert r["price_vs_original"] == (Decimal("612.5") - Decimal("500")) / Decimal("500")
    assert r["price_vs_adjusted"] == (Decimal("612.5") - Decimal("495")) / Decimal("495")


def test_price_vs_cost_nonpositive_adjusted_keeps_original() -> None:
    # domain-ledger allows adjusted_avg <= 0 (high-yield payback) — surface original, None adjusted.
    r = T.price_vs_cost(Decimal("100"), Decimal("80"), Decimal("0"))
    assert r["price_vs_original"] == (Decimal("100") - Decimal("80")) / Decimal("80")
    assert r["price_vs_adjusted"] is None
