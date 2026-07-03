import sqlite3

from fastapi.testclient import TestClient


def test_export_job_runs_csv(api_client: TestClient) -> None:
    r = api_client.post("/api/export/job-runs", json={})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "job_runs_all_all.csv" in r.headers["content-disposition"]
    text = r.content[3:].decode("utf-8")
    assert text.split("\r\n", 1)[0] == "id,job_id,started_at,finished_at,status,detail"


def test_export_llm_usage_csv_empty(api_client: TestClient) -> None:
    r = api_client.post("/api/export/llm-usage",
                        json={"from": "2026-01-01", "to": "2026-12-31"})
    assert r.status_code == 200
    assert "llm_usage_2026-01-01_2026-12-31.csv" in r.headers["content-disposition"]
    text = r.content[3:].decode("utf-8")
    assert text.split("\r\n", 1)[0] == "ts,model,agent,input_tokens,output_tokens,cost"


def test_export_bad_range_400(api_client: TestClient) -> None:
    r = api_client.post("/api/export/job-runs",
                        json={"from": "2026-12-31", "to": "2026-01-01"})
    assert r.status_code == 400
    assert r.json()["error"]["field"] == "from"


def test_export_audits_in_action_log_not_job_runs(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """2026-07-03 (human decision): exports are USER ACTIONS — recorded by the
    系統操作記錄 middleware, no longer as job_runs rows (the 排程執行歷史 stays a
    pure scheduler view)."""
    api_client.post("/api/export/llm-usage", json={})
    api_client.post("/api/export/job-runs", json={})
    kinds = {row["job_id"] for row in golden_db.execute(
        "SELECT job_id FROM job_runs WHERE job_id LIKE 'export:%'")}
    assert kinds == set()  # no job_runs audit rows anymore
    log = api_client.get("/api/system-log", params={"limit": 50}).json()["rows"]
    assert sum(1 for x in log if x["action"] == "匯出報表") >= 2


def test_runs_view_excludes_legacy_export_rows(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    # simulate a LEGACY export audit row written before the change
    golden_db.execute(
        "INSERT INTO job_runs (job_id, started_at, finished_at, status, detail) "
        "VALUES ('export:holdings', '2026-07-01T00:00:00', '2026-07-01T00:00:00', "
        "'ok', 'legacy')")
    golden_db.commit()
    rows = api_client.get("/api/scheduler/runs", params={"limit": 200}).json()["rows"]
    assert all(not r["job_id"].startswith("export:") for r in rows)
