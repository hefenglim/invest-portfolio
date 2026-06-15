"""Risk-alert rule engine (spec 03 §3.1). Pure over DashboardData — the SINGLE source
for both GET /api/alerts and the dashboard payload's embedded alerts. Seven market/quota
rules plus ``calib_gap`` (spec 03/04 I1, the AI calibration-error rule). Degrades
silently when an input is absent (never raises on missing market data).

``calib_gap`` is the one rule whose signal lives outside DashboardData: the portfolio-
wide AI calibration error in PERCENTAGE POINTS is FED IN as ``calib_gap: Decimal | None``
(the gate-and-compute lives in ``api.insight_service.calibration_gap`` — strategy/ stays
pure and never imports llm_insight). The threshold (``rules.calib_gap.value``) is also in
pp, so the comparison is pp-vs-pp. None means "below the global min_samples gate" → the
rule degrades silently. ``calibration_regression`` (spec 04c) is a separate EVENT in
``alert_events``; it is intentionally NOT a rule here."""

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
    calib_gap: Decimal | None = None,
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

    # calib_gap: pp-vs-pp comparison (both `calib_gap` and the threshold are percentage
    # points). None → below the global min_samples gate → silent (no alert). Single global
    # alert (no symbol).
    if (rules.calib_gap.enabled and rules.calib_gap.value is not None
            and calib_gap is not None and calib_gap > rules.calib_gap.value):
        alerts.append(Alert(
            id="calib_gap", sev="warn", rule="calib_gap", title="AI 校準誤差偏高",
            detail=f"calibration error {calib_gap}pp > {rules.calib_gap.value}pp",
            href="/settings"))

    return alerts


def compute_alerts(
    conn: sqlite3.Connection, *, now: datetime, reporting: Currency,
    calib_gap: Decimal | None = None,
) -> list[Alert]:
    """Build the dashboard once, read rules + quota, run the single rule core.

    ``calib_gap`` (the portfolio-wide AI calibration error in pp) is FED IN by the api
    layer (``api.insight_service.calibration_gap`` — strategy/ never imports llm_insight).
    The scheduler's ``_compute_alerts_for_scan`` calls this WITHOUT ``calib_gap`` → None →
    no calib_gap alert in the scan; that is intentional/acceptable (the calib_gap rule is
    a dashboard/settings surface, not a scan trigger).
    """
    data = build_dashboard(conn, now=now, reporting=reporting)
    return compute_alerts_from(
        data, get_alert_rules(conn),
        quota_remaining=budget_remaining(conn),
        quota_threshold=get_alert_threshold(conn),
        calib_gap=calib_gap,
    )
