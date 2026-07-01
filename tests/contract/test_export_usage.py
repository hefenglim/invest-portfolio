import sqlite3

from fastapi.testclient import TestClient


def test_export_job_runs_csv(api_client: TestClient) -> None:
    api_client.post("/api/export/holdings")  # seed one job_runs row (export:holdings)
    r = api_client.post("/api/export/job-runs", json={})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "job_runs_all_all.csv" in r.headers["content-disposition"]
    text = r.content[3:].decode("utf-8")
    assert text.split("\r\n", 1)[0] == "id,job_id,started_at,finished_at,status,detail"
    assert "export:holdings" in text


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


def test_export_usage_writes_audit_rows(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    api_client.post("/api/export/llm-usage", json={})
    api_client.post("/api/export/job-runs", json={})
    kinds = {row["job_id"] for row in golden_db.execute(
        "SELECT job_id FROM job_runs WHERE job_id LIKE 'export:%'")}
    assert "export:llm_usage" in kinds and "export:job_runs" in kinds
