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
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from pytest_socket import disable_socket, enable_socket

from portfolio_dash.api import composition, job_worker
from portfolio_dash.api import settlement_service as service
from portfolio_dash.api.app import create_app
from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
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
from portfolio_dash.shared.enums import Market
from portfolio_dash.shared.llm_config import (
    LLMRole,
    ModelConfig,
    add_topup,
    ensure_llm_seeded,
    set_role,
    upsert_model,
)
from tests.conftest import _seed_golden
from tests.scheduler import job_worker_stubs as stubs

NOW = datetime(2026, 6, 11, 22, 38, tzinfo=ZoneInfo("Asia/Taipei"))
_PKG = Path(jobs.__file__).resolve().parents[1]
_INSIGHT = "insight"  # the parameter that stands for the template's skipped insight task
_PAID = "paid"  # ... and for its generating one
#: The side-effect tables compared between the in-process and the child run, and the columns
#: left out (row ids and wall-clock stamps, which differ by construction).
_SIDE_EFFECTS = ("insights", "llm_usage", "settle_change_log")
_VOLATILE = {"id", "ts", "created_at", "written_at", "fetched_at"}
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


class Template(NamedTuple):
    path: Path
    #: A scheduled insight task its gates skip (no live strategy): a run that spends nothing.
    insight_job: str
    #: A scheduled insight task that GENERATES: a strategy, a priced default model and a
    #: top-up — every model call answered by the stub provider and booked into llm_usage.
    paid_job: str


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> Template:
    """A database booted by the REAL lifespan (every table a job reads), seeded with the
    golden ledger, a priced default model with a top-up, and two scheduled insight tasks
    (:class:`Template`)."""
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
            ensure_llm_seeded(conn)
            upsert_model(conn, ModelConfig(
                id="stub-m", model_alias="stub-m", provider="openai", model_name="stub-m",
                max_retries=0, input_price_per_mtok=Decimal("0.1"),
                output_price_per_mtok=Decimal("0.4")))
            set_role(conn, LLMRole.DEFAULT, "stub-m")
            add_topup(conn, Decimal("100"))
            strategy = cs.create_strategy(conn, name="S", body="觀察 {{kpis_json}}", now=NOW)
            paid = cs.create_insight_type(conn, name="付費測試", scope="portfolio", now=NOW)
            cs.set_strategies(conn, paid.id, [(strategy.id, 0)])
            paid_job = bind_insight_schedule(conn, paid.id, cron="0 10 * * *")
            conn.commit()
        finally:
            conn.close()
    get_settings.cache_clear()
    return Template(path, insight_job, paid_job)


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


def _side_effects(path: Path) -> dict[str, list[tuple[Any, ...]]]:
    """Every row of :data:`_SIDE_EFFECTS`, row ids and wall-clock stamps left out."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        out: dict[str, list[tuple[Any, ...]]] = {}
        for table in _SIDE_EFFECTS:
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            out[table] = [tuple((k, r[k]) for k in r.keys() if k not in _VOLATILE)
                          for r in rows]
        return out
    finally:
        conn.close()


def _run_inline(path: Path, job: str, **stub: Any) -> None:
    """*job* in-process on *path*, with the same stubs a child gets."""
    with pytest.MonkeyPatch.context() as mp:
        _use_db(mp, path)
        mp.setenv("PD_JOB_MODE", "inline")
        stubs.install(mp.setattr, **stub)
        composition.bind_runtime()
        jobs.register_after_job_hook(None)
        _dispatch(job)


def _open_trail(path: Path) -> None:
    """Lift the change trail's bound past every day: a write dated anywhere is recorded
    (with no settled generation the bound is empty and the trail records nothing)."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("INSERT OR IGNORE INTO settle_meta (id) VALUES (1)")
        conn.execute("UPDATE settle_meta SET held_through = '2999-12-31' WHERE id = 1")
        conn.commit()
    finally:
        conn.close()


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
    assert len(_SEAMS) >= 14, _SEAMS  # 15 until W4 retired the snapshot runner's seam
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


def test_what_may_run_in_a_child(template: Template, tmp_path: Path,
                                 monkeypatch: pytest.MonkeyPatch) -> None:
    insight_job = template.insight_job
    _use_db(monkeypatch, _copy(template.path, tmp_path))
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
    template: Template, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_db(monkeypatch, _copy(template.path, tmp_path))
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


@pytest.mark.parametrize("job", [*_CHILD_JOBS, _INSIGHT, _PAID])
def test_every_child_job_writes_the_row_the_in_process_run_writes(
    template: Template, tmp_path: Path, job: str,
) -> None:
    job_id = {_INSIGHT: template.insight_job, _PAID: template.paid_job}.get(job, job)
    inline_db = _copy(template.path, tmp_path / "inline")
    child_db = _copy(template.path, tmp_path / "child")

    _run_inline(inline_db, job_id)
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
    # ... and the same side effects: the cards it wrote, the model calls it booked.
    assert _side_effects(child_db) == _side_effects(inline_db)
    if job == _PAID:
        # A run that SPENDS: its spend columns come from the child's own usage_tally over
        # the llm_usage rows it booked through log_usage — the in-process figures, exactly.
        effects = _side_effects(child_db)
        assert child["status"] == "ok" and child["llm_calls"] >= 1, child
        assert Decimal(child["cost_usd"]) > 0 and child["tokens_in"] >= 1000
        assert len(effects["llm_usage"]) == child["llm_calls"]
        assert len(effects["insights"]) >= 1
    # The child is gone and forgot nothing behind it.
    assert jobs._CHILD_RUNS == {}
    assert not list(child_db.parent.glob("job-*.lock"))
    assert not list(child_db.parent.glob("job-*.progress.json"))


# ------------------------------------------------------------------- the manual door


def test_a_manual_run_goes_to_the_child_and_the_hook_brackets_it_here(
    template: Template, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _copy(template.path, tmp_path)
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
    template: Template, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _copy(template.path, tmp_path)
    _use_db(monkeypatch, db)
    _child_mode(monkeypatch, mode="pid", job_id="signal_scan")
    seen: list[str | None] = []
    jobs.register_after_job_hook(
        lambda job_id: lambda: seen.append(_row(db, job_id=job_id)["finished_at"]))
    run_id = _dispatch("signal_scan")
    assert run_id is not None and seen == [_row(db, run_id=run_id)["finished_at"]]
    assert seen[0] is not None


class SettledDb(NamedTuple):
    path: Path
    #: The reason of every automatic run the after-job hook asked for (none is run).
    started: list[str]


@pytest.fixture
def settled_db(template: Template, tmp_path: Path,
               monkeypatch: pytest.MonkeyPatch) -> Iterator[SettledDb]:
    """A template copy as the configured database with a published, usable store (the service
    tests' stub worker, inline) and the automatic starts recorded by reason instead of run.
    The caller binds the REAL after-job hook after :func:`_child_mode` (which clears it)."""
    from tests.settlement import service_support as sup

    db = sup.prepare(_copy(template.path, tmp_path), monkeypatch)
    assert service.start_run(scope="full", reason="button").ended is not None
    started: list[str] = []

    def record(*, scope: str, reason: str, **kw: Any) -> None:
        started.append(reason)

    monkeypatch.setattr(service, "start_automatic", record)
    yield SettledDb(db, started)
    service.reset_for_tests()


def _manual_run(job_id: str) -> None:
    """The 立即執行 door: the request inserts the row, its daemon thread runs the job."""
    with session() as conn:
        start_run(conn, job_id, now=NOW)
    run_job_func(job_id, now=NOW)


def _dispatch_behind_the_gate(db: Path, during_wait: Callable[[], None], *,
                              door: str = "schedule") -> None:
    """A run of ``signal_scan`` — its cron fire, or a manual 立即執行 — queued behind another
    heavy child (the gate's lock held here), *during_wait* run while it waits, then the gate
    released and the run finished."""
    gate = proc_lock.lock(proc_lock.heavy_child_lock_path(), timeout=5)
    run: Callable[[str], object] = _dispatch if door == "schedule" else _manual_run
    worker = threading.Thread(target=run, args=("signal_scan",), daemon=True)
    try:
        worker.start()
        _wait_until(lambda: any(c.handle is not None and c.waiting()
                                for c in jobs._CHILD_RUNS.values()))
        time.sleep(0.3)
        during_wait()
    finally:
        gate.release()
    worker.join(120)
    assert not worker.is_alive()
    assert _row(db, job_id="signal_scan")["status"] == "ok"


def _owner_revises_a_close(db: Path) -> None:
    """The owner's own price write (a new ``prices`` counter, dated before the live window):
    counters carry no actor, so only WHEN the job's mark was taken tells it apart."""
    conn = sqlite3.connect(db)
    try:
        install_actor(conn, "user:owner")
        upsert_prices(conn, [PriceRow(instrument="2330", market=Market.TW,
                                      as_of=date(2026, 3, 2), close=Decimal("612.5"),
                                      source="test")], fetched_at=NOW)
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("door", ["schedule", "manual"])
def test_an_owner_change_made_while_a_job_waits_for_the_gate_is_not_the_jobs(
    settled_db: SettledDb, monkeypatch: pytest.MonkeyPatch, door: str,
) -> None:
    """Code review 2026-10-20: the trail mark was taken BEFORE the job waited at the heavy
    gate — possibly minutes, behind a settlement child — so a price counter the owner created
    during the wait counted as the job's change and started an automatic run (reason
    ``job:<id>``) instead of leaving it to the owner's prompt. The mark is now taken when the
    gate grants the turn, on the cron fire and on 立即執行 alike."""
    db = settled_db.path
    _child_mode(monkeypatch, mode="pid", job_id="signal_scan")
    jobs.register_after_job_hook(service.after_job_hook)
    _dispatch_behind_the_gate(db, lambda: _owner_revises_a_close(db), door=door)
    pending = sqlite3.connect(db)
    try:   # the owner's change is recorded, and left pending for the owner's prompt
        assert pending.execute(
            "SELECT COUNT(*) FROM settle_change_agg WHERE claimed_run_id IS NULL "
            "AND settled_run_id IS NULL").fetchone()[0] == 1
    finally:
        pending.close()
    assert settled_db.started == []


def test_a_change_the_job_itself_makes_after_the_gate_still_starts_a_run(
    settled_db: SettledDb, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _child_mode(monkeypatch, mode="write", job_id="signal_scan")   # a cash movement, back-dated
    jobs.register_after_job_hook(service.after_job_hook)
    _dispatch_behind_the_gate(settled_db.path, lambda: None)
    assert settled_db.started == ["job:signal_scan"]


# ------------------------------------------------------------------- liveness


def test_a_child_that_dies_mid_run_is_finalised_error(
    template: Template, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _copy(template.path, tmp_path)
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
    template: Template, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _copy(template.path, tmp_path)
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
    template: Template, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _copy(template.path, tmp_path)
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
    template: Template, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _copy(template.path, tmp_path)
    _use_db(monkeypatch, db)
    data = proc_lock.data_dir()
    with session() as conn:
        orphan = start_job_run(conn, "signal_scan", now=NOW)
        live = start_job_run(conn, "news_daily", now=NOW)
        settle = start_job_run(conn, jobs.SETTLEMENT_JOB_ID, now=NOW)
        done = start_job_run(conn, "backup_daily", now=NOW)
        jobs.finish_job_run(conn, done, status="ok", detail="備份完成", now=NOW,
                            usage=None)
    def files(run_id: int) -> list[Path]:
        paths = [proc_lock.job_lock_path(run_id, data), jobs.progress_sidecar_path(run_id, data)]
        for p in paths:
            if not p.exists():
                p.write_text("{}", encoding="utf-8")
        return paths

    finished_files = files(done)  # a run whose row was final before this boot
    orphan_files = files(orphan)  # a free lock of a row this boot closes
    unknown_files = files(9999)  # no row at all
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
    # Files: only a run already final before this boot loses its files. The orphan's free
    # lock may belong to a surviving child that opened it and has not locked it yet (POSIX):
    # it stays until a LATER boot, when its row is final; a file of no known run stays too.
    assert not any(p.exists() for p in finished_files)
    assert all(p.exists() for p in orphan_files + unknown_files)
    with session() as conn:
        # the next boot: the lock of `live` is free now, so its row is closed in turn
        assert jobs.close_abandoned_runs(conn, now=NOW) == [live]
    assert not any(p.exists() for p in orphan_files)
    assert all(p.exists() for p in unknown_files)


def test_the_worker_never_runs_a_row_that_is_already_final(
    template: Template, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_db(monkeypatch, _copy(template.path, tmp_path))
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
                 jobs.QUEUED_BEHIND_GATE, jobs.SPAWN_FAILED.format(error="RuntimeError"),
                 jobs.JOB_TIMED_OUT.format(limit=jobs._limit_text(jobs.JOB_TIME_LIMIT_S)),
                 jobs.busy_sentence("signal_scan", "洞察組合 3")):
        assert text and not english_words(text), text
    assert jobs._limit_text(1800) == "30 分鐘" and jobs._limit_text(3) == "3 秒"
    # the owner's ruling, 2026-10-20: a job child runs at most 60 minutes
    assert jobs.JOB_TIME_LIMIT_S == 3600 and jobs._limit_text(jobs.JOB_TIME_LIMIT_S) == "60 分鐘"


# ------------------------------------------------------------------- package H review fixes


def _boom(*a: Any, **kw: Any) -> child_runner.ChildHandle:
    raise RuntimeError("can't start new thread")


def test_a_heavy_spawn_whose_waiter_cannot_start_never_wedges_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reviewer's ``gate_wedge.py``: the ticket used to stay at the head of the FIFO, so
    every later heavy child — every job, every settlement run — waited forever."""
    runner = child_runner.ChildRunner(heavy_lock_path=tmp_path / "heavy.lock")
    real_start = threading.Thread.start
    failed: list[str] = []

    def failing_start(self: threading.Thread) -> None:
        if self.name.startswith("child-waiter") and not failed:
            failed.append(self.name)
            raise RuntimeError("can't start new thread")
        real_start(self)

    monkeypatch.setattr(threading.Thread, "start", failing_start)
    with pytest.raises(RuntimeError):
        runner.spawn("this", heavy=True)
    monkeypatch.setattr(threading.Thread, "start", real_start)
    assert failed and runner.gate.queued() == 0
    result = runner.spawn("this", heavy=True).wait(timeout=60)
    assert result is not None and result.ok


def test_a_ticket_whose_wait_breaks_leaves_the_queue(tmp_path: Path) -> None:
    """Anything raised while a heavy child waits for its turn takes its ticket with it."""
    runner = child_runner.ChildRunner(heavy_lock_path=tmp_path / "heavy.lock")
    env = child_runner.child_env()
    first = child_runner.ChildHandle(runner, "this", (), True, env, None, 1024)
    second = child_runner.ChildHandle(runner, "this", (), True, env, None, 1024)
    runner.gate.enqueue(first)
    runner.gate.enqueue(second)
    held = runner.gate.enter(first)
    assert held is not None

    class _Broken(threading.Condition):
        def wait(self, timeout: float | None = None) -> bool:
            raise RuntimeError("interrupted")

    runner.gate._cond = _Broken()
    with pytest.raises(RuntimeError):
        runner.gate.enter(second)
    assert runner.gate.queued() == 0
    runner.gate.leave(first)
    held.release()


@pytest.mark.parametrize("door", ["cron", "manual"])
def test_a_spawn_that_raises_ends_the_static_row_and_frees_the_job(
    template: Template, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, door: str,
) -> None:
    """The reviewer's ``test_spawn_raise.py``: the row stayed ``running``, so 立即執行
    answered 409 until a restart."""
    db = _copy(template.path, tmp_path)
    _use_db(monkeypatch, db)
    _child_mode(monkeypatch)
    monkeypatch.setattr(child_runner, "spawn", _boom)
    if door == "cron":
        run_id = _dispatch("signal_scan")  # never raises to APScheduler
    else:
        with session() as conn:
            run_id = start_run(conn, "signal_scan", now=NOW)
        run_job_func("signal_scan", now=NOW)
    assert run_id is not None
    row = _row(db, run_id=run_id)
    assert (row["status"], row["detail"], row["reason"]) == (
        "error", "無法啟動工作子程序（RuntimeError）", "spawn_failed")
    with session() as conn:
        assert not jobs.latest_run_unfinished(conn, "signal_scan")
    assert jobs._CHILD_RUNS == {} and jobs.queued_progress() == {}


def test_a_spawn_that_raises_does_not_skip_the_next_fire_of_an_insight_task(
    template: Template, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _copy(template.path, tmp_path)
    _use_db(monkeypatch, db)
    _child_mode(monkeypatch)
    monkeypatch.setattr(child_runner, "spawn", _boom)
    _dispatch(template.insight_job)
    first = _row(db, job_id=template.insight_job)
    assert (first["status"], first["reason"]) == ("error", "spawn_failed")
    _dispatch(template.insight_job)  # the next day's fire runs (here: fails the same way)
    second = _row(db, job_id=template.insight_job)
    assert second["id"] != first["id"] and second["reason"] == "spawn_failed"


def test_a_child_over_its_time_limit_is_stopped_and_its_row_says_so(
    template: Template, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _copy(template.path, tmp_path)
    _use_db(monkeypatch, db)
    _child_mode(monkeypatch, mode="slow", job_id="signal_scan", sleep_s=120)
    monkeypatch.setattr(jobs, "JOB_TIME_LIMIT_S", 3.0)
    t0 = time.monotonic()
    run_id = _dispatch("signal_scan")
    assert time.monotonic() - t0 < 60
    assert run_id is not None
    row = _row(db, run_id=run_id)
    assert (row["status"], row["detail"], row["reason"]) == (
        "error", "工作執行逾時（超過 3 秒），已中止", "timeout")
    assert jobs._CHILD_RUNS == {}
    assert child_runner.default_runner().gate.queued() == 0
    # The gate is free: the next heavy child runs at once.
    assert child_runner.spawn("this", heavy=True).wait(timeout=60) is not None


def test_a_childs_writes_reach_the_change_trail_as_the_job(
    template: Template, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job worker stamps its writes ``job:<id>``, as the in-process run does — the after-job
    hook finds the job's own changes by that actor (``settlement_service.
    job_earliest_change``), so a worker without ``install_actor`` would never start the
    re-settlement a back-dated job write needs."""
    inline_db = _copy(template.path, tmp_path / "inline")
    child_db = _copy(template.path, tmp_path / "child")
    for db in (inline_db, child_db):
        _open_trail(db)
    _run_inline(inline_db, "signal_scan", mode="write", job_id="signal_scan")
    with pytest.MonkeyPatch.context() as mp:
        _use_db(mp, child_db)
        _child_mode(mp, mode="write", job_id="signal_scan")
        _dispatch("signal_scan")
    trail = _side_effects(child_db)["settle_change_log"]
    assert trail == _side_effects(inline_db)["settle_change_log"]
    assert [dict(r)["actor"] for r in trail] == ["job:signal_scan"]
    assert dict(trail[0])["table_name"] == "cash_movements"


def test_the_worker_appends_to_the_app_log_and_never_rotates_it(tmp_path: Path) -> None:
    """Two processes must not rotate one file: the worker's handler appends, nothing more."""
    from logging.handlers import RotatingFileHandler

    handler = job_worker.attach_append_log(tmp_path)
    try:
        assert type(handler) is logging.FileHandler
        assert not isinstance(handler, RotatingFileHandler)
        logging.getLogger("portfolio_dash.test").warning("工作子程序寫入")
        handler.flush()
        line = (tmp_path / "app.log").read_text(encoding="utf-8").strip().splitlines()[-1]
        assert json.loads(line)["msg"] == "工作子程序寫入"
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()
    src = Path(job_worker.__file__).read_text(encoding="utf-8")
    assert "configure_logging()" not in src and "RotatingFileHandler(" not in src


def test_a_door_says_queued_not_running_while_the_run_waits_for_the_gate(
    template: Template, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from portfolio_dash.api.routers.scheduler import insight_task_run_refusal

    db = _copy(template.path, tmp_path)
    _use_db(monkeypatch, db)
    task_id = int(template.paid_job.split(":", 1)[1])
    with session() as conn:
        run_id = start_run(conn, template.paid_job, now=NOW)
        running = insight_task_run_refusal(conn, task_id)
        jobs._CHILD_RUNS[run_id] = jobs._ChildRun(  # handed to a child still in the queue
            job_id=template.paid_job, run_id=run_id, progress_path=tmp_path / "p.json",
            lock_path=tmp_path / "l.lock")
        queued = insight_task_run_refusal(conn, task_id)
    assert running is not None and queued is not None
    assert json.loads(bytes(running.body))["error"] == {
        "code": "already_running", "message": f"洞察組合 {task_id} 執行中"}
    assert json.loads(bytes(queued.body))["error"] == {
        "code": "already_running", "message": f"洞察組合 {task_id} 已排入，等待其他背景工作完成"}
