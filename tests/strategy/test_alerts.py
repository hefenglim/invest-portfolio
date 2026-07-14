"""Risk-alert rule engine (spec 03 §3.1). The engine is the SINGLE source for both
GET /api/alerts and the dashboard payload's embedded alerts; these tests pin the six
v1 rules over the golden DB plus a hand-built DashboardData for fx_drift/exdiv_upcoming
(which do not fire on golden: avg≈spot and the only dividend is in the past)."""

import sqlite3
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
from portfolio_dash.shared.llm_config import (
    LLMRole,
    ModelConfig,
    ensure_llm_seeded,
    set_alert_threshold,
    set_role,
    upsert_model,
)
from portfolio_dash.strategy.alerts import compute_alerts, compute_alerts_from
from portfolio_dash.strategy.rules_config import DEFAULT_RULES

_NOW = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))


def test_single_weight_fires_on_golden(golden_db: sqlite3.Connection) -> None:
    data = build_dashboard(golden_db, now=_NOW, reporting=Currency.TWD)
    alerts = compute_alerts_from(data, DEFAULT_RULES,
                                 quota_remaining=Decimal("5"), quota_threshold=Decimal("1"))
    ids = {a.id for a in alerts}
    assert "single_weight:2330" in ids
    sw = next(a for a in alerts if a.id == "single_weight:2330")
    assert sw.sev == "risk" and sw.href == "/symbol/2330"
    # FH2 fix: the detail is display copy — a quantized percent + zh phrasing, never
    # the raw full-precision Decimal ("weight 0.7528455359… > 0.30").
    assert "門檻" in sw.detail and "%" in sw.detail
    assert "weight" not in sw.detail
    frac = sw.detail.split("%")[0].rsplit(" ", 1)[-1]
    assert len(frac.split(".")[-1]) <= 1  # at most 1 dp on the displayed percent


def test_quota_low_zero_is_risk(golden_db: sqlite3.Connection) -> None:
    data = build_dashboard(golden_db, now=_NOW, reporting=Currency.TWD)
    alerts = compute_alerts_from(data, DEFAULT_RULES,
                                 quota_remaining=Decimal("0"), quota_threshold=Decimal("1"))
    q = next(a for a in alerts if a.id == "quota_low")
    assert q.sev == "risk"


def test_quota_low_below_threshold_is_warn(golden_db: sqlite3.Connection) -> None:
    data = build_dashboard(golden_db, now=_NOW, reporting=Currency.TWD)
    alerts = compute_alerts_from(data, DEFAULT_RULES,
                                 quota_remaining=Decimal("0.5"), quota_threshold=Decimal("1"))
    q = next(a for a in alerts if a.id == "quota_low")
    assert q.sev == "warn"


def test_quota_low_gated_off_when_ai_inactive(golden_db: sqlite3.Connection) -> None:
    """3B: a low budget is NOT worth alerting on when AI is off (no enabled model)."""
    data = build_dashboard(golden_db, now=_NOW, reporting=Currency.TWD)
    alerts = compute_alerts_from(
        data, DEFAULT_RULES, quota_remaining=Decimal("0"), quota_threshold=Decimal("1"),
        ai_active=False,
    )
    assert not any(a.id == "quota_low" for a in alerts)


def test_quota_low_fires_when_ai_active_and_below(golden_db: sqlite3.Connection) -> None:
    data = build_dashboard(golden_db, now=_NOW, reporting=Currency.TWD)
    alerts = compute_alerts_from(
        data, DEFAULT_RULES, quota_remaining=Decimal("0.5"), quota_threshold=Decimal("1"),
        ai_active=True,
    )
    assert any(a.id == "quota_low" for a in alerts)


def test_quota_low_silent_above_threshold_regardless_of_ai(
    golden_db: sqlite3.Connection,
) -> None:
    data = build_dashboard(golden_db, now=_NOW, reporting=Currency.TWD)
    for ai in (True, False):
        alerts = compute_alerts_from(
            data, DEFAULT_RULES, quota_remaining=Decimal("5"), quota_threshold=Decimal("1"),
            ai_active=ai,
        )
        assert not any(a.id == "quota_low" for a in alerts)


def test_disabled_rule_silent(golden_db: sqlite3.Connection) -> None:
    data = build_dashboard(golden_db, now=_NOW, reporting=Currency.TWD)
    rules = DEFAULT_RULES.model_copy(deep=True)
    rules.single_weight.enabled = False
    alerts = compute_alerts_from(data, rules,
                                 quota_remaining=Decimal("5"), quota_threshold=Decimal("1"))
    assert not any(a.id.startswith("single_weight") for a in alerts)


def test_compute_alerts_wrapper_uses_db(golden_db: sqlite3.Connection) -> None:
    # golden DB has no topups -> budget_remaining 0; set $1 threshold explicitly (the
    # default is now 1.00) so 0 < 1 fires quota_low. single_weight fires regardless.
    # 3B: quota_low is now gated on ai_active, so activate AI (bind an enabled model to a
    # role) — this also proves the wrapper reads ai_active from the SAME conn.
    ensure_llm_seeded(golden_db)
    upsert_model(golden_db, ModelConfig(
        id="m", model_alias="M", provider="anthropic", model_name="claude-x"))
    set_role(golden_db, LLMRole.DEFAULT, "m")
    set_alert_threshold(golden_db, Decimal("1"))
    alerts = compute_alerts(golden_db, now=_NOW, reporting=Currency.TWD)
    ids = {a.id for a in alerts}
    assert "single_weight:2330" in ids and "quota_low" in ids
    q = next(a for a in alerts if a.id == "quota_low")
    assert q.sev == "risk"  # remaining == 0 -> risk


def test_compute_alerts_wrapper_gates_quota_low_when_ai_off(
    golden_db: sqlite3.Connection,
) -> None:
    """3B: with the golden DB AI-off (no model bound), the conn-bearing wrapper feeds
    ai_active=False, so quota_low does NOT fire even at $0 remaining."""
    set_alert_threshold(golden_db, Decimal("1"))
    alerts = compute_alerts(golden_db, now=_NOW, reporting=Currency.TWD)
    ids = {a.id for a in alerts}
    assert "single_weight:2330" in ids and "quota_low" not in ids


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
    assert "10.0%" in fd.detail and "門檻" in fd.detail  # FH2 display percent


def test_fx_drift_uses_account_display_name() -> None:
    # FH2 fix: the bell shows the accounts table's display name, not the raw id.
    acct = AccountFXResult(
        account_id="moomoo_my_us", home_ccy=Currency.MYR, foreign_ccy=Currency.USD,
        avg_rate=Decimal("4.0"), current_spot=Decimal("4.6"),
        foreign_cash=Decimal("0"), foreign_stock_value=Decimal("0"),
        realized_fx=None, unrealized_fx_stocks=None, unrealized_fx_cash=None)
    fx = FXSummary(by_account={"moomoo_my_us": acct}, reporting_currency=Currency.TWD,
                   reporting_realized_fx=Decimal("0"), reporting_unrealized_fx=Decimal("0"))
    data = _minimal_data(fx=fx, calendar=[])
    alerts = compute_alerts_from(
        data, DEFAULT_RULES, quota_remaining=Decimal("5"), quota_threshold=Decimal("1"),
        account_names={"moomoo_my_us": "Moomoo MY (US)"})
    fd = next(a for a in alerts if a.id == "fx_drift:moomoo_my_us")
    assert "Moomoo MY (US)" in fd.title
    assert "moomoo_my_us" not in fd.title


def test_exdiv_upcoming_fires() -> None:
    ex_date = _NOW.date() + timedelta(days=7)  # <= 14 default
    item = ExDividendItem(symbol="2330", name="TSMC", ex_date=ex_date, source="test")
    data = _minimal_data(fx=None, calendar=[item])
    alerts = compute_alerts_from(data, DEFAULT_RULES,
                                 quota_remaining=Decimal("5"), quota_threshold=Decimal("1"))
    ev = next(a for a in alerts if a.id == "exdiv_upcoming:2330")
    assert ev.sev == "info" and ev.href == "/symbol/2330"
    assert ev.detail == "7 天後除息"  # FH2: zh phrasing, not "ex-date in 7d"


def test_calib_gap_fires_above_threshold() -> None:
    # 20pp > 15pp default -> a single global warn alert (no symbol).
    data = _minimal_data(fx=None, calendar=[])
    alerts = compute_alerts_from(data, DEFAULT_RULES,
                                 quota_remaining=Decimal("5"), quota_threshold=Decimal("1"),
                                 calib_gap=Decimal("20"))
    cg = next(a for a in alerts if a.id == "calib_gap")
    assert cg.sev == "warn" and cg.rule == "calib_gap" and cg.href == "/settings"
    assert "20pp" in cg.detail and "15pp" in cg.detail
    assert "門檻" in cg.detail  # FH2: display copy is zh, not a debug log


def test_calib_gap_silent_at_or_below_threshold() -> None:
    # 10pp <= 15pp default -> does NOT fire (and equality at 15pp must not fire either).
    data = _minimal_data(fx=None, calendar=[])
    for gap in (Decimal("10"), Decimal("15")):
        alerts = compute_alerts_from(data, DEFAULT_RULES,
                                     quota_remaining=Decimal("5"), quota_threshold=Decimal("1"),
                                     calib_gap=gap)
        assert not any(a.id == "calib_gap" for a in alerts)


def test_calib_gap_silent_when_none() -> None:
    # None (below the global min_samples gate) -> silent, never fires.
    data = _minimal_data(fx=None, calendar=[])
    alerts = compute_alerts_from(data, DEFAULT_RULES,
                                 quota_remaining=Decimal("5"), quota_threshold=Decimal("1"),
                                 calib_gap=None)
    assert not any(a.id == "calib_gap" for a in alerts)


def test_calib_gap_silent_when_rule_disabled() -> None:
    # A high gap but the rule is off -> silent.
    data = _minimal_data(fx=None, calendar=[])
    rules = DEFAULT_RULES.model_copy(deep=True)
    rules.calib_gap.enabled = False
    alerts = compute_alerts_from(data, rules,
                                 quota_remaining=Decimal("5"), quota_threshold=Decimal("1"),
                                 calib_gap=Decimal("40"))
    assert not any(a.id == "calib_gap" for a in alerts)
