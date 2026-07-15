"""Contract tests for POST /api/export/rebalance-report.

The route builds a print-optimized, self-contained HTML execution guide of the CURRENT
rebalance preview. Validation PARITY with POST /api/rebalance/preview: a negative target
ratio is a 400 validation_error. Exports are user actions -> audited by the 系統操作記錄
middleware (no job_runs row). The request wire shape is {"targets": {symbol: ratio-string}}.
"""

import sqlite3

from fastapi.testclient import TestClient

from tests.conftest import DashboardClientFactory, _seed_dual_account


def test_export_rebalance_report_ok(api_client: TestClient) -> None:
    r = api_client.post(
        "/api/export/rebalance-report", json={"targets": {"2330": "0.30", "AAPL": "0.70"}}
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    cd = r.headers["content-disposition"]
    assert "attachment" in cd
    assert "rebalance-plan-20260611-1430.html" in cd  # filename from GOLDEN_NOW
    doc = r.content.decode("utf-8")
    assert doc.lstrip().startswith("<!doctype html>")
    assert "再平衡試算執行指南" in doc


def test_export_rebalance_report_rebate_footnote(api_client: TestClient) -> None:
    """FE-D1: a TW sell leg surfaces the 預估次月折讓 footnote, clearly 不計入成本."""
    r = api_client.post(
        "/api/export/rebalance-report", json={"targets": {"2330": "0.30", "AAPL": "0.70"}}
    )
    assert r.status_code == 200
    doc = r.content.decode("utf-8")
    assert "預估次月折讓合計" in doc and "不計入成本" in doc


def test_export_rebalance_report_negative_ratio_400(api_client: TestClient) -> None:
    """Mirrors the preview route: a negative ratio is a 400 validation_error / field=targets."""
    r = api_client.post("/api/export/rebalance-report", json={"targets": {"2330": "-0.1"}})
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["field"] == "targets"


def test_export_rebalance_report_empty_targets_ok(api_client: TestClient) -> None:
    """No targets -> a valid document with the 目前無需任何交易 notice, still 200."""
    r = api_client.post("/api/export/rebalance-report", json={"targets": {}})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    doc = r.content.decode("utf-8")
    assert "目前無需任何交易" in doc


def test_export_rebalance_report_audit_in_action_log(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """Export audits via 系統操作記錄, not job_runs (same seam as the other exports)."""
    api_client.post(
        "/api/export/rebalance-report", json={"targets": {"2330": "0.30", "AAPL": "0.70"}}
    )
    row = golden_db.execute(
        "SELECT * FROM job_runs WHERE job_id LIKE 'export:%'"
    ).fetchone()
    assert row is None
    log = api_client.get("/api/system-log", params={"limit": 20}).json()["rows"]
    assert any(
        x["action"] == "匯出報表" and x["path"] == "/api/export/rebalance-report"
        for x in log
    )


def test_export_rebalance_report_dual_account(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """A dual-account symbol (AAPL: schwab + moomoo_my_us) renders both leg accounts and the
    constituent under 執行清單/摘要表, over the real API path."""
    client = dashboard_client_factory(_seed_dual_account)
    r = client.post(
        "/api/export/rebalance-report", json={"targets": {"2330": "0.6", "AAPL": "0.4"}}
    )
    assert r.status_code == 200
    doc = r.content.decode("utf-8")
    assert "Charles Schwab" in doc and "TW Broker" in doc
    assert "Moomoo MY (US)" in doc  # AAPL's second constituent
    assert "小計" in doc  # per-account subtotal
