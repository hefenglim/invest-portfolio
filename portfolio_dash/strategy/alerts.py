"""Risk-alert rule engine (spec 03 §3.1). Pure over DashboardData — the SINGLE source
for both GET /api/alerts and the dashboard payload's embedded alerts. Six v1 rules;
calib_gap/calibration_regression are deferred to spec 04. Degrades silently when an
input is absent (never raises on missing market data)."""

import sqlite3
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.portfolio.dashboard_models import DashboardData
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.llm_config import budget_remaining, get_alert_threshold
from portfolio_dash.strategy.rules_config import AlertRules, get_alert_rules

Severity = Literal["risk", "warn", "info"]
_ZERO = Decimal("0")
_ONE = Decimal("1")


class Alert(BaseModel):
    id: str
    sev: Severity
    rule: str
    title: str
    detail: str
    href: str | None = None


def compute_alerts_from(
    data: DashboardData, rules: AlertRules, *,
    quota_remaining: Decimal, quota_threshold: Decimal,
) -> list[Alert]:
    alerts: list[Alert] = []
    as_of = data.as_of.date()

    if rules.single_weight.enabled and rules.single_weight.value is not None:
        thr = rules.single_weight.value
        for h in data.holdings:
            if h.weight is not None and h.weight > thr:
                alerts.append(Alert(
                    id=f"single_weight:{h.symbol}", sev="risk", rule="single_weight",
                    title=f"{h.symbol} 單一持股權重偏高",
                    detail=f"weight {h.weight} > {thr}", href=f"/symbol/{h.symbol}"))

    if rules.sector_weight.enabled and rules.sector_weight.value is not None and data.allocation:
        thr = rules.sector_weight.value
        for sector, w in data.allocation.weights.items():
            if w > thr:
                alerts.append(Alert(
                    id=f"sector_weight:{sector}", sev="risk", rule="sector_weight",
                    title=f"{sector} 產業權重偏高", detail=f"weight {w} > {thr}"))

    if rules.stale_price.enabled:
        for p in data.freshness.prices:
            if p.stale:
                alerts.append(Alert(
                    id=f"stale_price:{p.symbol}", sev="warn", rule="stale_price",
                    title=f"{p.symbol} 報價過期", detail="stored price is stale",
                    href=f"/symbol/{p.symbol}"))

    if rules.missing_price.enabled:
        for sym in data.freshness.missing_prices:
            alerts.append(Alert(
                id=f"missing_price:{sym}", sev="warn", rule="missing_price",
                title=f"{sym} 無報價", detail="no stored price", href=f"/symbol/{sym}"))

    if rules.fx_drift.enabled and rules.fx_drift.value is not None and data.fx:
        thr = rules.fx_drift.value
        for acct_id, res in data.fx.by_account.items():
            if res.avg_rate is not None and res.avg_rate != _ZERO and res.current_spot is not None:
                drift = abs(res.current_spot / res.avg_rate - _ONE)
                if drift > thr:
                    alerts.append(Alert(
                        id=f"fx_drift:{acct_id}", sev="info", rule="fx_drift",
                        title=f"{acct_id} 匯率偏離成本", detail=f"drift {drift} > {thr}"))

    if rules.exdiv_upcoming.enabled and rules.exdiv_upcoming.value is not None:
        days = int(rules.exdiv_upcoming.value)
        for item in data.ex_dividend_calendar:
            delta = (item.ex_date - as_of).days
            if 0 <= delta <= days:
                alerts.append(Alert(
                    id=f"exdiv_upcoming:{item.symbol}", sev="info", rule="exdiv_upcoming",
                    title=f"{item.symbol} 即將除息", detail=f"ex-date in {delta}d",
                    href=f"/symbol/{item.symbol}"))

    if rules.quota_low.enabled and quota_remaining < quota_threshold:
        sev: Severity = "risk" if quota_remaining == _ZERO else "warn"
        alerts.append(Alert(
            id="quota_low", sev=sev, rule="quota_low", title="LLM 額度偏低",
            detail=f"remaining {quota_remaining} < {quota_threshold}", href="/settings"))

    return alerts


def compute_alerts(
    conn: sqlite3.Connection, *, now: datetime, reporting: Currency
) -> list[Alert]:
    """Build the dashboard once, read rules + quota, run the single rule core."""
    data = build_dashboard(conn, now=now, reporting=reporting)
    return compute_alerts_from(
        data, get_alert_rules(conn),
        quota_remaining=budget_remaining(conn),
        quota_threshold=get_alert_threshold(conn),
    )
