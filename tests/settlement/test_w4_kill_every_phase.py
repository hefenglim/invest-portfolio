"""PERF plan W4 / spec §18: the settlement child killed in EVERY phase leaves the published
generation intact, and the resume equals a fresh run.

A published generation G exists; a back-dated trade makes it dirty; a dirty run in a REAL child
is held just before one phase (``tests/settlement/worker_stubs.py`` ``slow_phase``) and killed
there. The run must end 中斷 without publishing, the waiter resumes it once, and the resumed
generation (G + 1, the only publish after G) equals a fresh full run of the same ledger on every
settled day both hold.
"""

from __future__ import annotations

import os
import signal
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from portfolio_dash.api import settlement_service as service
from portfolio_dash.shared import proc_lock
from portfolio_dash.shared.config import get_settings
from tests.settlement import service_support as sup
from tests.settlement.conftest import CorpusFactory
from tests.settlement.test_wave2_cross import _child, _settled

PHASES = ("_load", "_check", "_plan", "_valuate", "_close", "_bench", "_swap")
BACK_DATED = date(2026, 2, 2)


@pytest.fixture
def golden(corpus_db: CorpusFactory, monkeypatch: pytest.MonkeyPatch
           ) -> Iterator[tuple[Path, datetime]]:
    corpus = corpus_db("golden")
    path = sup.prepare(corpus.path, monkeypatch, stub_worker=False)
    yield path, corpus.now
    sup.wait_until(lambda: service.active_run_id() is None)
    service.reset_for_tests()
    get_settings.cache_clear()


def _fresh_days(corpus_db: CorpusFactory, monkeypatch: pytest.MonkeyPatch,
                now: datetime) -> dict[str, dict[str, Any]]:
    """A fresh full run, inline, on another copy WITH the same back-dated trade."""
    other = corpus_db("golden").path
    with monkeypatch.context() as m:
        sup.prepare(other, m, stub_worker=False)
        m.setenv("PD_SETTLE_MODE", "inline")
        sup.back_dated_trade(other, BACK_DATED)
        service.start_run(scope="full", reason="button", now=now)
        rows = {r["day"]: r for r in _settled(other) if r["table"] == "settled_day"}
    service.reset_for_tests()
    get_settings.cache_clear()
    return rows


@pytest.mark.parametrize("phase", PHASES)
def test_a_kill_in_any_phase_keeps_the_generation_and_the_resume_equals_fresh(
    golden: tuple[Path, datetime], corpus_db: CorpusFactory,
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, phase: str,
) -> None:
    path, now = golden
    published = service.start_run(scope="full", reason="button", now=now)  # inline: G
    assert sup.report(path, published.run_id).summary.status == "published"
    generation = sup.state(path).generation
    sup.back_dated_trade(path, BACK_DATED)
    assert sup.state(path).dirty_from == BACK_DATED

    marker = _child(monkeypatch, tmp_path, "slow_phase")
    monkeypatch.setenv("PD_STUB_PHASE", phase)
    started = service.start_run(scope="dirty", reason="button", now=now)
    sup.wait_until(lambda: not marker.exists())  # the child sleeps just before `phase`
    progress = service.read_progress(started.run_id)
    assert progress is not None and progress.pid is not None
    os.kill(progress.pid, signal.SIGTERM)
    sup.wait_until(lambda: service.active_run_id() is None
                   and len(sup.runs(path)) == 3
                   and sup.runs(path)[-1].status not in ("preparing", "running"))

    _, killed, resumed = sup.runs(path)
    assert killed.run_id == started.run_id and killed.status == "interrupted", phase
    assert killed.generation_after is None, "a killed run published"
    assert resumed.reason == "resume" and resumed.status == "published", phase
    state = sup.state(path)
    assert state.generation == generation + 1, "only the resume may publish after G"
    assert state.dirty_from is None and state.run_id is None
    assert not proc_lock.is_held(proc_lock.settlement_lock_path())

    fresh = _fresh_days(corpus_db, monkeypatch, now)
    mine = {r["day"]: r for r in _settled(path) if r["table"] == "settled_day"}
    common = sorted(set(fresh) & set(mine))
    assert len(common) == len(fresh), phase
    assert [mine[d] for d in common] == [fresh[d] for d in common], phase
