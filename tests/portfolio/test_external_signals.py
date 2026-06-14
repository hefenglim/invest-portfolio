"""Hand-checked unit tests for the external-signal derivations (spec 20.5).

Pure Decimal math (no float): consecutive-buy run, trailing net-buy sum, change /
YoY / MoM ratios (None when denominator <= 0), historical percentile, and the VIX
zone classifier. Mirrors the discipline in ``portfolio/technicals.py``.
"""

from decimal import Decimal

from portfolio_dash.portfolio import external_signals as ES


def _d(values: list[str]) -> list[Decimal]:
    return [Decimal(v) for v in values]


# --- consecutive_buy_days -----------------------------------------------------


def test_consecutive_buy_days_trailing_run() -> None:
    # Trailing run of positives counts from the newest end (last element newest).
    assert ES.consecutive_buy_days(_d(["1", "1", "-1", "1", "1", "1"])) == 3


def test_consecutive_buy_days_all_positive() -> None:
    assert ES.consecutive_buy_days(_d(["5", "2", "3"])) == 3


def test_consecutive_buy_days_zero_breaks_run() -> None:
    # A zero (not strictly positive) ends the run.
    assert ES.consecutive_buy_days(_d(["1", "0", "1"])) == 1


def test_consecutive_buy_days_newest_not_positive_is_zero() -> None:
    assert ES.consecutive_buy_days(_d(["1", "1", "-2"])) == 0


def test_consecutive_buy_days_empty() -> None:
    assert ES.consecutive_buy_days([]) == 0


def test_consecutive_sell_days() -> None:
    assert ES.consecutive_sell_days(_d(["1", "-1", "-2", "-3"])) == 3


# --- net_buy_sum --------------------------------------------------------------


def test_net_buy_sum_last_n() -> None:
    assert ES.net_buy_sum(_d(["10", "20", "30", "40"]), 3) == Decimal("90")


def test_net_buy_sum_fewer_than_n() -> None:
    assert ES.net_buy_sum(_d(["10", "20"]), 5) == Decimal("30")


def test_net_buy_sum_empty() -> None:
    assert ES.net_buy_sum([], 3) == Decimal("0")


# --- chg_pct / yoy / mom (None when denom <= 0) -------------------------------


def test_chg_pct_basic() -> None:
    assert ES.chg_pct(Decimal("110"), Decimal("100")) == Decimal("0.1")


def test_chg_pct_negative() -> None:
    assert ES.chg_pct(Decimal("90"), Decimal("100")) == Decimal("-0.1")


def test_chg_pct_zero_denominator_none() -> None:
    assert ES.chg_pct(Decimal("5"), Decimal("0")) is None


def test_chg_pct_negative_denominator_none() -> None:
    assert ES.chg_pct(Decimal("5"), Decimal("-3")) is None


def test_yoy_and_mom() -> None:
    assert ES.yoy(Decimal("131"), Decimal("100")) == Decimal("0.31")
    assert ES.mom(Decimal("104"), Decimal("100")) == Decimal("0.04")
    assert ES.yoy(Decimal("100"), Decimal("0")) is None
    assert ES.mom(Decimal("100"), Decimal("0")) is None


# --- percentile ---------------------------------------------------------------


def test_percentile_in_range() -> None:
    hist = _d(["10", "20", "30", "40"])
    p = ES.percentile(Decimal("25"), hist)
    assert p is not None
    assert Decimal("0") <= p <= Decimal("1")
    # 2 of 4 historical values are <= 25 -> 0.5.
    assert p == Decimal("0.5")


def test_percentile_max() -> None:
    hist = _d(["10", "20", "30"])
    assert ES.percentile(Decimal("30"), hist) == Decimal("1")


def test_percentile_min() -> None:
    hist = _d(["10", "20", "30"])
    # No historical value <= 5 -> 0.
    assert ES.percentile(Decimal("5"), hist) == Decimal("0")


def test_percentile_empty_history_none() -> None:
    assert ES.percentile(Decimal("25"), []) is None


# --- vix_zone -----------------------------------------------------------------


def test_vix_zone_boundaries() -> None:
    assert ES.vix_zone(Decimal("14.99")) == "low"
    assert ES.vix_zone(Decimal("15")) == "normal"
    assert ES.vix_zone(Decimal("24.99")) == "normal"
    assert ES.vix_zone(Decimal("25")) == "elevated"
    assert ES.vix_zone(Decimal("34.99")) == "elevated"
    assert ES.vix_zone(Decimal("35")) == "high"
    assert ES.vix_zone(Decimal("80")) == "high"


# --- snapshot -> variable assemblers (pure; spec 20.5) ------------------------


def test_build_institutional_foreign_net_and_streak() -> None:
    rows = [
        {"date": "2026-06-09", "name": "Foreign_Investor", "buy": 100, "sell": 200},
        {"date": "2026-06-10", "name": "Foreign_Investor", "buy": 300, "sell": 100},
        {"date": "2026-06-11", "name": "Foreign_Investor", "buy": 400, "sell": 100},
        {"date": "2026-06-11", "name": "Investment_Trust", "buy": 50, "sell": 10},
    ]
    out = ES.build_institutional(rows, symbol="2330", as_of="2026-06-11")
    assert out["symbol"] == "2330"
    assert out["last_as_of"] == "2026-06-11"
    assert out["consecutive_buy_days"] == 2  # 06-11 +, 06-10 +, 06-09 - breaks
    # Foreign net over the window = (-100) + 200 + 300 = 400.
    assert out["foreign_net_total"] == "400"


def test_build_institutional_empty_unavailable() -> None:
    assert ES.build_institutional([], symbol="2330", as_of="2026-06-11") == {
        "unavailable": True, "last_as_of": None
    }


def test_build_margin_balance_change() -> None:
    rows = [
        {"date": "2026-06-09", "MarginPurchaseTodayBalance": 20000,
         "ShortSaleTodayBalance": 500},
        {"date": "2026-06-11", "MarginPurchaseTodayBalance": 19280,
         "ShortSaleTodayBalance": 540},
    ]
    out = ES.build_margin(rows, symbol="2330", as_of="2026-06-11")
    assert out["margin_balance"] == "19280"
    assert out["short_balance"] == "540"
    # (19280 - 20000) / 20000 = -0.036
    assert out["margin_balance_chg"] == "-0.036"


def test_build_valuation_fields() -> None:
    rows = [{"date": "2026-06-11", "PER": "24.1", "PBR": "6.2", "dividend_yield": "1.8"}]
    out = ES.build_valuation(rows, symbol="2330", as_of="2026-06-11")
    assert out["per"] == "24.1" and out["pbr"] == "6.2" and out["dividend_yield"] == "1.8"
    # 5y percentile of the latest PER over the window's PER history (single point -> 1).
    assert out["per_percentile"] == "1"


def test_build_monthly_revenue_yoy_mom() -> None:
    rows = [
        {"date": "2025-05-31", "revenue": 100, "revenue_year": 2025, "revenue_month": 5},
        {"date": "2026-04-30", "revenue": 120, "revenue_year": 2026, "revenue_month": 4},
        {"date": "2026-05-31", "revenue": 131, "revenue_year": 2026, "revenue_month": 5},
    ]
    out = ES.build_monthly_revenue(rows, symbol="2330", as_of="2026-05-31")
    assert out["latest_revenue"] == "131"
    assert out["yoy"] == "0.31"  # vs 2025-05 = 100
    # mom uses the previous row (2026-04 = 120): (131-120)/120.
    assert out["mom"] == str(Decimal("11") / Decimal("120"))


def test_build_financials_keeps_recent() -> None:
    rows = [{"date": "2026-03-31", "type": "EPS", "value": "14.2", "origin_name": "EPS"}]
    out = ES.build_financials(rows, symbol="2330", as_of="2026-03-31")
    assert out["last_as_of"] == "2026-03-31"
    assert out["rows"] == rows


def test_build_market_sentiment() -> None:
    out = ES.build_market_sentiment(
        vix_close=Decimal("14.2"), as_of_vix="2026-06-11",
        fng={"score": "62", "rating": "greed"}, as_of_fng="2026-06-11",
    )
    assert out["vix"] == "14.2" and out["vix_zone"] == "low"
    assert out["fear_greed"] == "62" and out["fear_greed_rating"] == "greed"


def test_build_market_sentiment_partial() -> None:
    # VIX present, F&G missing -> still available, F&G fields null.
    out = ES.build_market_sentiment(
        vix_close=Decimal("30"), as_of_vix="2026-06-11", fng=None, as_of_fng=None
    )
    assert out["vix_zone"] == "elevated"
    assert out["fear_greed"] is None and out["fear_greed_rating"] is None


def test_build_market_sentiment_all_missing_unavailable() -> None:
    out = ES.build_market_sentiment(
        vix_close=None, as_of_vix=None, fng=None, as_of_fng=None
    )
    assert out == {"unavailable": True, "last_as_of": None}


def test_build_index_quotes() -> None:
    out = ES.build_index_quotes(
        {"^TWII": Decimal("22150.5"), "^GSPC": Decimal("5980.12"), "^KLSE": Decimal("1612")},
        as_of="2026-06-11",
    )
    assert out["TAIEX"] == "22150.5" and out["SPX"] == "5980.12" and out["KLCI"] == "1612"
    assert out["last_as_of"] == "2026-06-11"


def test_build_index_quotes_empty_unavailable() -> None:
    assert ES.build_index_quotes({}, as_of=None) == {"unavailable": True, "last_as_of": None}
