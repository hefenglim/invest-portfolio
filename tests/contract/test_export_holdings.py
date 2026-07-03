import sqlite3

from fastapi.testclient import TestClient


def test_export_holdings_csv(api_client: TestClient) -> None:
    r = api_client.post("/api/export/holdings")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert "holdings_snapshot_2026-06-11.csv" in r.headers["content-disposition"]
    body = r.content
    assert body[:3] == b"\xef\xbb\xbf"
    text = body[3:].decode("utf-8")
    header = text.split("\r\n", 1)[0]
    assert header.startswith("symbol,name,market,board,account_id,quote_ccy,shares")
    assert "reporting_ccy_value" in header
    assert "\r\n2330," in text  # the 2330 holding row (symbol is the first column)
    assert "# as_of=2026-06-11" in text and "fx_rates=" in text


def test_export_holdings_audit_in_action_log(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """2026-07-03: exports audit via 系統操作記錄, no job_runs rows (user decision)."""
    api_client.post("/api/export/holdings")
    row = golden_db.execute(
        "SELECT * FROM job_runs WHERE job_id = 'export:holdings'"
    ).fetchone()
    assert row is None
    log = api_client.get("/api/system-log", params={"limit": 20}).json()["rows"]
    assert any(x["action"] == "匯出報表" and x["path"] == "/api/export/holdings"
               for x in log)
