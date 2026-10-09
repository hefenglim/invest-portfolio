"""DEF-030: the background workers finalize every run row, in a sentence the owner can read.

The measured symptom was run #153 ``status=error, detail="'insight:10'"`` — ``str(KeyError)``,
which is a Python repr of the dict key, written verbatim into the 排程中心 status chip. Two
worker properties are pinned here, on the static path and on the insight path alike:

* a failure's ``detail`` is 「執行失敗：<例外類別>：<訊息>」, never the bare ``str(exc)``;
* a pre-inserted ``running`` row is ALWAYS finalized — before the fix the insight worker
  returned silently when no runner was registered or the runner raised, leaving the row
  ``running`` forever (and the 409 overlap guard refusing every later run of the task).
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

from portfolio_dash.scheduler import jobs

NOW = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _own_session(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    @contextmanager
    def _session() -> Iterator[sqlite3.Connection]:
        yield conn

    monkeypatch.setattr(jobs, "session", _session)
    jobs.register_insight_runner(None)
    yield
    jobs.register_insight_runner(None)


def _row(conn: sqlite3.Connection, run_id: int) -> Any:
    row = conn.execute("SELECT * FROM job_runs WHERE id=?", (run_id,)).fetchone()
    assert row is not None
    return row


def test_failure_detail_names_the_exception_class_and_message() -> None:
    assert jobs.failure_detail(KeyError("insight:10")) == "執行失敗：KeyError：'insight:10'"
    assert jobs.failure_detail(RuntimeError("")) == "執行失敗：RuntimeError"


def test_static_job_failure_is_written_as_a_sentence(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(c: sqlite3.Connection, *, now: datetime) -> str:
        raise KeyError("x")

    monkeypatch.setattr(jobs, "_jobs_by_id", lambda: {
        "boom": jobs.JobSpec("boom", boom, "0 8 * * *", "Asia/Taipei", True, "d"),
    })
    rid = jobs.start_job_run(conn, "boom", now=NOW)
    jobs.run_job_func("boom", now=NOW)
    row = _row(conn, rid)
    assert row["status"] == "error"
    assert row["detail"] == "執行失敗：KeyError：'x'"
    # the synchronous door (run_job) writes the same sentence
    sync_id = jobs.run_job(conn, "boom", now=NOW)
    assert _row(conn, sync_id)["detail"] == "執行失敗：KeyError：'x'"


def test_manual_worker_on_an_insight_row_dispatches_to_the_insight_runner(
    conn: sqlite3.Connection
) -> None:
    jobs.bind_insight_schedule(conn, 10, cron="0 8 * * *")
    seen: list[tuple[int, int | None, str]] = []

    def runner(c: sqlite3.Connection, insight_type_id: int, *, now: datetime,
               run_id: int | None = None, trigger: Any = None) -> None:
        seen.append((insight_type_id, run_id, trigger.source))
        jobs.finish_job_run(c, run_id or 0, status="ok", detail="done", now=now)

    jobs.register_insight_runner(runner)
    rid = jobs.start_run(conn, "insight:10", now=NOW)
    jobs.run_job_func("insight:10", now=NOW)
    assert seen == [(10, rid, "manual")]
    assert _row(conn, rid)["status"] == "ok"
    assert _row(conn, rid)["payload"] == "10"


def test_manual_worker_finalizes_the_row_when_the_runner_raises(
    conn: sqlite3.Connection
) -> None:
    jobs.bind_insight_schedule(conn, 11, cron="0 8 * * *")

    def runner(c: sqlite3.Connection, insight_type_id: int, **kw: object) -> None:
        raise ValueError("provider down")

    jobs.register_insight_runner(runner)
    rid = jobs.start_run(conn, "insight:11", now=NOW)
    jobs.run_job_func("insight:11", now=NOW)
    row = _row(conn, rid)
    assert row["finished_at"] is not None
    assert row["status"] == "error"
    assert row["detail"] == "執行失敗：ValueError：provider down"


def test_task_door_worker_finalizes_the_row_when_no_runner_is_registered(
    conn: sqlite3.Connection
) -> None:
    rid = jobs.start_insight_run(conn, 12, now=NOW)
    jobs.run_insight_func(12, now=NOW, run_id=rid)
    row = _row(conn, rid)
    assert row["finished_at"] is not None, "a running row left behind blocks every later run"
    assert row["status"] == "error"
    assert "執行失敗" in row["detail"]


def test_manual_worker_finalizes_a_row_the_registry_cannot_run(
    conn: sqlite3.Connection
) -> None:
    rid = jobs.start_job_run(conn, "ghost_job", now=NOW)
    jobs.run_job_func("ghost_job", now=NOW)
    row = _row(conn, rid)
    assert row["status"] == "error" and row["finished_at"] is not None
    assert "找不到排程工作" in row["detail"] and "KeyError" not in row["detail"]


def test_cron_path_records_a_failed_insight_run(conn: sqlite3.Connection) -> None:
    # the cron path used to log the exception and write NOTHING, so a failing scheduled
    # task was invisible in the 排程中心 while the same failure via 立即執行 was a red row.
    jobs.bind_insight_schedule(conn, 13, cron="0 8 * * *")

    def runner(c: sqlite3.Connection, insight_type_id: int, **kw: object) -> None:
        raise ValueError("provider down")

    jobs.register_insight_runner(runner)
    jobs.dispatch_job(conn, "insight:13", now=NOW)
    row = conn.execute(
        "SELECT status, detail, payload FROM job_runs WHERE job_id='insight:13'"
    ).fetchone()
    assert row is not None
    assert row["status"] == "error"
    assert row["detail"] == "執行失敗：ValueError：provider down"
    assert row["payload"] == "13"


def test_cron_path_tells_the_runner_it_is_a_scheduled_run(conn: sqlite3.Connection) -> None:
    jobs.bind_insight_schedule(conn, 14, cron="0 8 * * *")
    seen: list[str] = []

    def runner(c: sqlite3.Connection, insight_type_id: int, *, now: datetime,
               trigger: Any = None, **kw: object) -> None:
        seen.append(trigger.source)

    jobs.register_insight_runner(runner)
    jobs.dispatch_job(conn, "insight:14", now=NOW)
    assert seen == ["schedule"]


def test_a_static_job_id_can_never_look_like_a_dynamic_one() -> None:
    """The ``insight:`` prefix is read as "kind=insight" in four places (the 排程中心 cost
    block, the task-run history query, two page links). That is sound ONLY while no static
    job id contains a colon — pinned here, so the whitelist of those readers stays true."""
    import re

    assert all(re.fullmatch(r"[a-z0-9_]+", j.id) for j in jobs.JOBS), [j.id for j in jobs.JOBS]
    assert jobs.insight_job_id(10) == "insight:10"
