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


# --- batch ③ technical signals (2026-07-05) -----------------------------------


def _dec_series(vals: list[float]) -> list[Decimal]:
    return [Decimal(str(v)) for v in vals]


def test_rsi_all_gains_is_100_all_losses_is_0() -> None:
    up = _dec_series([float(i) for i in range(1, 20)])       # strictly rising
    down = _dec_series([float(i) for i in range(20, 1, -1)])  # strictly falling
    assert T.rsi(up, 14) == Decimal("100")
    assert T.rsi(down, 14) == Decimal("0")


def test_rsi_none_when_too_short_and_midrange_when_balanced() -> None:
    assert T.rsi(_dec_series([1, 2, 3]), 14) is None
    # a symmetric zig-zag hovers near 50
    zig = _dec_series([10, 11, 10, 11, 10, 11, 10, 11, 10, 11, 10, 11, 10, 11, 10])
    r = T.rsi(zig, 14)
    assert r is not None and Decimal("30") < r < Decimal("70")


def test_ma_cross_detects_golden_and_days_ago() -> None:
    # A long decline (MA20 well BELOW MA60) then a sharp rise that lifts MA20 back above
    # MA60 -> a genuine golden cross landing within the detectable window.
    decline = [200.0 - 1.0 * i for i in range(80)]   # 200 -> 121
    rise = [121.0 + 6.0 * i for i in range(1, 31)]   # 127 -> 301
    sig = T.ma_cross(_dec_series(decline + rise))
    assert sig["cross"] == "golden"
    assert isinstance(sig["days_ago"], int) and sig["days_ago"] >= 0


def test_ma_cross_detects_death() -> None:
    # A long rise then a sharp decline that drags MA20 below MA60 -> death.
    rise = [100.0 + 1.0 * i for i in range(80)]        # 100 -> 179
    decline = [179.0 - 6.0 * i for i in range(1, 31)]  # 173 -> -1 (values only, not price)
    assert T.ma_cross(_dec_series(rise + decline))["cross"] == "death"


def test_ma_cross_none_when_no_cross_or_too_short() -> None:
    assert T.ma_cross(_dec_series([1.0] * 30))["cross"] is None  # < slow+1
    flat = _dec_series([50.0] * 120)
    assert T.ma_cross(flat)["cross"] is None  # never crosses


def test_week52_position_pct_from_high_low() -> None:
    closes = _dec_series([100, 120, 80, 90])  # hi 120, lo 80, price 90
    pos = T.week52_position(closes)
    assert pos["high"] == Decimal("120") and pos["low"] == Decimal("80")
    assert pos["pct_from_high"] == (Decimal("90") - Decimal("120")) / Decimal("120")
    assert pos["pct_from_low"] == (Decimal("90") - Decimal("80")) / Decimal("80")
    assert pos["window_days"] == 4  # honest actual window, not padded to 252


def test_week52_window_caps_at_252_with_multi_year_history() -> None:
    # P1 acceptance: once 5y (>252 sessions) exists, the 52-week window fills to 252.
    closes = _dec_series([100.0 + (i % 37) for i in range(400)])  # > 252 sessions
    assert T.week52_position(closes)["window_days"] == 252
    w52 = T.technical_signals(closes)["week52"]
    assert isinstance(w52, dict) and w52["window_days"] == 252


def test_trend_structure_uptrend_downtrend_range() -> None:
    up = _dec_series([float(i) for i in range(1, 21)])
    down = _dec_series([float(i) for i in range(20, 0, -1)])
    rng = _dec_series([10, 12, 9, 11, 10, 12, 9, 11])
    assert T.trend_structure(up)["structure"] == "uptrend"
    assert T.trend_structure(down)["structure"] == "downtrend"
    assert T.trend_structure(rng)["structure"] == "range"
    assert T.trend_structure(_dec_series([1, 2]))["structure"] is None


def test_volume_signal_surge_and_gate() -> None:
    vols = _dec_series([100.0] * 20 + [250.0])  # latest 2.5x the 20-bar avg
    sig = T.volume_signal(vols)
    assert sig["surge"] is True
    assert sig["ratio_to_avg"] == Decimal("250") / Decimal("100")
    assert T.volume_signal(_dec_series([1.0, 2.0]))["surge"] is None  # too short


def test_volume_signal_none_gap_in_window_degrades() -> None:
    # A gap session (None volume) inside the needed window+1 recent bars must degrade
    # to the insufficient-data result — never raise (never-500 discipline).
    vols: list[Decimal | None] = [*([Decimal("100")] * 20), None, Decimal("250")]
    sig = T.volume_signal(vols)
    assert sig == {"ratio_to_avg": None, "surge": None}


def test_volume_signal_trims_trailing_none() -> None:
    # The newest row is systematically volume-less when the latest-quote provider
    # carries no volume (TW: twse) — trailing Nones are trimmed so the signal
    # computes from the most recent sessions WITH volume instead of degrading daily.
    vols: list[Decimal | None] = [*([Decimal("100")] * 20), Decimal("250"), None]
    sig = T.volume_signal(vols)
    assert sig["ratio_to_avg"] == Decimal("250") / Decimal("100")
    assert sig["surge"] is True
    # All-None (nothing trimmable left) -> insufficient-data result.
    assert T.volume_signal([None] * 25) == {"ratio_to_avg": None, "surge": None}


def test_technical_signals_accepts_none_padded_volumes() -> None:
    # The aligned-with-closes series is None-padded for gap sessions; a gap OUTSIDE
    # the recent window must not block the computed signal.
    closes = _dec_series([100.0 + i for i in range(1, 80)])
    vols: list[Decimal | None] = [None, *_dec_series([100.0] * 78)]
    out = T.technical_signals(closes, vols)
    vol = out["volume"]
    assert isinstance(vol, dict)
    assert vol["ratio_to_avg"] == Decimal("1.00")
    assert vol["surge"] is False


def test_technical_signals_bundles_and_omits_volume_by_default() -> None:
    closes = _dec_series([100.0 + i for i in range(1, 80)])
    out = T.technical_signals(closes)
    assert set(out) == {"rsi14", "ma_cross", "week52", "trend"}  # no volume without data
    assert T.technical_signals([]) == {"unavailable": True}
    with_vol = T.technical_signals(closes, _dec_series([100.0] * 79))
    assert "volume" in with_vol  # fed volumes -> section present


def test_technical_signals_are_quantized_for_display() -> None:
    # RSI to 1 dp, position ratios to 4 dp — no 26-digit Decimal noise in the prompt.
    closes = _dec_series([100.0 + (i % 7) - 3 for i in range(1, 90)])
    out = T.technical_signals(closes)
    rsi_v = out["rsi14"]
    assert isinstance(rsi_v, Decimal)
    assert rsi_v == rsi_v.quantize(Decimal("0.1"))  # exactly 1 dp
    w52 = out["week52"]
    assert isinstance(w52, dict)
    pfh = w52["pct_from_high"]
    assert pfh is None or pfh == pfh.quantize(Decimal("0.0001"))
