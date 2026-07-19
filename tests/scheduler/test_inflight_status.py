"""FU-D36: the in-flight registry wrapper marks 執行中 during a run and clears after.

The status endpoint keys 執行中 vs 成功/失敗 off :func:`running_job_ids`. These unit tests
pin the wrapper invariants both execution paths rely on:
  * ``run_job`` (cron/sync) marks the job WHILE its func runs and clears it AFTER the row is
    finalized (so a status poll never reads a finished run as 已排入),
  * the mark is cleared on the FAILURE path too (the ``finally``), and the error message is
    recorded to ``job_runs.detail``,
  * ``run_job_func`` (manual/async daemon target) marks then clears the same way.
"""

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

import portfolio_dash.scheduler.jobs as J
from portfolio_dash.scheduler.jobs import (
    create_scheduler_tables,
    run_job,
    run_job_func,
    running_job_ids,
    start_job_run,
)

NOW = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    """Guarantee cross-test isolation of the process-global in-flight set."""
    J._INFLIGHT_JOBS.clear()
    yield
    J._INFLIGHT_JOBS.clear()


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    create_scheduler_tables(c)
    return c


def _one_spec(fn: Callable[..., str]) -> dict[str, J.JobSpec]:
    return {"probe": J.JobSpec("probe", fn, "0 0 * * *", "Asia/Taipei", True, "probe job")}


def test_run_job_marks_inflight_during_func_and_clears_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _conn()
    seen: dict[str, Any] = {}

    def fn(_c: sqlite3.Connection, *, now: datetime) -> str:
        seen["during"] = "probe" in running_job_ids()
        return "ok detail"

    monkeypatch.setattr(J, "_jobs_by_id", lambda: _one_spec(fn))
    assert running_job_ids() == set()  # idle before
    rid = run_job(conn, "probe", now=NOW)
    assert seen["during"] is True          # marked 執行中 while the func ran
    assert running_job_ids() == set()      # cleared after the row was finalized
    row = conn.execute(
        "SELECT status, detail, finished_at FROM job_runs WHERE id=?", (rid,)
    ).fetchone()
    assert row["status"] == "ok" and row["detail"] == "ok detail" and row["finished_at"]


def test_run_job_clears_inflight_and_records_message_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _conn()

    def fn(_c: sqlite3.Connection, *, now: datetime) -> str:
        raise RuntimeError("boom in the job")

    monkeypatch.setattr(J, "_jobs_by_id", lambda: _one_spec(fn))
    rid = run_job(conn, "probe", now=NOW)
    assert running_job_ids() == set()      # cleared even on failure (the finally)
    row = conn.execute("SELECT status, detail FROM job_runs WHERE id=?", (rid,)).fetchone()
    assert row["status"] == "error" and "boom in the job" in row["detail"]


def test_run_job_func_marks_then_clears(monkeypatch: pytest.MonkeyPatch) -> None:
    """The manual/async daemon target finalizes the pre-inserted 'running' row + clears."""
    conn = _conn()
    start_job_run(conn, "probe", now=NOW)  # the 已排入 row the /run endpoint inserts
    seen: dict[str, Any] = {}

    def fn(_c: sqlite3.Connection, *, now: datetime) -> str:
        seen["during"] = "probe" in running_job_ids()
        return "done"

    @contextmanager
    def _fake_session() -> Iterator[sqlite3.Connection]:
        yield conn  # reuse the test conn (do NOT close — the assertions read it after)

    monkeypatch.setattr(J, "_jobs_by_id", lambda: _one_spec(fn))
    monkeypatch.setattr(J, "session", _fake_session)

    run_job_func("probe", now=NOW)
    assert seen["during"] is True
    assert running_job_ids() == set()
    row = conn.execute(
        "SELECT status, detail, finished_at FROM job_runs "
        "WHERE job_id='probe' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["status"] == "ok" and row["detail"] == "done" and row["finished_at"]


def test_running_job_ids_returns_a_snapshot_copy() -> None:
    """Callers get a copy — mutating it must not corrupt the live registry."""
    J._mark_running("x")
    try:
        snap = running_job_ids()
        snap.add("y")
        assert "y" not in running_job_ids()  # snapshot is isolated
        assert running_job_ids() == {"x"}
    finally:
        J._clear_running("x")


# --- FU-D46: progress messages on the in-flight registry -----------------------


def test_registry_value_shape_since_plus_progress() -> None:
    """The registry value carries {since, progress}; progress starts None."""
    J._mark_running("p")
    try:
        entry = J._INFLIGHT_JOBS["p"]
        assert entry.since  # ISO stamp recorded at mark time
        assert entry.progress is None
        assert J.running_progress() == {"p": None}
    finally:
        J._clear_running("p")


def test_set_progress_noop_when_job_not_running() -> None:
    """set_progress on an unmarked job must not create an entry (strict no-op)."""
    J.set_progress("ghost", "回補 2330 (1/3)")
    assert running_job_ids() == set()
    assert J.running_progress() == {}


def test_set_progress_visible_then_cleared_with_run() -> None:
    """Progress is readable while marked and dies with _clear_running (never outlives)."""
    J._mark_running("p")
    J.set_progress("p", "回補 2330 (3/18)")
    assert J.running_progress() == {"p": "回補 2330 (3/18)"}
    J._clear_running("p")
    assert J.running_progress() == {}


def test_remark_resets_stale_progress() -> None:
    """A new run of the same job starts progress-less (no stale message leaks in)."""
    J._mark_running("p")
    J.set_progress("p", "old message")
    J._clear_running("p")
    J._mark_running("p")
    try:
        assert J.running_progress() == {"p": None}
    finally:
        J._clear_running("p")


def test_run_job_progress_observable_during_func(monkeypatch: pytest.MonkeyPatch) -> None:
    """A job func's set_progress is visible mid-run and gone after the row finalizes."""
    conn = _conn()
    seen: dict[str, Any] = {}

    def fn(_c: sqlite3.Connection, *, now: datetime) -> str:
        J.set_progress("probe", "步驟 1/2")
        seen["progress"] = J.running_progress().get("probe")
        return "ok detail"

    monkeypatch.setattr(J, "_jobs_by_id", lambda: _one_spec(fn))
    run_job(conn, "probe", now=NOW)
    assert seen["progress"] == "步驟 1/2"
    assert J.running_progress() == {}  # cleared with the run


def test_set_progress_thread_safety_hammer() -> None:
    """Concurrent mark/progress/clear across threads never corrupts the registry."""
    import threading
    import time

    stop = threading.Event()
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            while not stop.is_set():
                J._mark_running("hammer")
                J.set_progress("hammer", "msg")
                J.running_progress()
                running_job_ids()
                J._clear_running("hammer")
        except BaseException as exc:  # noqa: BLE001 — any thread failure fails the test
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    stop.set()
    for t in threads:
        t.join(timeout=5)
    J._clear_running("hammer")
    assert not errors
    assert "hammer" not in running_job_ids()


# --- FU-D46: news runner progress forwarding (additive seam) -------------------


def test_news_daily_forwards_progress_to_capable_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runner accepting `progress` gets a callback that feeds the registry."""
    conn = _conn()
    seen: dict[str, Any] = {}

    def runner(
        _c: sqlite3.Connection, *, now: datetime, progress: Callable[[str], None]
    ) -> dict[str, int]:
        progress("蒐集 2330 新聞（1/3）")
        seen["during"] = J.running_progress().get("news_daily")
        return {"organized": 1, "headline_only": 0, "skipped_existing": 0}

    monkeypatch.setattr(J, "_NEWS_RUNNER", runner)
    J._mark_running("news_daily")  # simulate the run wrapper owning the job
    try:
        detail = J.news_daily(conn, now=NOW)
    finally:
        J._clear_running("news_daily")
    assert seen["during"] == "蒐集 2330 新聞（1/3）"
    assert detail.startswith("news: organized 1")


def test_news_daily_legacy_runner_without_progress_still_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stub/legacy runner without the param is called exactly as before (no TypeError)."""
    def runner(_c: sqlite3.Connection, *, now: datetime) -> str:
        return "not a dict"

    monkeypatch.setattr(J, "_NEWS_RUNNER", runner)
    assert J.news_daily(_conn(), now=NOW) == "news pass complete"
