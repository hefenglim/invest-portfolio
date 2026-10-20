"""The service (package E) driving the REAL worker (package D): the wave-2 merge's cross tests
(plan §6.4 step 3).

E's own tests run a stand-in worker; D's run the worker without the service. These run both
together, inline and in a real child process: what the service reads (status, the waiter, boot)
must agree with what the worker wrote (the progress file, the report, the store).

A child that has to be stopped mid-way, or die early, gets one step of the real worker patched
by ``tests/settlement/worker_stubs.py`` (``PD_TEST_STUBS``); the rest is the real code.
"""

from __future__ import annotations

import os
import signal
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from portfolio_dash.api import settlement_service as service
from portfolio_dash.portfolio import settle_worker
from portfolio_dash.portfolio import settlement_store as st
from portfolio_dash.portfolio.settlement_store import TriggerCheck
from portfolio_dash.shared import child_runner, proc_lock
from portfolio_dash.shared.config import get_settings
from tests.reference.corpora import open_db
from tests.settlement import service_support as sup
from tests.settlement.conftest import CorpusFactory

STUBS = "tests.settlement.worker_stubs"


@pytest.fixture
def golden(corpus_db: CorpusFactory, monkeypatch: pytest.MonkeyPatch
           ) -> Iterator[tuple[Path, datetime]]:
    corpus = corpus_db("golden")
    path = sup.prepare(corpus.path, monkeypatch, stub_worker=False)
    yield path, corpus.now
    sup.wait_until(lambda: service.active_run_id() is None)
    service.reset_for_tests()
    get_settings.cache_clear()


def _child(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str = "",
           sleep_s: float = 60) -> Path:
    """Child mode with the real worker module; *mode* patches one step (worker_stubs)."""
    monkeypatch.setenv("PD_SETTLE_MODE", "child")
    marker = tmp_path / "stub-once"
    if mode:
        marker.write_text("1", encoding="utf-8")
        monkeypatch.setenv("PD_TEST_MODE", "1")
        monkeypatch.setenv("PD_TEST_STUBS", STUBS)
        monkeypatch.setenv("PD_STUB_WORKER", mode)
        monkeypatch.setenv("PD_STUB_ONCE", str(marker))
        monkeypatch.setenv("PD_STUB_SLEEP_S", str(sleep_s))
    return marker


def _status(path: Path, now: datetime) -> Any:
    conn = open_db(path)
    try:
        return service.status(conn, viewer=None, now=now,
                              reporting=get_settings().reporting_currency)
    finally:
        conn.close()


def _settled(path: Path) -> list[dict[str, Any]]:
    """The published generation's days and periods, generation number left out."""
    conn = open_db(path)
    try:
        gen = st.store_state(conn, reporting=get_settings().reporting_currency).generation
        out: list[dict[str, Any]] = []
        for table, order in (("settled_day", "day"), ("settled_period", "kind, period")):
            for row in conn.execute(
                    f"SELECT * FROM {table} WHERE generation = ? ORDER BY {order}", (gen,)):
                d = dict(row)
                d.pop("generation")
                d.pop("built_at", None)
                out.append({"table": table, **d})
        return out
    finally:
        conn.close()


def _fresh_inline(corpus_db: CorpusFactory, monkeypatch: pytest.MonkeyPatch,
                  now: datetime) -> list[dict[str, Any]]:
    """An independent full run, inline, on another copy of the same corpus."""
    other = corpus_db("golden").path
    with monkeypatch.context() as m:
        sup.prepare(other, m, stub_worker=False)
        m.setenv("PD_SETTLE_MODE", "inline")
        service.start_run(scope="full", reason="button", now=now)
        rows = _settled(other)
    service.reset_for_tests()
    get_settings.cache_clear()
    return rows


def _same_days_as_a_fresh_run(path: Path, corpus_db: CorpusFactory,
                              monkeypatch: pytest.MonkeyPatch, now: datetime) -> None:
    """Every day both hold equals an independent fresh run's. A resume is started by the
    service with the wall clock, so its range reaches past the corpus day the fresh run
    stops at: compare the days in common (the prefix property makes them comparable)."""
    fresh = {r["day"]: r for r in _fresh_inline(corpus_db, monkeypatch, now)
             if r["table"] == "settled_day"}
    mine = {r["day"]: r for r in _settled(path) if r["table"] == "settled_day"}
    common = sorted(set(fresh) & set(mine))
    assert len(common) == len(fresh), "the resumed generation lacks days the fresh one has"
    assert [mine[d] for d in common] == [fresh[d] for d in common]


def _agree(path: Path, run_id: int, now: datetime) -> None:
    """status ≡ progress file ≡ report, once the run ended."""
    report = sup.report(path, run_id)
    progress = service.read_progress(run_id)
    status = _status(path, now)
    assert progress is not None and progress.run_id == run_id
    assert progress.status == report.summary.status
    assert status.last_run is not None and status.last_run.run_id == run_id
    assert status.last_run.status == report.summary.status
    assert [p.status for p in progress.phases] == [p.status for p in report.steps]


@pytest.mark.parametrize("mode", ["inline", "child"])
def test_a_real_run_through_the_service_publishes(
    golden: tuple[Path, datetime], monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str,
) -> None:
    path, now = golden
    if mode == "child":
        _child(monkeypatch, tmp_path)
    started = service.start_run(scope="full", reason="button", started_by="user:guest",
                                now=now)
    sup.wait_until(lambda: service.active_run_id() is None)
    report = sup.report(path, started.run_id)
    assert report.summary.status == "published", report.failure
    assert sup.job_row(path, started.run_id)["status"] == "ok"
    state = sup.state(path)
    assert state.generation == 1 and state.dirty_from is None and state.run_id is None
    assert state.settled_through == report.summary.settled_through_after
    assert _status(path, now).state == "clean"
    _agree(path, started.run_id, now)
    assert not proc_lock.is_held(proc_lock.settlement_lock_path())


def test_the_start_up_status_reads_preparing(
    golden: tuple[Path, datetime], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    path, now = golden
    _child(monkeypatch, tmp_path, "slow_claim", sleep_s=3)
    started = service.start_run(scope="full", reason="button", now=now)
    seen = _status(path, now)
    assert seen.state == "preparing"
    assert seen.run is not None and seen.run.run_id == started.run_id
    sup.wait_until(lambda: service.active_run_id() is None)
    assert sup.report(path, started.run_id).summary.status == "published"
    assert _status(path, now).state == "clean"


def test_status_progress_and_report_agree_mid_run(
    golden: tuple[Path, datetime], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    path, now = golden
    marker = _child(monkeypatch, tmp_path, "slow_valuate", sleep_s=4)
    started = service.start_run(scope="full", reason="button", now=now)
    sup.wait_until(lambda: not marker.exists())  # the child is inside the valuation sleep
    sup.wait_until(lambda: sup.report(path, started.run_id).summary.status == "running")
    status = _status(path, now)
    progress = service.read_progress(started.run_id)
    report = sup.report(path, started.run_id)
    assert status.state == "running"
    assert progress is not None and progress.status == "running"
    assert status.run is not None and status.run.run_id == started.run_id
    assert progress.pid is not None and progress.pid != os.getpid()
    done = {p.key for p in report.steps if p.status == "done"}
    assert {"load", "check"} <= done
    assert {p.key for p in progress.phases if p.status == "done"} >= done
    sup.wait_until(lambda: service.active_run_id() is None)
    _agree(path, started.run_id, now)


def test_a_kill_with_the_web_process_alive_ends_interrupted_and_resumes_once(
    golden: tuple[Path, datetime], corpus_db: CorpusFactory,
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    path, now = golden
    marker = _child(monkeypatch, tmp_path, "slow_valuate")
    started = service.start_run(scope="full", reason="button", now=now)
    sup.wait_until(lambda: not marker.exists())
    progress = service.read_progress(started.run_id)
    assert progress is not None and progress.pid is not None
    os.kill(progress.pid, signal.SIGTERM)  # TerminateProcess on Windows
    sup.wait_until(lambda: service.active_run_id() is None
                   and len(sup.runs(path)) == 2
                   and sup.runs(path)[-1].status not in ("preparing", "running"))
    first, resumed = sup.runs(path)
    assert first.run_id == started.run_id and first.status == "interrupted"
    assert first.resumed
    assert resumed.reason == "resume" and resumed.previous_run_id == first.run_id
    assert resumed.status == "published"
    assert sup.job_row(path, first.run_id)["status"] == "error"
    assert sup.state(path).dirty_from is None
    _same_days_as_a_fresh_run(path, corpus_db, monkeypatch, now)


def test_a_kill_across_a_restart_is_finalised_and_resumed_by_boot(
    golden: tuple[Path, datetime], corpus_db: CorpusFactory,
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The web process is gone (no waiter): the child dies alone, and the next boot ends the
    run 中斷 and resumes it."""
    path, now = golden
    marker = _child(monkeypatch, tmp_path, "slow_valuate")
    run_id = sup.fake_preparing_run(path, scope="full", through=now.date() - timedelta(days=1))
    handle = child_runner.spawn(service.WORKER_MODULE, ["--run", str(run_id)])
    try:
        sup.wait_until(lambda: not marker.exists())
        progress = service.read_progress(run_id)
        assert progress is not None and progress.pid is not None
        os.kill(progress.pid, signal.SIGTERM)
        assert handle.wait(30) is not None
    finally:
        handle.terminate_and_wait(10)
    assert sup.report(path, run_id).summary.status == "running"  # nobody finalised it
    conn = open_db(path)
    try:
        plan = service.on_boot(conn, trigger_check=TriggerCheck())
        conn.commit()
    finally:
        conn.close()
    assert plan.interrupted == (run_id,)
    assert plan.start is not None
    service.start_after_boot(plan)
    sup.wait_until(lambda: service.active_run_id() is None
                   and sup.runs(path)[-1].status not in ("preparing", "running"))
    first, *rest = sup.runs(path)
    assert first.status == "interrupted"
    assert rest and rest[-1].status == "published"
    assert sup.state(path).dirty_from is None
    assert not proc_lock.is_held(proc_lock.settlement_lock_path())
    _same_days_as_a_fresh_run(path, corpus_db, monkeypatch, now)


def test_a_real_child_that_dies_before_claiming_ends_failed(
    golden: tuple[Path, datetime], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    path, now = golden
    _child(monkeypatch, tmp_path, "die_before_claim")
    before = sup.state(path)
    started = service.start_run(scope="full", reason="button", now=now)
    sup.wait_until(lambda: service.active_run_id() is None)
    report = sup.report(path, started.run_id)
    assert report.summary.status == "failed"
    assert report.failure is not None and report.failure.reason_text
    assert sup.job_row(path, started.run_id)["status"] == "error"
    after = sup.state(path)
    assert (after.generation, after.dirty_from, after.run_id) == (
        before.generation, before.dirty_from, None)
    assert not proc_lock.is_held(proc_lock.settlement_lock_path())


def test_a_real_phase_failure_records_its_class_on_the_job_row(
    golden: tuple[Path, datetime], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real worker catches a phase's exception, ends the run itself and returns: the
    class reaches ``job_runs.reason`` through ``RunFailure.error_class`` (spec §9.4), never
    through a traceback (wave-2 review E #3)."""
    path, now = golden

    def broken(self: object, *args: object, **kwargs: object) -> object:
        raise ValueError("a broken valuation")

    monkeypatch.setattr(settle_worker._Run, "_valuate", broken)
    started = service.start_run(scope="full", reason="button", now=now)
    report = sup.report(path, started.run_id)
    assert report.summary.status == "failed"
    assert report.failure is not None and report.failure.error_class == "ValueError"
    row = sup.job_row(path, started.run_id)
    assert (row["status"], row["reason"]) == ("error", "failed:ValueError")
    assert "（ValueError）" in row["detail"]

