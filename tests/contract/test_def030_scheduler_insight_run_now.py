"""DEF-030 (functional test H-04, 2026-09-23): 排程中心 on an ``insight:<id>`` row.

Measured on the demo: 洞察管線 created a scheduled task (``insight:10``); 系統設定 › 排程中心 ›
insight:10 › 立即執行 answered ``202 {run_id 153}`` and a few seconds later the row read
「失敗 'insight:10'」 — run #153 ``status=error``, ``detail="'insight:10'"``, 0.0 s. The async
door's worker (``scheduler/jobs.py::run_job_func``) looked the id up in the STATIC registry
(``_jobs_by_id()[job_id]``) — which by construction never holds a dynamic ``kind=insight``
row — and wrote the bare ``KeyError`` text into ``job_runs.detail``. The cron path
(``dispatch_job``) had always dispatched by ``kind``; the manual path never did.

Three more facts from the same row: its name was the raw id (``insight:10``, the
description fallback), a PAUSED task's schedule toggle still read 「啟用」 (``enabled`` is the
schedule row's intent; the task's own ``enabled`` lived elsewhere and nothing combined
them), and a ``schedule_config`` row the registry cannot run was accepted with a 202 and
failed in the background instead of being refused up front.

The background thread is made synchronous and pointed at the test's own database, so these
tests exercise the REAL worker (``run_job_func``) rather than asserting only the 202.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.api.routers import scheduler as sched_router
from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.llm_insight import generate
from portfolio_dash.scheduler import jobs


class _SyncThread:
    """A ``threading.Thread`` stand-in that runs its target inline on ``start()``."""

    def __init__(self, *, target: Any, kwargs: dict[str, Any], daemon: bool) -> None:
        self._target = target
        self._kwargs = kwargs

    def start(self) -> None:
        self._target(**self._kwargs)


@pytest.fixture
def inline_worker(
    golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Run the router's background worker inline, on the test's own database."""

    @contextmanager
    def _session() -> Iterator[sqlite3.Connection]:
        yield golden_db

    monkeypatch.setattr(jobs, "session", _session)
    monkeypatch.setattr(sched_router, "threading", SimpleNamespace(Thread=_SyncThread))
    yield
    jobs.register_insight_runner(None)


def _task(conn: sqlite3.Connection, *, name: str, enabled: bool = True) -> int:
    cs.ensure_seeded(conn)
    it = cs.create_insight_type(
        conn, name=name, scope="portfolio", enabled=enabled,
        now=datetime(2026, 9, 23, 9, 0),
    )
    jobs.bind_insight_schedule(conn, it.id, cron="0 8 * * *")
    return it.id


def test_run_now_on_an_insight_row_runs_the_insight_runner_and_finishes_ok(
    api_client: TestClient, golden_db: sqlite3.Connection, inline_worker: None
) -> None:
    it_id = _task(golden_db, name="每日持倉週報")
    seen: list[tuple[int, int | None, Any]] = []

    def runner(c: sqlite3.Connection, insight_type_id: int, *, now: datetime,
               run_id: int | None = None, trigger: Any = None) -> None:
        seen.append((insight_type_id, run_id, trigger))
        # what the real runner does at the end of a run: finalize the pre-inserted row
        generate._write_job_run(c, insight_type_id, status="ok", reason="",
                                cost=Decimal("0"), now=now, run_id=run_id)

    jobs.register_insight_runner(runner)
    r = api_client.post(f"/api/scheduler/jobs/insight:{it_id}/run")
    assert r.status_code == 202, r.text
    run_id = r.json()["run_id"]
    row = golden_db.execute(
        "SELECT job_id, status, detail, payload, finished_at FROM job_runs WHERE id=?",
        (run_id,),
    ).fetchone()
    assert row["status"] == "ok", dict(row)
    assert row["finished_at"] is not None
    assert row["payload"] == str(it_id)          # the same row shape the cron path writes
    assert "KeyError" not in (row["detail"] or "")
    # the runner finalized THIS row (not a second one) and was told it was a manual run
    assert len(seen) == 1 and seen[0][0] == it_id and seen[0][1] == run_id
    assert seen[0][2] is not None and seen[0][2].source == "manual"
    n = golden_db.execute(
        "SELECT COUNT(*) AS n FROM job_runs WHERE job_id=?", (f"insight:{it_id}",)
    ).fetchone()["n"]
    assert n == 1


def test_run_now_refuses_a_row_the_scheduler_cannot_run_with_404_zh(
    api_client: TestClient, golden_db: sqlite3.Connection, inline_worker: None
) -> None:
    # a schedule_config row that is neither a registered static job nor a kind=insight
    # binding (a job removed from the registry, a hand-edited row) used to be accepted with a
    # 202 and fail in the background with the bare KeyError text.
    golden_db.execute(
        "INSERT INTO schedule_config (job_id, enabled, cron, timezone) "
        "VALUES ('ghost_job', 1, '0 8 * * *', 'Asia/Taipei')"
    )
    golden_db.commit()
    r = api_client.post("/api/scheduler/jobs/ghost_job/run")
    assert r.status_code == 404, r.text
    err = r.json()["error"]
    assert err["code"] == "not_found"
    assert "找不到" in err["message"] and "ghost_job" in err["message"]
    assert golden_db.execute(
        "SELECT COUNT(*) AS n FROM job_runs WHERE job_id='ghost_job'"
    ).fetchone()["n"] == 0
    # an id with no row at all is refused the same way, in the same words
    r2 = api_client.post("/api/scheduler/jobs/nope/run")
    assert r2.status_code == 404 and "找不到" in r2.json()["error"]["message"]


def test_run_now_on_a_paused_task_is_refused_like_the_task_door(
    api_client: TestClient, golden_db: sqlite3.Connection, inline_worker: None
) -> None:
    it_id = _task(golden_db, name="暫停中的任務", enabled=False)
    via_scheduler = api_client.post(f"/api/scheduler/jobs/insight:{it_id}/run")
    via_task = api_client.post(f"/api/insight-tasks/{it_id}/run")
    assert via_scheduler.status_code == via_task.status_code == 409
    assert via_scheduler.json()["error"] == via_task.json()["error"]
    assert via_scheduler.json()["error"]["code"] == "task_disabled"


def test_jobs_list_names_the_insight_row_and_reports_the_effective_state(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    live = _task(golden_db, name="每日持倉週報")
    paused = _task(golden_db, name="暫停中的任務", enabled=False)
    jobs_by_id = {j["id"]: j for j in api_client.get("/api/scheduler/jobs").json()["jobs"]}

    row = jobs_by_id[f"insight:{live}"]
    assert "每日持倉週報" in row["label"]
    assert row["kind"] == "insight"
    assert row["enabled"] is True and row["effective_enabled"] is True
    assert row["paused_reason"] is None

    prow = jobs_by_id[f"insight:{paused}"]
    assert "暫停中的任務" in prow["label"]
    assert prow["enabled"] is True            # the schedule row's own intent, unchanged
    assert prow["effective_enabled"] is False  # …but the task will not execute
    assert prow["paused_reason"] and "暫停" in prow["paused_reason"]
    assert prow["next"] is None

    static = jobs_by_id["quotes_tw"]
    assert static["kind"] == "system" and static["label"] is None
    assert static["effective_enabled"] is True and static["paused_reason"] is None


def test_runs_history_names_insight_runs_by_their_task(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    it_id = _task(golden_db, name="每日持倉週報")
    jobs.start_insight_run(golden_db, it_id, now=datetime(2026, 9, 23, 9, 0))
    rows = api_client.get(
        "/api/scheduler/runs", params={"job_id": f"insight:{it_id}"}
    ).json()["rows"]
    assert rows and "每日持倉週報" in rows[0]["label"]
    quotes = api_client.get("/api/scheduler/runs", params={"job_id": "quotes_tw"}).json()
    assert all(r["label"] is None for r in quotes["rows"])
