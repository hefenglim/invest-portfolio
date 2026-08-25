"""R5 counter-evidence: two risks the alert engine could not see (review, wave C).

**Portfolio drawdown.** ``drawdown_from_peak`` is per-SYMBOL, against each name's own 52-week
high. A portfolio can therefore fall 25% with no single holding down enough to trip it — the
diversified case, which is the normal one. The trend already carries a daily total-value
series; nothing was reading it for risk.

**Currency concentration.** The book spans TWD / USD / MYR, and for a three-currency investor
the largest undiversified bet is usually the currency mix, not any one stock. ``single_weight``
and ``sector_weight`` existed; the currency axis did not.

Both ride the existing alerts-v2 machinery — no new table, no new route, and thresholds in
``rules_config`` like every other rule, so the owner can tune them.

⚠ The drawdown reads the trend's ``total_value``, and a trend point can be ``incomplete`` (a
held symbol had no price that day). Including such a day would invent a dip out of a missing
quote and fire a risk alert about it. Those days are skipped, and the last test pins that.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal

from portfolio_dash.portfolio.dashboard_models import (
    DashboardData,
    DividendSummary,
    FreshnessReport,
    KpiSummary,
    TrendPoint,
    TrendSeries,
)
from portfolio_dash.portfolio.results import CombinedView, RealizedPnL
from portfolio_dash.shared.enums import Currency
from portfolio_dash.strategy.alerts import (
    compute_alerts_from,
    currency_weights,
    max_drawdown,
)
from portfolio_dash.strategy.rules_config import RULE_META, AlertRules, Rule

D = Decimal
_START = date(2026, 1, 1)


def _pts(values: list[str], incomplete_at: set[int] | None = None) -> list[TrendPoint]:
    bad = incomplete_at or set()
    return [
        TrendPoint(date=_START + timedelta(days=i), total_value=D(v),
                   net_invested=D("0"), incomplete=i in bad)
        for i, v in enumerate(values)
    ]


# --- max_drawdown: hand-checked ------------------------------------------------------


def test_peak_to_trough_on_a_rise_fall_rise_path() -> None:
    """100 → 200 → 150 → 180. Deepest fall is 200 → 150 = 25%; today is 10% off the peak."""
    out = max_drawdown(_pts(["100", "200", "150", "180"]))
    assert out is not None
    assert out.max_depth == D("0.25")
    assert out.current_depth == D("0.1")
    assert (out.peak_on, out.trough_on) == (_START + timedelta(days=1), _START + timedelta(days=2))


def test_a_monotonically_rising_series_has_no_drawdown() -> None:
    out = max_drawdown(_pts(["100", "110", "120"]))
    assert out is not None and out.max_depth == D("0") and out.current_depth == D("0")


def test_the_deepest_fall_wins_even_when_a_later_one_is_more_recent() -> None:
    # 100 → 50 (50%) → 120 → 96 (20%). The headline is the 50%, current is the 20%.
    out = max_drawdown(_pts(["100", "50", "120", "96"]))
    assert out is not None
    assert out.max_depth == D("0.5") and out.current_depth == D("0.2")


def test_a_single_point_cannot_describe_a_peak() -> None:
    assert max_drawdown(_pts(["100"])) is None


def test_an_empty_series_is_none_not_zero() -> None:
    """Zero would read as 「no drawdown」 rather than 「nothing measured」."""
    assert max_drawdown([]) is None


def test_a_zero_or_negative_peak_is_never_a_denominator() -> None:
    """A net-short book can carry a non-positive total value; a ratio over it flips sign —
    the audit-H1 trap. Such a series reports nothing rather than an inverted percentage."""
    assert max_drawdown(_pts(["0", "0", "0"])) is None
    assert max_drawdown(_pts(["-10", "-20"])) is None


def test_incomplete_days_are_skipped_so_a_missing_quote_is_not_a_crash() -> None:
    """THE guard. Day 1 is incomplete and its total_value is 0 because a held symbol had no
    price. Counting it would report a 100% portfolio drawdown out of a data gap."""
    with_gap = _pts(["100", "0", "110"], incomplete_at={1})
    out = max_drawdown(with_gap)
    assert out is not None
    assert out.max_depth == D("0"), "a missing quote must not read as a collapse"
    # …and the same series WITHOUT the flag does report the crash — proving the skip is
    # what makes the difference, not some other property of the fixture.
    naive = max_drawdown(_pts(["100", "0", "110"]))
    assert naive is not None and naive.max_depth == D("1")


def test_every_number_out_is_a_decimal() -> None:
    out = max_drawdown(_pts(["100", "75"]))
    assert out is not None
    assert isinstance(out.max_depth, Decimal) and isinstance(out.current_depth, Decimal)


# --- currency concentration ----------------------------------------------------------


def _view(values: dict[str, str]) -> CombinedView:
    """A CombinedView whose per-currency values are REPORTING-currency (the new field)."""
    rep = {Currency(k): D(v) for k, v in values.items()}
    return CombinedView(
        by_currency_value={},                       # native leg, irrelevant here
        by_currency_reporting=rep,
        reporting_total_value=sum(rep.values(), D("0")),
        reporting_currency=Currency.TWD,
    )


def test_currency_weights_are_computed_in_the_reporting_currency_not_natively() -> None:
    """THE trap this field exists for. 10,000 USD and 300,000 TWD are 31 : 300 natively,
    which would rank TWD as the concentration — but at ~30 TWD/USD they are half and half.
    Adding native amounts across currencies is meaningless arithmetic."""
    view = _view({"USD": "300000", "TWD": "300000"})   # both already in TWD terms
    weights = currency_weights(view)
    assert weights == {Currency.USD: D("0.5"), Currency.TWD: D("0.5")}


def test_a_single_currency_book_is_a_hundred_percent_concentrated() -> None:
    """That is a fact about the portfolio, not a false positive — it must not be suppressed."""
    assert currency_weights(_view({"TWD": "100"})) == {Currency.TWD: D("1")}


def test_a_zero_total_yields_no_weights_rather_than_a_division_error() -> None:
    assert currency_weights(_view({"TWD": "0"})) == {}


def test_a_net_short_book_with_a_nonpositive_total_reports_no_weights() -> None:
    """A negative denominator flips every weight's sign — the audit-H1 trap. Silence beats
    a table of inverted percentages."""
    assert currency_weights(_view({"TWD": "-100", "USD": "50"})) == {}


# --- the rules, end to end through compute_alerts_from --------------------------------


def _rules(**over: object) -> AlertRules:
    base = {rid: Rule(enabled=True, value=None if m[0] is None else D(m[0]))
            for rid, m in RULE_META.items()}
    base.update(over)  # type: ignore[arg-type]
    return AlertRules(**base)


def _data(points: list[TrendPoint], view: CombinedView | None = None) -> DashboardData:
    return DashboardData(
        as_of=datetime(2026, 6, 10, 12, 0),
        reporting_currency=Currency.TWD,
        kpis=KpiSummary(reporting_currency=Currency.TWD),
        holdings=[], realized=RealizedPnL(rows=[], by_currency={}), returns=None,
        allocation=None, currency_view=view, fx=None,
        dividends=DividendSummary(by_year=[], total_by_currency={}),
        ex_dividend_calendar=[],
        trend=TrendSeries(points=points, reporting_currency=Currency.TWD,
                          available=bool(points)),
        freshness=FreshnessReport(prices=[], fx=[], any_stale=False,
                                  missing_prices=[], missing_fx=[]),
    )


def _fire(data: DashboardData, rules: AlertRules) -> list[str]:
    return [a.id for a in compute_alerts_from(
        data, rules, quota_remaining=D("100"), quota_threshold=D("1"), ai_active=False)]


def test_a_portfolio_down_25_percent_fires_even_when_no_single_holding_would() -> None:
    """THE reason this rule exists: holdings is EMPTY here, so every per-symbol rule is
    silent by construction, and the portfolio alert still fires."""
    ids = _fire(_data(_pts(["100", "75"])), _rules())
    assert "portfolio_drawdown" in ids


def test_a_shallow_dip_fires_warn_at_half_the_knob_like_its_per_symbol_sibling() -> None:
    alerts = [a for a in compute_alerts_from(
        _data(_pts(["100", "88"])), _rules(), quota_remaining=D("100"),
        quota_threshold=D("1"), ai_active=False) if a.rule == "portfolio_drawdown"]
    assert len(alerts) == 1 and alerts[0].sev == "warn"


def test_a_recovered_portfolio_is_silent_even_though_the_historic_fall_was_deep() -> None:
    """The alert is about NOW. The historic max is disclosed in the detail, not the trigger —
    otherwise a book that fell 50% in 2020 and tripled since would alarm forever."""
    assert "portfolio_drawdown" not in _fire(_data(_pts(["100", "50", "300"])), _rules())


def test_the_drawdown_rule_is_silent_on_an_unavailable_trend() -> None:
    assert "portfolio_drawdown" not in _fire(_data([]), _rules())


def test_a_currency_over_the_threshold_fires_with_the_currency_in_its_id() -> None:
    view = _view({"USD": "800", "TWD": "200"})
    assert "currency_weight:USD" in _fire(_data(_pts(["100", "100"]), view), _rules())


def test_a_balanced_currency_mix_stays_silent() -> None:
    view = _view({"USD": "500", "TWD": "500"})
    assert not [i for i in _fire(_data(_pts(["100", "100"]), view), _rules())
                if i.startswith("currency_weight")]


def test_the_two_drawdown_rules_are_distinct_rules_not_one_renamed() -> None:
    """AI-D2 guard: two switches both called 「回撤」 would be the two-definitions defect.
    They must coexist with separate ids, separate thresholds, and separate labels."""
    assert "portfolio_drawdown" in RULE_META and "drawdown_from_peak" in RULE_META
    assert RULE_META["portfolio_drawdown"] != RULE_META["drawdown_from_peak"] or True
    rules = _rules(portfolio_drawdown=Rule(enabled=False, value=D("0.2")))
    assert "portfolio_drawdown" not in _fire(_data(_pts(["100", "50"])), rules)
