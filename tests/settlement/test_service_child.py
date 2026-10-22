"""The settlement service with REAL child processes (``PD_SETTLE_MODE=child``, spec §9.2, §9.4).

The child runs ``tests/settlement/stub_settle_worker.py`` (``service.WORKER_MODULE`` pointed
at it): a real process, a real OS lock, a real progress file with the child's own pid — what
the waiter, cancel and boot reason about. Each test starts at most two children (≈ 2–4 s each
on Windows).
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from portfolio_dash.api import settlement_service as service
from portfolio_dash.portfolio.settlement_store import TriggerCheck
from portfolio_dash.shared import child_runner, proc_lock
from portfolio_dash.shared.config import get_settings
from tests.reference.corpora import open_db
from tests.settlement import service_support as sup
from tests.settlement import stub_settle_worker as stub
from tests.settlement.conftest import CorpusFactory


@pytest.fixture
def path(corpus_db: CorpusFactory, tmp_path: Path,
         monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    p = sup.prepare(corpus_db("golden").path, monkeypatch, stub_worker=False)
    monkeypatch.setenv("PD_SETTLE_MODE", "child")
    monkeypatch.setenv(stub.PLAN_ENV, str(tmp_path / "plan.json"))
    monkeypatch.setattr(service, "WORKER_MODULE", "tests.settlement.stub_settle_worker")
    yield p
    sup.wait_until(lambda: service.active_run_id() is None)
    service.reset_for_tests()
    get_settings.cache_clear()


def _plan(tmp: Path, *behaviours: str) -> None:
    (tmp / "plan.json").write_text(json.dumps(list(behaviours)), encoding="utf-8")


def _done() -> bool:
    return service.active_run_id() is None


def test_a_child_run_returns_at_once_and_the_waiter_finalises_it(
    path: Path, tmp_path: Path
) -> None:
    _plan(tmp_path, "publish")
    started = service.start_run(scope="full", reason="button", started_by="user:guest")
    assert started.ended is None  # the call did not wait for the child
    sup.wait_until(_done)
    report = sup.report(path, started.run_id)
    assert report.summary.status == "published"
    row = sup.job_row(path, started.run_id)
    assert row["status"] == "ok" and row["finished_at"] is not None
    assert not proc_lock.is_held(proc_lock.settlement_lock_path())


def test_cancel_terminates_waits_and_keeps_the_current_version(
    path: Path, tmp_path: Path
) -> None:
    _plan(tmp_path, "sleep")
    started = service.start_run(scope="full", reason="button", started_by="user:guest")
    sup.wait_until(lambda: sup.report(path, started.run_id).summary.status == "running")
    sup.wait_until(lambda: proc_lock.is_held(proc_lock.settlement_lock_path()))
    result = service.cancel(by="user:guest")
    assert result == service.CancelResult(run_id=started.run_id, status="cancelled",
                                          cancelled=True)
    assert service.active_run_id() is None  # cancel returns after the waiter
    report = sup.report(path, started.run_id)
    assert report.summary.cancelled_by == "user:guest"
    assert report.failure is not None and report.failure.consequence == \
        "目前尚無結算版本，歷史照常即時計算"
    row = sup.job_row(path, started.run_id)
    assert (row["status"], row["reason"]) == ("skipped", "cancelled")
    assert row["detail"].startswith("重新結算已取消，保留目前版本")
    assert sup.state(path).run_id is None and sup.state(path).dirty_from is not None
    assert not proc_lock.is_held(proc_lock.settlement_lock_path())
    assert len(sup.runs(path)) == 1  # a cancel starts no follow-up


def test_a_cancel_keeps_a_run_queued_behind_it(path: Path, tmp_path: Path) -> None:
    """Review E #6: the nightly run queued behind a cancelled one (its job row said
    「排在其後」) still starts; the cancelled run's own follow-up (a chained run) does not."""
    _plan(tmp_path, "sleep", "publish")
    started = service.start_run(scope="full", reason="button", started_by="user:guest")
    sup.wait_until(lambda: sup.report(path, started.run_id).summary.status == "running")
    queued = service.start_automatic(scope="nightly", reason="nightly")
    assert queued.queued_behind == started.run_id
    result = service.cancel(by="user:guest")
    assert result.cancelled
    sup.wait_until(lambda: len(sup.runs(path)) == 2 and _done())
    first, second = sup.runs(path)
    assert first.status == "cancelled"
    assert second.reason == "nightly" and second.status == "published"
    assert second.previous_run_id is None


def test_a_child_that_dies_before_claiming_ends_failed(path: Path, tmp_path: Path) -> None:
    _plan(tmp_path, "die_before_claim")
    started = service.start_run(scope="full", reason="button")
    sup.wait_until(_done)
    report = sup.report(path, started.run_id)
    assert report.summary.status == "failed"
    assert sup.job_row(path, started.run_id)["status"] == "error"


def test_boot_stops_an_orphan_child_by_its_own_pid_and_resumes_it(
    path: Path, tmp_path: Path
) -> None:
    """A child that outlived its web process: boot terminates it by the pid it recorded,
    waits for the lock to free, ends the run interrupted, then plans the resume."""
    service.reset_for_tests()
    run_id = sup.fake_preparing_run(path, scope="full")
    _plan(tmp_path, "sleep")
    handle = child_runner.spawn("tests.settlement.stub_settle_worker", ["--run", str(run_id)])
    try:
        sup.wait_until(lambda: sup.report(path, run_id).summary.status == "running")
        sup.wait_until(lambda: service.read_progress(run_id) is not None)
        assert proc_lock.is_held(proc_lock.settlement_lock_path())
        conn = open_db(path)
        try:
            plan = service.on_boot(conn, trigger_check=TriggerCheck())
            conn.commit()
        finally:
            conn.close()
        assert not proc_lock.is_held(proc_lock.settlement_lock_path())
        assert handle.wait(30) is not None
    finally:
        handle.terminate_and_wait(10)
    assert plan.interrupted == (run_id,)
    report = sup.report(path, run_id)
    assert report.summary.status == "interrupted"
    assert plan.start is not None and plan.start.scope == "full"  # generation 0: a full run


def test_a_worker_that_cannot_be_spawned_ends_failed_and_frees_the_service(
    path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``child_runner.spawn`` raising (no thread / process under memory pressure) must never
    leave the service believing a run is active: the run ends 失敗 with the class, and the
    next start is accepted (wave-3 review H)."""
    def no_spawn(*args: object, **kwargs: object) -> object:
        raise RuntimeError("can't start new thread")

    with monkeypatch.context() as m:
        m.setattr(child_runner, "spawn", no_spawn)
        started = service.start_run(scope="full", reason="button")
    assert started.ended is not None and started.ended.status == "failed"
    assert service.active_run_id() is None
    row = sup.job_row(path, started.run_id)
    assert (row["status"], row["reason"]) == ("error", "failed:RuntimeError")
    _plan(tmp_path, "publish")
    again = service.start_run(scope="full", reason="button")
    sup.wait_until(_done)
    assert sup.report(path, again.run_id).summary.status == "published"


def test_the_apps_stop_ends_a_running_child_interrupted_and_boot_resumes_it(
    path: Path, tmp_path: Path
) -> None:
    """Backlog item 2: the lifespan's stop terminates the running settle child
    (``child_runner.stop_all``) instead of leaving it to hold the database through the WAL
    checkpoint. Its waiter ends the run 中斷 with the stop's reason — never 已取消, which boot
    would not resume — starts no follow-up and sends no failure push (a resume run's included:
    boot resumes it), and the next boot plans its resume."""
    _plan(tmp_path, "publish", "sleep")
    first = service.start_run(scope="full", reason="button")
    sup.wait_until(_done)
    assert sup.report(path, first.run_id).summary.status == "published"   # generation 1
    resumed = service.start_automatic(scope="full", reason="resume",
                                      previous_run_id=first.run_id)
    run_id = resumed.run_id
    assert run_id is not None
    sup.wait_until(lambda: sup.report(path, run_id).summary.status == "running")
    sup.wait_until(lambda: proc_lock.is_held(proc_lock.settlement_lock_path()))
    service.shutdown()
    assert child_runner.stop_all(30) == []
    assert service.active_run_id() is None          # the waiter finished before the return
    assert not proc_lock.is_held(proc_lock.settlement_lock_path())
    report = sup.report(path, run_id)
    assert report.summary.status == "interrupted" and report.summary.cancelled_by is None
    assert report.failure is not None
    assert report.failure.reason_text == service._STOP_INTERRUPTED == "系統停止時結算尚未完成"
    row = sup.job_row(path, run_id)
    assert (row["status"], row["reason"]) == ("error", "interrupted")
    assert row["detail"].startswith("重新結算中斷") and service._STOP_INTERRUPTED in row["detail"]
    assert [r.run_id for r in sup.runs(path)] == [first.run_id, run_id]   # no follow-up
    assert sup.PUSHES == []
    conn = open_db(path)
    try:
        plan = service.on_boot(conn, trigger_check=TriggerCheck())
        conn.commit()
    finally:
        conn.close()
    assert plan.interrupted == ()                   # nothing left for boot to end
    assert plan.start == service.BootStart(scope="full", reason="resume",
                                           previous_run_id=run_id, mark_resumed=True)


def test_the_apps_stop_ends_a_run_queued_behind_the_gate_interrupted(
    path: Path, tmp_path: Path
) -> None:
    """A run still waiting for the heavy gate when the stop comes never starts its process,
    and ends 中斷 — as boot ends a ``preparing`` orphan — never 失敗 「結算程序無法啟動」."""
    _plan(tmp_path, "publish")
    gate = proc_lock.lock(proc_lock.heavy_child_lock_path(), timeout=5)  # another heavy child
    try:
        started = service.start_run(scope="full", reason="button")
        time.sleep(0.3)
        assert sup.report(path, started.run_id).summary.status == "preparing"
        service.shutdown()
        assert child_runner.stop_all(30) == []
    finally:
        gate.release()
    assert service.active_run_id() is None
    report = sup.report(path, started.run_id)
    assert report.summary.status == "interrupted"
    assert report.failure is not None and report.failure.reason_text == service._STOP_INTERRUPTED
    assert (sup.job_row(path, started.run_id)["status"],
            sup.job_row(path, started.run_id)["reason"]) == ("error", "interrupted")
    assert service.read_progress(started.run_id) is None   # its process never ran
