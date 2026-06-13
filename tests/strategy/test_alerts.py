"""Risk-alert rule engine (spec 03 §3.1). The engine is the SINGLE source for both
GET /api/alerts and the dashboard payload's embedded alerts; these tests pin the six
v1 rules over the golden DB plus a hand-built DashboardData for fx_drift/exdiv_upcoming
(which do not fire on golden: avg≈spot and the only dividend is in the past)."""

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from portfolio_dash.forex.results import AccountFXResult, FXSummary
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.portfolio.dashboard_models import (
    DashboardData,
    DividendSummary,
    ExDividendItem,
    FreshnessReport,
    KpiSummary,
    TrendSeries,
)
from portfolio_dash.portfolio.results import RealizedPnL
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.llm_config import set_alert_threshold
from portfolio_dash.strategy.alerts import compute_alerts, compute_alerts_from
from portfolio_dash.strategy.rules_config import DEFAULT_RULES

_NOW = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))


def test_single_weight_fires_on_golden(golden_db) -> None:
    data = build_dashboard(golden_db, now=_NOW, reporting=Currency.TWD)
    alerts = compute_alerts_from(data, DEFAULT_RULES,
                                 quota_remaining=Decimal("5"), quota_threshold=Decimal("1"))
    ids = {a.id for a in alerts}
    assert "single_weight:2330" in ids
    sw = next(a for a in alerts if a.id == "single_weight:2330")
    assert sw.sev == "risk" and sw.href == "/symbol/2330"


def test_quota_low_zero_is_risk(golden_db) -> None:
    data = build_dashboard(golden_db, now=_NOW, reporting=Currency.TWD)
    alerts = compute_alerts_from(data, DEFAULT_RULES,
                                 quota_remaining=Decimal("0"), quota_threshold=Decimal("1"))
    q = next(a for a in alerts if a.id == "quota_low")
    assert q.sev == "risk"


def test_quota_low_below_threshold_is_warn(golden_db) -> None:
    data = build_dashboard(golden_db, now=_NOW, reporting=Currency.TWD)
    alerts = compute_alerts_from(data, DEFAULT_RULES,
                                 quota_remaining=Decimal("0.5"), quota_threshold=Decimal("1"))
    q = next(a for a in alerts if a.id == "quota_low")
    assert q.sev == "warn"


def test_disabled_rule_silent(golden_db) -> None:
    data = build_dashboard(golden_db, now=_NOW, reporting=Currency.TWD)
    rules = DEFAULT_RULES.model_copy(deep=True)
    rules.single_weight.enabled = False
    alerts = compute_alerts_from(data, rules,
                                 quota_remaining=Decimal("5"), quota_threshold=Decimal("1"))
    assert not any(a.id.startswith("single_weight") for a in alerts)


def test_compute_alerts_wrapper_uses_db(golden_db) -> None:
    # golden DB has no topups -> budget_remaining 0; set a $1 threshold so 0 < 1 fires
    # quota_low (default threshold is 0, which would not). single_weight fires regardless.
    set_alert_threshold(golden_db, Decimal("1"))
    alerts = compute_alerts(golden_db, now=_NOW, reporting=Currency.TWD)
    ids = {a.id for a in alerts}
    assert "single_weight:2330" in ids and "quota_low" in ids
    q = next(a for a in alerts if a.id == "quota_low")
    assert q.sev == "risk"  # remaining == 0 -> risk


def _minimal_data(*, fx: FXSummary | None,
                  calendar: list[ExDividendItem]) -> DashboardData:
    """A DashboardData populated only where the fx_drift / exdiv_upcoming rules read.

    Everything the two rules ignore is set to a neutral empty value so the engine has
    nothing else to fire on (no holdings, no allocation, no stale/missing prices).
    """
    reporting = Currency.TWD
    return DashboardData(
        as_of=_NOW,
        reporting_currency=reporting,
        kpis=KpiSummary(reporting_currency=reporting),
        holdings=[],
        realized=RealizedPnL(rows=[], by_currency={}),
        returns=None,
        allocation=None,
        currency_view=None,
        fx=fx,
        dividends=DividendSummary(by_year=[], total_by_currency={}),
        ex_dividend_calendar=calendar,
        trend=TrendSeries(points=[], reporting_currency=reporting, available=False),
        freshness=FreshnessReport(prices=[], fx=[], any_stale=False,
                                  missing_prices=[], missing_fx=[]),
    )


def test_fx_drift_fires() -> None:
    acct = AccountFXResult(
        account_id="schwab", home_ccy=Currency.TWD, foreign_ccy=Currency.USD,
        avg_rate=Decimal("30"), current_spot=Decimal("33"),
        foreign_cash=Decimal("0"), foreign_stock_value=Decimal("0"),
        realized_fx=None, unrealized_fx_stocks=None, unrealized_fx_cash=None)
    fx = FXSummary(by_account={"schwab": acct}, reporting_currency=Currency.TWD,
                   reporting_realized_fx=Decimal("0"), reporting_unrealized_fx=Decimal("0"))
    data = _minimal_data(fx=fx, calendar=[])
    alerts = compute_alerts_from(data, DEFAULT_RULES,
                                 quota_remaining=Decimal("5"), quota_threshold=Decimal("1"))
    # drift = |33/30 - 1| = 0.10 > 0.03 default -> fires info
    fd = next(a for a in alerts if a.id == "fx_drift:schwab")
    assert fd.sev == "info"


def test_exdiv_upcoming_fires() -> None:
    ex_date = _NOW.date() + timedelta(days=7)  # <= 14 default
    item = ExDividendItem(symbol="2330", name="TSMC", ex_date=ex_date, source="test")
    data = _minimal_data(fx=None, calendar=[item])
    alerts = compute_alerts_from(data, DEFAULT_RULES,
                                 quota_remaining=Decimal("5"), quota_threshold=Decimal("1"))
    ev = next(a for a in alerts if a.id == "exdiv_upcoming:2330")
    assert ev.sev == "info" and ev.href == "/symbol/2330"
