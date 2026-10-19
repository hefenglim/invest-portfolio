"""Job workers (PERF-D4; spec §16; plan §7.2): scheduled jobs in a child process.

- **The composition root** (``api/composition.bind_runtime``) binds every ``register_*`` seam
  of ``scheduler/jobs.py``; the lifespan and the job worker both call it.
- **Equality**: every child-dispatched job — every static job but the settlement jobs and the
  retired one, and a kind=insight task — runs in a REAL child (``python -m
  portfolio_dash.api.job_worker``) against a copy of a database booted by the real lifespan,
  with its network steps stubbed by ``tests/scheduler/job_worker_stubs.py`` (``PD_TEST_STUBS``),
  and the row it writes equals the row the in-process run writes on another copy with the
  same stubs: status, detail, reason, results, spend.
- **Where things run**: a manual 立即執行 goes to the child like its cron fire; the settlement
  jobs and the interactive refresh-quotes door stay in-process; the after-job hook brackets a
  child's run in the WEB process (asked before the spawn, called once the row is final).
- **Liveness**: a child that dies or is killed mid-run is finalised ``error``
  (「工作子程序異常結束」); boot finalises a ``running`` row whose ``job-<run_id>.lock`` is free
  and leaves one whose lock is held; a run waiting for the heavy gate reads as queued, with
  the server's sentence; a child's progress reaches ``running_progress()`` through its sidecar.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import shutil
import signal
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from pytest_socket import disable_socket, enable_socket

from portfolio_dash.api import composition, job_worker
from portfolio_dash.api import settlement_service as service
from portfolio_dash.api.app import create_app
from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.scheduler import jobs
from portfolio_dash.scheduler.jobs import (
    JOBS,
    RETIRED_JOB_IDS,
    WEB_PROCESS_JOB_IDS,
    JobOutcome,
    bind_insight_schedule,
    dispatch_job,
    run_job_func,
    start_job_run,
    start_run,
)
from portfolio_dash.shared import child_runner, proc_lock
from portfolio_dash.shared.config import get_settings
from portfolio_dash.shared.db import install_actor, job_actor, session
from tests.conftest import _seed_golden
from tests.scheduler import job_worker_stubs as stubs

NOW = datetime(2026, 6, 11, 22, 38, tzinfo=ZoneInfo("Asia/Taipei"))
_PKG = Path(jobs.__file__).resolve().parents[1]
_INSIGHT = "insight"  # the parameter that stands for the template's scheduled insight task
_ROW_COLUMNS = ("job_id", "status", "detail", "reason", "results", "payload", "cost_usd",
                "llm_calls", "tokens_in", "tokens_out", "is_shadow")
_JOBS_TREE = ast.parse(Path(jobs.__file__).read_text(encoding="utf-8"))
#: Every ``register_*`` seam of scheduler/jobs.py, read off the source.
_SEAMS = sorted(n.name for n in ast.walk(_JOBS_TREE)
                if isinstance(n, ast.FunctionDef) and n.name.startswith("register_"))
#: The module global each seam sets (its ``global`` statement).
_SEAM_GLOBALS = tuple(sorted({
    name for fn in ast.walk(_JOBS_TREE)
    if isinstance(fn, ast.FunctionDef) and fn.name.startswith("register_")
    for g in ast.walk(fn) if isinstance(g, ast.Global) for name in g.names
}))


# ------------------------------------------------------------------------------- fixtures


@contextmanager
def _seams_restored() -> Iterator[None]:
    """Every seam global of ``scheduler/jobs.py`` as it was, whatever the block bound."""
    saved = {name: getattr(jobs, name) for name in _SEAM_GLOBALS}
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(jobs, name, value)


@pytest.fixture(autouse=True)
def _isolated_jobs() -> Iterator[None]:
    with _seams_restored():
        jobs._INFLIGHT_JOBS.clear()
        jobs._CHILD_RUNS.clear()
        yield
    jobs._INFLIGHT_JOBS.clear()
    jobs._CHILD_RUNS.clear()
    jobs._SIDECAR = None
    get_settings.cache_clear()


def _drop_log_handlers() -> None:
    """The lifespan attaches the JSON-lines file handler to the root logger; take it off
    again, so the rest of the session does not write into this module's temp folder."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "name", None) == "portfolio_dash.jsonlines":
            root.removeHandler(handler)
            handler.close()


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    """A database booted by the REAL lifespan (every table a job reads), seeded with the
    golden ledger and one scheduled insight task. Returns ``(path, insight job id)``."""
    path = tmp_path_factory.mktemp("jobs_template") / "portfolio.db"
    with pytest.MonkeyPatch.context() as mp, _seams_restored():
        mp.setenv("DB_PATH", str(path))
        mp.setenv("PD_SETTLE", "off")
        get_settings.cache_clear()
        enable_socket()
        try:
            with TestClient(create_app()):
                pass
        finally:
            disable_socket(allow_unix_socket=True)
            _drop_log_handlers()
            service.reset_for_tests()
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            _seed_golden(conn)
            task = cs.create_insight_type(conn, name="排程測試", scope="portfolio", now=NOW)
            insight_job = bind_insight_schedule(conn, task.id, cron="0 9 * * *")
            conn.commit()
        finally:
            conn.close()
    get_settings.cache_clear()
    return path, insight_job


def _copy(template_path: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "portfolio.db"
    shutil.copyfile(template_path, dest)
    return dest


def _use_db(mp: pytest.MonkeyPatch, path: Path) -> None:
    mp.setenv("DB_PATH", str(path))
    get_settings.cache_clear()


def _child_mode(mp: pytest.MonkeyPatch, **stub: Any) -> None:
    """``PD_JOB_MODE=child`` with the worker registered and the stubs named for the child."""
    mp.setenv("PD_JOB_MODE", "child")
    for key, value in stubs.env(**stub).items():
        mp.setenv(key, value)
    composition.bind_runtime()
    jobs.register_after_job_hook(None)


def _row(path: Path, run_id: int | None = None, job_id: str | None = None) -> dict[str, Any]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        if run_id is not None:
            row = conn.execute("SELECT * FROM job_runs WHERE id = ?", (run_id,)).fetchone()
        else:
            row = conn.execute("SELECT * FROM job_runs WHERE job_id = ? ORDER BY id DESC "
                               "LIMIT 1", (job_id,)).fetchone()
        assert row is not None
        return dict(row)
    finally:
        conn.close()


def _dispatch(job_id: str) -> int | None:
    with session() as conn:
        install_actor(conn, job_actor(job_id))
        return dispatch_job(conn, job_id, now=NOW)


def _wait_until(cond: Callable[[], bool], timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.05)


def _spawns_refused(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    spawned: list[str] = []

    def refuse(module: str, *a: Any, **kw: Any) -> child_runner.ChildHandle:
        spawned.append(module)
        raise AssertionError(f"spawned {module}")

    monkeypatch.setattr(child_runner, "spawn", refuse)
    return spawned


# ------------------------------------------------------------------- the composition root


def test_bind_runtime_calls_every_register_seam_of_the_scheduler() -> None:
    """A new seam in ``scheduler/jobs.py`` that the composition root does not bind fails
    HERE, not as a job that silently does nothing in production."""
    src = Path(composition.__file__).read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "bind_runtime")
    called = {c.func.attr for c in ast.walk(fn)
              if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)}
    assert len(_SEAMS) >= 15, _SEAMS
    assert set(_SEAMS) - called == set()
    assert len(_SEAM_GLOBALS) == len(_SEAMS)  # one global per seam, found by the scan


def test_bind_runtime_leaves_no_seam_unbound() -> None:
    for name in _SEAM_GLOBALS:
        setattr(jobs, name, None)
    composition.bind_runtime()
    unbound = [name for name in _SEAM_GLOBALS if getattr(jobs, name) is None]
    assert unbound == []
    assert jobs.job_worker_module() == composition.JOB_WORKER_MODULE == job_worker.__name__


def test_the_worker_rebuilds_now_in_the_parents_zone() -> None:
    args = jobs._worker_args("quotes_tw", 7, NOW, "schedule")
    assert args == ["quotes_tw", "7", "--now", NOW.isoformat(), "--trigger", "schedule",
                    "--tz", "Asia/Taipei"]
    now = jobs.parse_worker_now(NOW.isoformat(), "Asia/Taipei")
    assert now == NOW and now.tzinfo == NOW.tzinfo and now.isoformat() == NOW.isoformat()


# ------------------------------------------------------------------- what runs where


def test_what_may_run_in_a_child(template: tuple[Path, str], tmp_path: Path,
                                 monkeypatch: pytest.MonkeyPatch) -> None:
    _, insight_job = template
    _use_db(monkeypatch, _copy(template[0], tmp_path))
    with session() as conn:
        for spec in JOBS:
            expected = spec.id not in WEB_PROCESS_JOB_IDS | RETIRED_JOB_IDS
            assert jobs.child_dispatchable(conn, spec.id) is expected, spec.id
        assert jobs.child_dispatchable(conn, insight_job)
        assert not jobs.child_dispatchable(conn, "no_such_job")
        # Inline mode, or no registered worker: nothing goes to a child.
        monkeypatch.setenv("PD_JOB_MODE", "child")
        jobs.register_job_worker(None)
        assert not jobs.runs_in_child(conn, "signal_scan")
        jobs.register_job_worker(composition.JOB_WORKER_MODULE)
        assert jobs.runs_in_child(conn, "signal_scan")
        monkeypatch.setenv("PD_JOB_MODE", "inline")
        assert not jobs.runs_in_child(conn, "signal_scan")


def test_the_settlement_jobs_and_the_refresh_door_stay_in_process(
    template: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_db(monkeypatch, _copy(template[0], tmp_path))
    _child_mode(monkeypatch)
    stubs.install(monkeypatch.setattr)
    spawned = _spawns_refused(monkeypatch)
    calls: list[str] = []

    def runner(conn: sqlite3.Connection, scope: str, *, now: datetime) -> JobOutcome:
        calls.append(scope)
        return JobOutcome("ok", "已啟動每晚例行結算 #3，於背景執行")

    jobs.register_settlement_runner(runner)
    _dispatch("settlement_nightly")
    with session() as conn:
        _, outcome = jobs.run_job_outcome(conn, "quotes_tw", now=NOW)  # the 立即更新報價 door
    assert calls == ["nightly"] and spawned == []
    assert outcome.status == "ok"


# ------------------------------------------------------------------- equality, job by job


_CHILD_JOBS = [s.id for s in JOBS if s.id not in WEB_PROCESS_JOB_IDS | RETIRED_JOB_IDS]


@pytest.mark.parametrize("job", [*_CHILD_JOBS, _INSIGHT])
def test_every_child_job_writes_the_row_the_in_process_run_writes(
    template: tuple[Path, str], tmp_path: Path, job: str,
) -> None:
    template_path, insight_job = template
    job_id = insight_job if job == _INSIGHT else job
    inline_db = _copy(template_path, tmp_path / "inline")
    child_db = _copy(template_path, tmp_path / "child")

    with pytest.MonkeyPatch.context() as mp:
        _use_db(mp, inline_db)
        mp.setenv("PD_JOB_MODE", "inline")
        stubs.install(mp.setattr)
        composition.bind_runtime()
        jobs.register_after_job_hook(None)
        _dispatch(job_id)
    inline = _row(inline_db, job_id=job_id)

    spawned: list[tuple[str, tuple[str, ...], bool]] = []
    real_spawn = child_runner.spawn

    def spawn(module: str, args: Any = (), **kw: Any) -> child_runner.ChildHandle:
        spawned.append((module, tuple(args), bool(kw.get("heavy"))))
        return real_spawn(module, args, **kw)

    with pytest.MonkeyPatch.context() as mp:
        _use_db(mp, child_db)
        _child_mode(mp)
        mp.setattr(child_runner, "spawn", spawn)
        _dispatch(job_id)
    child = _row(child_db, job_id=job_id)

    # It really ran in a heavy job worker child, on the row this process inserted.
    assert [(m, a[:2], h) for m, a, h in spawned] == [
        (composition.JOB_WORKER_MODULE, (job_id, str(child["id"])), True)]
    assert child["finished_at"] is not None
    assert child["detail"] not in (jobs.WORKER_DIED, jobs.WORKER_NOT_STARTED), child
    assert {k: child[k] for k in _ROW_COLUMNS} == {k: inline[k] for k in _ROW_COLUMNS}
    # The child is gone and forgot nothing behind it.
    assert jobs._CHILD_RUNS == {}
    assert not list(child_db.parent.glob("job-*.lock"))
    assert not list(child_db.parent.glob("job-*.progress.json"))


# ------------------------------------------------------------------- the manual door


def test_a_manual_run_goes_to_the_child_and_the_hook_brackets_it_here(
    template: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _copy(template[0], tmp_path)
    _use_db(monkeypatch, db)
    _child_mode(monkeypatch, mode="pid", job_id="signal_scan")
    events: list[tuple[str, str, Any]] = []

    def hook(job_id: str) -> Callable[[], None]:
        events.append(("before", job_id, os.getpid()))

        def after() -> None:
            events.append(("after", job_id, _row(db, run_id=run_id)["finished_at"]))

        return after

    jobs.register_after_job_hook(hook)
    with session() as conn:
        run_id = start_run(conn, "signal_scan", now=NOW)
    run_job_func("signal_scan", now=NOW)  # the 立即執行 thread's target, run here
    row = _row(db, run_id=run_id)
    assert row["status"] == "ok"
    pid = int(row["detail"].removeprefix("程序 "))
    assert pid != os.getpid()  # the job ran in another process ...
    assert events[0] == ("before", "signal_scan", os.getpid())  # ... the hook in this one,
    assert events[1][:2] == ("after", "signal_scan") and events[1][2] is not None  # after it


def test_a_cron_fire_brackets_the_child_with_the_hook_too(
    template: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _copy(template[0], tmp_path)
    _use_db(monkeypatch, db)
    _child_mode(monkeypatch, mode="pid", job_id="signal_scan")
    seen: list[str | None] = []
    jobs.register_after_job_hook(
        lambda job_id: lambda: seen.append(_row(db, job_id=job_id)["finished_at"]))
    run_id = _dispatch("signal_scan")
    assert run_id is not None and seen == [_row(db, run_id=run_id)["finished_at"]]
    assert seen[0] is not None


# ------------------------------------------------------------------- liveness


def test_a_child_that_dies_mid_run_is_finalised_error(
    template: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _copy(template[0], tmp_path)
    _use_db(monkeypatch, db)
    _child_mode(monkeypatch, mode="die", job_id="signal_scan")
    run_id = _dispatch("signal_scan")
    assert run_id is not None
    row = _row(db, run_id=run_id)
    assert (row["status"], row["detail"], row["reason"]) == (
        "error", "工作子程序異常結束", "worker_died")
    assert row["finished_at"] is not None and row["cost_usd"] is None
    assert jobs.running_job_ids() == set() and jobs._CHILD_RUNS == {}


def test_a_child_killed_mid_run_is_finalised_error_and_its_progress_was_visible(
    template: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _copy(template[0], tmp_path)
    _use_db(monkeypatch, db)
    _child_mode(monkeypatch, mode="slow", job_id="signal_scan", sleep_s=120)
    with session() as conn:
        run_id = start_run(conn, "signal_scan", now=NOW)
    worker = threading.Thread(target=run_job_func, args=("signal_scan",),
                              kwargs={"now": NOW}, daemon=True)
    worker.start()
    # The child's set_progress reaches this process's status through the sidecar.
    _wait_until(lambda: jobs.running_progress().get("signal_scan") == stubs.STUB_PROGRESS)
    assert "signal_scan" in jobs.running_job_ids()
    assert "signal_scan" not in jobs.queued_progress()
    sidecar = jobs.progress_sidecar_path(run_id)
    pid = int(json.loads(sidecar.read_text(encoding="utf-8"))["pid"])
    assert pid != os.getpid()
    os.kill(pid, signal.SIGTERM)  # an external kill of the worker's own interpreter
    worker.join(60)
    assert not worker.is_alive()
    row = _row(db, run_id=run_id)
    assert (row["status"], row["detail"]) == ("error", "工作子程序異常結束")
    assert jobs.running_job_ids() == set() and jobs.running_progress() == {}
    assert not sidecar.exists()


def test_a_run_waiting_for_the_heavy_gate_reads_as_queued_with_the_servers_sentence(
    template: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _copy(template[0], tmp_path)
    _use_db(monkeypatch, db)
    _child_mode(monkeypatch, mode="pid", job_id="signal_scan")
    with session() as conn:
        run_id = start_run(conn, "signal_scan", now=NOW)
    gate = proc_lock.lock(proc_lock.heavy_child_lock_path(), timeout=5)  # another heavy child
    worker = threading.Thread(target=run_job_func, args=("signal_scan",),
                              kwargs={"now": NOW}, daemon=True)
    try:
        worker.start()
        _wait_until(lambda: run_id in jobs._CHILD_RUNS
                    and jobs._CHILD_RUNS[run_id].handle is not None)
        time.sleep(0.3)
        assert jobs.queued_progress() == {"signal_scan": jobs.QUEUED_BEHIND_GATE}
        assert "signal_scan" not in jobs.running_job_ids()
        assert _row(db, run_id=run_id)["finished_at"] is None
    finally:
        gate.release()
    worker.join(120)
    assert not worker.is_alive()
    assert _row(db, run_id=run_id)["status"] == "ok"
    assert jobs.queued_progress() == {}


def test_boot_closes_the_rows_no_live_process_owns(
    template: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _copy(template[0], tmp_path)
    _use_db(monkeypatch, db)
    data = proc_lock.data_dir()
    with session() as conn:
        orphan = start_job_run(conn, "signal_scan", now=NOW)
        live = start_job_run(conn, "news_daily", now=NOW)
        settle = start_job_run(conn, jobs.SETTLEMENT_JOB_ID, now=NOW)
        done = start_job_run(conn, "backup_daily", now=NOW)
        jobs.finish_job_run(conn, done, status="ok", detail="備份完成", now=NOW,
                            usage=None)
    stale_lock = proc_lock.job_lock_path(9999, data)
    stale_lock.write_text("", encoding="utf-8")
    stale_sidecar = jobs.progress_sidecar_path(9999, data)
    stale_sidecar.write_text("{}", encoding="utf-8")
    with proc_lock.lock(proc_lock.job_lock_path(live, data), timeout=5):
        with session() as conn:
            closed = jobs.close_abandoned_runs(conn, now=NOW)
        assert proc_lock.job_lock_path(live, data).exists()  # a held lock is never removed
    assert closed == [orphan]
    row = _row(db, run_id=orphan)
    assert (row["status"], row["detail"], row["reason"]) == (
        "error", "系統重新啟動時此工作尚未完成", "interrupted")
    assert _row(db, run_id=live)["finished_at"] is None
    assert _row(db, run_id=settle)["finished_at"] is None
    assert _row(db, run_id=done)["detail"] == "備份完成"
    assert not stale_lock.exists() and not stale_sidecar.exists()


def test_the_worker_never_runs_a_row_that_is_already_final(
    template: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_db(monkeypatch, _copy(template[0], tmp_path))
    ran: list[str] = []

    def func(conn: sqlite3.Connection, *, now: datetime) -> str:
        ran.append("x")
        return "完成"

    monkeypatch.setattr(jobs, "_jobs_by_id", lambda: {"signal_scan": jobs.JobSpec(
        "signal_scan", func, "0 0 * * *", "Asia/Taipei", True, "")})
    with session() as conn:
        run_id = start_job_run(conn, "signal_scan", now=NOW)
        jobs.finish_job_run(conn, run_id, status="error", detail="系統重新啟動時此工作尚未完成",
                            now=NOW, usage=None)
        assert jobs.run_in_worker(conn, "signal_scan", run_id, now=NOW, trigger="manual")
        assert not jobs.run_in_worker(conn, "news_daily", run_id, now=NOW, trigger="manual")
        fresh = start_job_run(conn, "signal_scan", now=NOW)
        assert jobs.run_in_worker(conn, "signal_scan", fresh, now=NOW, trigger="schedule")
    assert ran == ["x"]


def test_the_job_details_of_a_lost_worker_speak_chinese() -> None:
    from tests.contract.test_def073_job_details_speak_chinese import english_words

    for text in (jobs.WORKER_DIED, jobs.WORKER_NOT_STARTED, jobs.ABANDONED_AT_BOOT,
                 jobs.QUEUED_BEHIND_GATE):
        assert text and not english_words(text), text
