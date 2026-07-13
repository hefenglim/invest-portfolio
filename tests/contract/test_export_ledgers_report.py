"""Contract tests for POST /api/export/ledgers-report.

The route builds a print-optimized, self-contained 帳本報告 covering the four ledgers over
an optional {from, to} range — the SAME RangeBody + 400 validation as the other range
exports (from > to -> validation_error / field=from). Exports are user actions -> audited by
the 系統操作記錄 middleware (匯出報表).
"""

from fastapi.testclient import TestClient


def test_export_ledgers_report_ok_unbounded(api_client: TestClient) -> None:
    r = api_client.post("/api/export/ledgers-report", json={})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    cd = r.headers["content-disposition"]
    assert "attachment" in cd
    assert "ledger-report-20260611-1430.html" in cd  # filename from GOLDEN_NOW
    doc = r.content.decode("utf-8")
    assert doc.lstrip().startswith("<!doctype html>")
    assert "帳本報告" in doc
    assert "全部期間" in doc  # unbounded range label


def test_export_ledgers_report_bad_range_400(api_client: TestClient) -> None:
    """from > to is a 400 validation_error / field=from — same shape as the CSV range routes."""
    r = api_client.post(
        "/api/export/ledgers-report", json={"from": "2026-12-31", "to": "2026-01-01"}
    )
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["field"] == "from"


def test_export_ledgers_report_bounded_range_ok(api_client: TestClient) -> None:
    r = api_client.post(
        "/api/export/ledgers-report", json={"from": "2026-01-01", "to": "2026-06-30"}
    )
    assert r.status_code == 200
    doc = r.content.decode("utf-8")
    assert "2026-01-01 ～ 2026-06-30" in doc  # bounded range label in the header


def test_export_ledgers_report_audited(api_client: TestClient) -> None:
    api_client.post("/api/export/ledgers-report", json={})
    log = api_client.get("/api/system-log", params={"limit": 20}).json()["rows"]
    assert any(
        x["action"] == "匯出報表" and x["path"] == "/api/export/ledgers-report"
        for x in log
    )
