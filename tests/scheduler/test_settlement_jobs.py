"""The scheduler side of the settlement (plan §6.2 "Jobs", P3, P7, P11, P13).

- the two settlement jobs, their crons, and that they reach the settlement only through
  ``register_settlement_runner`` (P13) and never through a job child (P7);
- ``RETIRED_JOB_IDS``: ``snapshot_monthly`` is neither mounted nor listed, its run history
  stays, and the dynamic ``insight:<id>`` rows still mount (P11);
- the after-job hook brackets a static job's whole run (P13);
- a quote job's ``results`` reach ``job_runs.results`` (P3) — what ``final_through`` reads.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.pricing.results import RefreshSummary
from portfolio_dash.scheduler import jobs, runtime
from portfolio_dash.scheduler.jobs import (
    JOBS,
    RETIRED_JOB_IDS,
    WEB_PROCESS_JOB_IDS,
    JobOutcome,
    bind_insight_schedule,
    dispatch_job,
    ensure_scheduler_seeded,
    finish_job_run,
    migrate_job_runs_results,
    run_job_func,
    run_job_outcome,
    start_job_run,
)

NOW = datetime(2026, 10, 19, 6, 40, tzinfo=ZoneInfo("Asia/Taipei"))


@pytest.fixture(autouse=True)
def _seams(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(jobs, "_SETTLEMENT_RUNNER", None)
    monkeypatch.setattr(jobs, "_AFTER_JOB_HOOK", None)
    jobs._INFLIGHT_JOBS.clear()
    yield
    jobs._INFLIGHT_JOBS.clear()


def _spec(job_id: str) -> jobs.JobSpec:
    return {s.id: s for s in JOBS}[job_id]


def test_the_two_settlement_jobs_and_their_schedules() -> None:
    nightly, selfcheck = _spec("settlement_nightly"), _spec("settlement_selfcheck")
    assert (nightly.default_cron, nightly.default_timezone) == ("40 6 * * *", "Asia/Taipei")
    assert (selfcheck.default_cron, selfcheck.default_timezone) == ("30 4 * * sun",
                                                                    "Asia/Taipei")
    assert nightly.default_enabled and selfcheck.default_enabled
    assert WEB_PROCESS_JOB_IDS == {"settlement_nightly", "settlement_selfcheck"}
    assert jobs.SETTLEMENT_JOB_ID not in {s.id for s in JOBS}  # the run's row, not a job


def test_a_settlement_job_reaches_the_settlement_only_through_its_runner(
    conn: sqlite3.Connection,
) -> None:
    calls: list[tuple[str, datetime]] = []

    def runner(c: sqlite3.Connection, scope: str, *, now: datetime) -> JobOutcome:
        calls.append((scope, now))
        return JobOutcome("ok", "已啟動每晚例行結算 #3，於背景執行")

    _, idle = run_job_outcome(conn, "settlement_nightly", now=NOW)
    assert (idle.status, idle.detail) == ("ok", "重新結算執行器未接線，未執行")
    jobs.register_settlement_runner(runner)
    try:
        _, nightly = run_job_outcome(conn, "settlement_nightly", now=NOW)
        _, weekly = run_job_outcome(conn, "settlement_selfcheck", now=NOW)
    finally:
        jobs.register_settlement_runner(None)
    assert calls == [("nightly", NOW), ("selfcheck", NOW)]
    assert nightly.detail == "已啟動每晚例行結算 #3，於背景執行"


def test_the_scheduler_layer_never_imports_the_api() -> None:
    from pathlib import Path

    src = Path(jobs.__file__).read_text(encoding="utf-8")
    assert "portfolio_dash.api" not in src
    assert "settlement_service" not in src.replace("api/settlement_service.py", "")


def test_a_retired_job_is_not_mounted_but_an_insight_task_still_is(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    ensure_scheduler_seeded(conn)
    bind_insight_schedule(conn, 5, cron="0 9 * * *")
    # a database from before W4 keeps the retired job's row (ensure_job_rows never deletes)
    conn.execute("INSERT OR IGNORE INTO schedule_config (job_id, cron, timezone, enabled) "
                 "VALUES ('snapshot_monthly', '50 23 * * *', 'Asia/Taipei', 1)")
    assert "snapshot_monthly" in {
        str(r[0]) for r in conn.execute("SELECT job_id FROM schedule_config")}

    @contextlib.contextmanager
    def fake_session() -> Iterator[sqlite3.Connection]:
        yield conn

    monkeypatch.setattr(runtime, "session", fake_session)
    ids = {j.id for j in runtime.build_scheduler().get_jobs()}
    assert RETIRED_JOB_IDS == {"snapshot_monthly"}
    assert "snapshot_monthly" not in ids
    assert "insight:5" in ids
    assert {"settlement_nightly", "settlement_selfcheck", "quotes_tw"} <= ids


def test_a_stale_trigger_of_a_retired_job_runs_nothing(conn: sqlite3.Connection) -> None:
    ensure_scheduler_seeded(conn)
    assert dispatch_job(conn, "snapshot_monthly", now=NOW) is None
    assert conn.execute("SELECT COUNT(*) FROM job_runs").fetchone()[0] == 0


def test_the_listing_skips_a_retired_job_and_its_history_stays(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    golden_db.execute("INSERT INTO job_runs (job_id, started_at, finished_at, status, detail) "
                      "VALUES ('snapshot_monthly', '2026-06-01T23:50:00+08:00', "
                      "'2026-06-01T23:50:01+08:00', 'ok', '寫入本月 KPI 快照')")
    golden_db.commit()
    listed = {j["id"] for j in api_client.get("/api/scheduler/jobs").json()["jobs"]}
    assert "snapshot_monthly" not in listed
    assert {"settlement_nightly", "settlement_selfcheck"} <= listed
    assert "snapshot_monthly" not in api_client.get("/api/scheduler/status").json()["jobs"]
    runs = api_client.get("/api/scheduler/runs", params={"job_id": "snapshot_monthly"}).json()
    assert [r["detail"] for r in runs["rows"]] == ["寫入本月 KPI 快照"]


def _probe(order: list[str]) -> jobs.JobFunc:
    def run(c: sqlite3.Connection, *, now: datetime) -> str:
        order.append("run")
        return "完成"

    return run


def _hook(seen: list[str]) -> jobs.AfterJobHook:
    def begin(job_id: str) -> Any:
        seen.append(f"before:{job_id}")
        return lambda: seen.append(f"after:{job_id}")

    return begin


def test_the_after_job_hook_brackets_a_scheduled_static_job(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_scheduler_seeded(conn)
    order: list[str] = []
    monkeypatch.setattr(jobs, "_jobs_by_id", lambda: {
        **{s.id: s for s in JOBS},
        "probe": jobs.JobSpec("probe", _probe(order), "0 1 * * *", "Asia/Taipei", True,
                              "probe")})
    jobs.register_after_job_hook(_hook(order))
    try:
        dispatch_job(conn, "probe", now=NOW)
        dispatch_job(conn, "settlement_nightly", now=NOW)  # a settlement job: no hook
    finally:
        jobs.register_after_job_hook(None)
    assert order == ["before:probe", "run", "after:probe"]


def test_the_after_job_hook_brackets_a_manual_run_too(
    monkeypatch: pytest.MonkeyPatch, golden_db: sqlite3.Connection
) -> None:
    order: list[str] = []

    @contextlib.contextmanager
    def fake_session() -> Iterator[sqlite3.Connection]:
        yield golden_db

    monkeypatch.setattr(jobs, "session", fake_session)
    monkeypatch.setattr(jobs, "_jobs_by_id", lambda: {
        "probe": jobs.JobSpec("probe", _probe(order), "0 1 * * *", "Asia/Taipei", True,
                              "probe")})
    start_job_run(golden_db, "probe", now=NOW)
    jobs.register_after_job_hook(_hook(order))
    try:
        run_job_func("probe", now=NOW)
    finally:
        jobs.register_after_job_hook(None)
    assert order == ["before:probe", "run", "after:probe"]


def test_a_failing_hook_never_fails_the_job(conn: sqlite3.Connection,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(job_id: str) -> Any:
        raise RuntimeError("hook exploded")

    jobs.register_after_job_hook(broken)
    try:
        assert jobs._begin_after_job("quotes_tw") is None
        jobs._end_after_job("quotes_tw", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    finally:
        jobs.register_after_job_hook(None)


def test_a_quote_job_writes_its_results(conn: sqlite3.Connection,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    migrate_job_runs_results(conn)
    monkeypatch.setattr(jobs, "build_worklist", lambda c, market: (
        [jobs.InstrumentRef(symbol="2330", market=market)], []))
    monkeypatch.setattr(jobs, "default_registry", lambda c=None: None)
    monkeypatch.setattr(jobs, "refresh_quotes", lambda *a, **kw: RefreshSummary(
        ok={}, failed=["2330"], fetched_at=NOW))
    monkeypatch.setattr(jobs, "_HELD_SYMBOLS_FN", lambda c: {"2330"})
    run_id, outcome = run_job_outcome(conn, "quotes_tw", now=NOW)
    stored = json.loads(conn.execute("SELECT results FROM job_runs WHERE id = ?",
                                     (run_id,)).fetchone()[0])
    assert stored == json.loads(json.dumps(outcome.results))
    assert stored["held_failed"] == ["2330"]


def test_finish_job_run_takes_an_outcome_code_and_results(conn: sqlite3.Connection) -> None:
    migrate_job_runs_results(conn)
    run_id = start_job_run(conn, "settlement", now=NOW)
    finish_job_run(conn, run_id, status="skipped", detail="重新結算已取消，保留目前版本",
                   reason="cancelled", results={"days": 3}, now=NOW)
    row = conn.execute("SELECT status, reason, results, finished_at FROM job_runs WHERE id = ?",
                       (run_id,)).fetchone()
    assert (row[0], row[1], json.loads(row[2])) == ("skipped", "cancelled", {"days": 3})
    assert row[3] is not None


def test_results_are_dropped_not_fatal_without_the_boot_migration(
    conn: sqlite3.Connection,
) -> None:
    run_id = start_job_run(conn, "quotes_tw", now=NOW)
    finish_job_run(conn, run_id, status="ok", detail="完成", results={"held_failed": []},
                   now=NOW)
    assert conn.execute("SELECT status FROM job_runs WHERE id = ?",
                        (run_id,)).fetchone()[0] == "ok"


def test_start_job_run_can_leave_the_row_in_the_callers_transaction(
    conn: sqlite3.Connection,
) -> None:
    start_job_run(conn, "settlement", now=NOW, commit=False)
    assert conn.in_transaction
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM job_runs").fetchone()[0] == 0


def test_the_boot_migration_adds_the_schedule_stamp(conn: sqlite3.Connection) -> None:
    migrate_job_runs_results(conn)
    migrate_job_runs_results(conn)  # idempotent
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(schedule_config)")}
    assert "updated_at" in cols


def test_the_schedule_page_stamps_a_change(api_client: TestClient,
                                           golden_db: sqlite3.Connection) -> None:
    """Review E #2: the 排程 PUT stamps ``schedule_config.updated_at`` when the schedule
    changed (and only then), so final_through never re-judges older fires by the new cron."""
    ensure_scheduler_seeded(golden_db)

    def stamp() -> object:
        return golden_db.execute(
            "SELECT updated_at FROM schedule_config WHERE job_id = 'quotes_tw'").fetchone()[0]

    same = api_client.put("/api/scheduler/jobs/quotes_tw", json={"cron": "0 14 * * mon-fri"})
    assert same.status_code == 200, same.text
    assert stamp() is None
    moved = api_client.put("/api/scheduler/jobs/quotes_tw", json={"cron": "30 14 * * mon-fri"})
    assert moved.status_code == 200, moved.text
    assert str(stamp()).startswith("2026-06-11T14:30")  # the request's clock (GOLDEN_NOW)
