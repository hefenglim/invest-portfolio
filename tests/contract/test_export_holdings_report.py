"""Contract tests for POST /api/export/holdings-report.

The route builds a print-optimized, self-contained 持倉報告 from the CURRENT dashboard
snapshot. It takes an empty JSON body ({}); exports are user actions -> audited by the
系統操作記錄 middleware (匯出報表), never a job_runs row.
"""

import sqlite3

from fastapi.testclient import TestClient


def test_export_holdings_report_ok(api_client: TestClient) -> None:
    r = api_client.post("/api/export/holdings-report", json={})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    cd = r.headers["content-disposition"]
    assert "attachment" in cd
    assert "holdings-report-20260611-1430.html" in cd  # filename from GOLDEN_NOW
    doc = r.content.decode("utf-8")
    assert doc.lstrip().startswith("<!doctype html>")
    assert "持倉報告" in doc
    assert "持倉明細表" in doc


def test_export_holdings_report_filtered_by_account(api_client: TestClient) -> None:
    """Wave A3: the report follows the account chip. Golden = 2330 (tw_broker) + AAPL
    (schwab); filtering to tw_broker removes AAPL from the 持倉明細表, states the filter in
    the header, and annotates the whole-portfolio KPI/配置 sections 「（全組合）」."""
    r = api_client.post("/api/export/holdings-report", json={"account": "tw_broker"})
    assert r.status_code == 200
    doc = r.content.decode("utf-8")
    assert "2330" in doc and "TSMC" in doc     # tw_broker holding present in the table
    assert "AAPL" not in doc                    # schwab holding filtered out
    assert "篩選" in doc                          # filter statement in the header
    assert "KPI 摘要（全組合）" in doc              # whole-portfolio sections annotated
    assert "配置（全組合）" in doc


def test_export_holdings_report_unfiltered_is_full(api_client: TestClient) -> None:
    r = api_client.post("/api/export/holdings-report", json={})
    doc = r.content.decode("utf-8")
    assert "2330" in doc and "AAPL" in doc      # both holdings present
    assert "篩選" not in doc                       # no filter statement
    assert "全組合" not in doc                      # sections un-annotated (grand)


def test_export_holdings_report_audited_not_job_run(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    api_client.post("/api/export/holdings-report", json={})
    row = golden_db.execute(
        "SELECT * FROM job_runs WHERE job_id LIKE 'export:%'"
    ).fetchone()
    assert row is None
    log = api_client.get("/api/system-log", params={"limit": 20}).json()["rows"]
    assert any(
        x["action"] == "匯出報表" and x["path"] == "/api/export/holdings-report"
        for x in log
    )
