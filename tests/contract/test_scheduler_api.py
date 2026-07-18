"""Contract tests for /api/scheduler/* (spec 15) — uses the golden_db + api_client.

The §15.3 background thread opens its OWN ``session()`` (the throwaway file DB from
the ``_safe_db`` fixture), so these tests assert only the synchronous running-row
insert on ``golden_db`` + the 202; the thread's completion is covered by the
``run_job_func`` unit path. ``daemon=True`` keeps the test process able to exit.
"""

import sqlite3

from fastapi.testclient import TestClient


def test_list_jobs_shape(api_client: TestClient, golden_db: sqlite3.Connection) -> None:
    r = api_client.get("/api/scheduler/jobs")
    assert r.status_code == 200
    jobs = r.json()["jobs"]
    ids = {j["id"] for j in jobs}
    assert {"quotes_tw", "quotes_us", "quotes_my"} <= ids
    tw = next(j for j in jobs if j["id"] == "quotes_tw")
    assert tw["cron"] and tw["tz"] == "Asia/Taipei" and tw["enabled"] is True
    assert tw["next"] is None  # no live scheduler in tests
    assert "last" in tw and "desc" in tw


def test_put_invalid_cron_400_no_write(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    r = api_client.put("/api/scheduler/jobs/quotes_tw", json={"cron": "not a cron"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_cron"
    assert r.json()["error"]["field"] == "cron"
    row = golden_db.execute(
        "SELECT cron FROM schedule_config WHERE job_id='quotes_tw'"
    ).fetchone()
    assert row["cron"] == "0 14 * * mon-fri"  # unchanged


def test_put_invalid_tz_400_field_tz(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    r = api_client.put("/api/scheduler/jobs/quotes_tw", json={"tz": "Mars/Phobos"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_cron"
    assert r.json()["error"]["field"] == "tz"


def test_put_valid_tz_bad_cron_field_cron(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    # Senior-review IMPORTANT-2: a valid tz + invalid cron must blame "cron", not "tz".
    r = api_client.put(
        "/api/scheduler/jobs/quotes_tw",
        json={"tz": "Asia/Kuala_Lumpur", "cron": "not a cron"},
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_cron"
    assert r.json()["error"]["field"] == "cron"
    row = golden_db.execute(
        "SELECT cron, timezone FROM schedule_config WHERE job_id='quotes_tw'"
    ).fetchone()
    assert row["cron"] == "0 14 * * mon-fri" and row["timezone"] == "Asia/Taipei"  # no write


def test_put_updates_row(api_client: TestClient, golden_db: sqlite3.Connection) -> None:
    r = api_client.put(
        "/api/scheduler/jobs/quotes_tw",
        json={"cron": "30 17 * * mon-fri", "tz": "Asia/Kuala_Lumpur"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["cron"] == "30 17 * * mon-fri" and body["tz"] == "Asia/Kuala_Lumpur"
    assert body["id"] == "quotes_tw"
    row = golden_db.execute(
        "SELECT cron, timezone FROM schedule_config WHERE job_id='quotes_tw'"
    ).fetchone()
    assert row["cron"] == "30 17 * * mon-fri" and row["timezone"] == "Asia/Kuala_Lumpur"


def test_put_enabled_toggle(api_client: TestClient, golden_db: sqlite3.Connection) -> None:
    r = api_client.put("/api/scheduler/jobs/quotes_tw", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    row = golden_db.execute(
        "SELECT enabled FROM schedule_config WHERE job_id='quotes_tw'"
    ).fetchone()
    assert row["enabled"] == 0


def test_put_unknown_404(api_client: TestClient) -> None:
    assert api_client.put("/api/scheduler/jobs/nope", json={"enabled": False}).status_code == 404


def test_run_already_running_409(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    golden_db.execute(
        "INSERT INTO job_runs (job_id, started_at) VALUES ('quotes_tw','2026-06-11T14:00:00+08:00')"
    )
    golden_db.commit()
    r = api_client.post("/api/scheduler/jobs/quotes_tw/run")
    assert r.status_code == 409 and r.json()["error"]["code"] == "already_running"


def test_run_unknown_404(api_client: TestClient) -> None:
    assert api_client.post("/api/scheduler/jobs/nope/run").status_code == 404


def test_run_202_inserts_row(
    api_client: TestClient, golden_db: sqlite3.Connection, monkeypatch: object
) -> None:
    # hermetic: stub the job func so the bg thread does no network
    import portfolio_dash.scheduler.jobs as J

    monkeypatch.setattr(  # type: ignore[attr-defined]
        J,
        "_jobs_by_id",
        lambda: {
            "quotes_tw": J.JobSpec(
                "quotes_tw",
                lambda conn, *, now: "stub",
                "0 14 * * mon-fri",
                "Asia/Taipei",
                True,
                "d",
            )
        },
    )
    r = api_client.post("/api/scheduler/jobs/quotes_tw/run")
    assert r.status_code == 202
    body = r.json()
    assert body["job_id"] == "quotes_tw" and isinstance(body["run_id"], int)
    row = golden_db.execute("SELECT * FROM job_runs WHERE id=?", (body["run_id"],)).fetchone()
    assert row is not None and row["started_at"]


def test_runs_history_and_limit(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    r = api_client.get("/api/scheduler/runs?limit=10")
    assert r.status_code == 200 and "rows" in r.json() and "total_count" in r.json()
    assert api_client.get("/api/scheduler/runs?limit=501").status_code == 400


def test_runs_row_shape_and_filter(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    golden_db.execute(
        "INSERT INTO job_runs (job_id, started_at, finished_at, status, detail) "
        "VALUES ('quotes_my','2026-06-10T17:30:06+08:00',"
        "'2026-06-10T17:30:36+08:00','error','boom')"
    )
    golden_db.commit()
    r = api_client.get("/api/scheduler/runs?job_id=quotes_my")
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert all(row["job_id"] == "quotes_my" for row in rows)
    row = rows[0]
    assert set(row) == {
        "id", "job_id", "started_at", "finished_at", "status", "detail",
        "duration_s", "cost_usd",
    }
    assert row["status"] == "error" and row["cost_usd"] is None
    assert row["duration_s"] == 30.0


def test_runs_unfinished_status_null(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    golden_db.execute(
        "INSERT INTO job_runs (job_id, started_at) VALUES ('quotes_us','2026-06-11T14:00:00+08:00')"
    )
    golden_db.commit()
    r = api_client.get("/api/scheduler/runs?job_id=quotes_us")
    row = r.json()["rows"][0]
    assert row["status"] is None and row["finished_at"] is None and row["duration_s"] is None


# --- FU-D36 status endpoint (需求七) -------------------------------------------


def test_status_shape_all_jobs_idle(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    r = api_client.get("/api/scheduler/status")
    assert r.status_code == 200
    body = r.json()
    assert body["active"] is False  # golden DB has no runs → nothing active
    jobs = body["jobs"]
    assert {"quotes_tw", "quotes_us", "quotes_my"} <= set(jobs)
    tw = jobs["quotes_tw"]
    assert set(tw) == {"running", "queued", "last_run"}
    assert tw["running"] is False and tw["queued"] is False and tw["last_run"] is None


def test_status_queued_when_unfinished_row_but_not_inflight(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    # A run row inserted (started_at set, finished_at NULL) but the worker has not marked
    # it running yet → 已排入. active flips True.
    golden_db.execute(
        "INSERT INTO job_runs (job_id, started_at, status) "
        "VALUES ('quotes_tw','2026-06-11T14:00:00+08:00','running')"
    )
    golden_db.commit()
    body = api_client.get("/api/scheduler/status").json()
    assert body["active"] is True
    tw = body["jobs"]["quotes_tw"]
    assert tw["queued"] is True and tw["running"] is False


def test_status_running_when_in_flight_registry(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    import portfolio_dash.scheduler.jobs as J

    golden_db.execute(
        "INSERT INTO job_runs (job_id, started_at, status) "
        "VALUES ('quotes_us','2026-06-11T14:00:00+08:00','running')"
    )
    golden_db.commit()
    J._mark_running("quotes_us")
    try:
        body = api_client.get("/api/scheduler/status").json()
    finally:
        J._clear_running("quotes_us")
    us = body["jobs"]["quotes_us"]
    assert us["running"] is True and us["queued"] is False and body["active"] is True


def test_status_last_run_reports_completed_error_with_message(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    golden_db.execute(
        "INSERT INTO job_runs (job_id, started_at, finished_at, status, detail) "
        "VALUES ('quotes_my','2026-06-10T17:30:06+08:00',"
        "'2026-06-10T17:30:36+08:00','error','provider timeout')"
    )
    golden_db.commit()
    body = api_client.get("/api/scheduler/status").json()
    my = body["jobs"]["quotes_my"]
    assert my["running"] is False and my["queued"] is False
    assert my["last_run"]["ok"] is False
    assert my["last_run"]["status"] == "error"
    assert my["last_run"]["message"] == "provider timeout"
    assert body["active"] is False  # a completed run is not active


def test_status_last_run_prefers_latest_completed_over_shadow(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    # A completed ok run, then a later shadow row: last_run must reflect the real run.
    golden_db.execute(
        "INSERT INTO job_runs (job_id, started_at, finished_at, status, detail, is_shadow) "
        "VALUES ('quotes_tw','2026-06-11T14:00:00+08:00','2026-06-11T14:00:05+08:00',"
        "'ok','3 ok, 0 failed',0)"
    )
    golden_db.execute(
        "INSERT INTO job_runs (job_id, started_at, finished_at, status, detail, is_shadow) "
        "VALUES ('quotes_tw','2026-06-11T14:01:00+08:00','2026-06-11T14:01:05+08:00',"
        "'error','shadow noise',1)"
    )
    golden_db.commit()
    tw = api_client.get("/api/scheduler/status").json()["jobs"]["quotes_tw"]
    assert tw["last_run"]["ok"] is True and tw["last_run"]["message"] == "3 ok, 0 failed"
