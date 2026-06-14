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
