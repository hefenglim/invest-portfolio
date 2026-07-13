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
