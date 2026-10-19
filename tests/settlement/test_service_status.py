"""``status()`` — the chip's read (spec §15, §14.1, §9.4 liveness).

Every state of the chip, derived from the store, the reports, the OS lock and the progress
file; the prompt rule; 「你」 only for the person who started a run; and the read itself never
writes (DEF-065) and never starts anything.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from portfolio_dash.api import settlement_service as service
from portfolio_dash.portfolio import settlement_store as st
from portfolio_dash.portfolio.settlement_models import RunProgress, SettlementStatus
from portfolio_dash.shared import proc_lock
from portfolio_dash.shared.config import get_settings
from portfolio_dash.shared.enums import Currency
from tests.reference.corpora import open_db
from tests.settlement import service_support as sup
from tests.settlement import stub_settle_worker as stub
from tests.settlement.conftest import CorpusFactory

_WRITE = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP|BEGIN)\b", re.I)


@pytest.fixture
def path(corpus_db: CorpusFactory, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    yield sup.prepare(corpus_db("golden").path, monkeypatch)
    service.reset_for_tests()
    get_settings.cache_clear()


def _status(path: Path, *, viewer: str | None = "user:guest",
            now: datetime | None = None) -> SettlementStatus:
    conn = open_db(path)
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        result = service.status(conn, viewer=viewer, now=now or service._now(),
                                reporting=Currency.TWD)
        assert conn.total_changes == 0
    finally:
        conn.close()
    writes = [s for s in statements if _WRITE.match(s)]
    assert not writes, f"status wrote: {writes[:3]}"
    return result


def test_off_reads_off_and_nothing_else(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PD_SETTLE", "off")
    result = _status(path)
    assert result.state == "off" and not result.needs_prompt and result.run is None


def test_an_unsettled_ledger_is_pending_and_a_published_one_clean(path: Path) -> None:
    assert _status(path).state == "pending"  # generation 0 with ledger rows
    service.start_run(scope="full", reason="button", started_by="user:guest")
    result = _status(path)
    assert result.state == "clean" and result.generation == 1 and result.calc_ok
    assert result.settled_through == service._now().date() - timedelta(days=1)
    assert result.live_from == result.settled_through + timedelta(days=1)
    assert result.stale_from is None and result.final_through == result.settled_through
    assert result.last_run is not None and result.last_run.status == "published"
    assert result.last_run.trigger_text.startswith("你 於 ")
    assert result.last_change_run is None  # a first run settles no recorded change


def test_preparing_reads_preparing_whatever_the_lock(path: Path) -> None:
    run_id = sup.fake_preparing_run(path)
    result = _status(path)
    assert result.state == "preparing"
    assert result.run is not None and result.run.run_id == run_id
    assert result.run.status == "preparing" and len(result.run.phases) == 8


def _write_progress(path: Path, run_id: int, *, heartbeat: datetime) -> None:
    conn = open_db(path)
    try:
        report = st.get_run(conn, run_id)
    finally:
        conn.close()
    assert report is not None
    progress = RunProgress(run_id=run_id, pid=4242, heartbeat=heartbeat, status="running",
                           reason=report.summary.reason, started_by=report.summary.started_by,
                           trigger_text=report.summary.trigger_text,
                           started_at=report.summary.started_at, percent=58)
    service.progress_path().write_text(progress.model_dump_json(by_alias=True),
                                       encoding="utf-8")


def test_running_is_the_lock_never_the_heartbeat(path: Path) -> None:
    service.start_run(scope="full", reason="button")  # generation 1
    run_id = sup.fake_preparing_run(path, claim=True, started_by="user:guest", reason="prompt")
    _write_progress(path, run_id, heartbeat=datetime.now(UTC))
    with proc_lock.lock(proc_lock.settlement_lock_path(), timeout=2):
        live = _status(path)
        assert live.state == "running" and not live.slow
        assert live.run is not None and live.run.pid == 4242 and live.run.percent == 58
        assert live.run.trigger_text.startswith("你 於 ")
        _write_progress(path, run_id, heartbeat=datetime.now(UTC) - timedelta(seconds=90))
        slow = _status(path)
        assert slow.state == "running" and slow.slow  # 「進行緩慢」, never 已中斷
    gone = _status(path)
    assert gone.state == "interrupted"  # the lock is free and the report is not preparing


def test_a_failed_run_reads_failed(path: Path) -> None:
    stub.PLAN.append("fail")
    service.start_run(scope="full", reason="button")
    assert _status(path).state == "failed"
    service.start_run(scope="full", reason="button")
    assert _status(path).state == "clean"


def test_a_selfcheck_difference_lasts_until_a_later_selfcheck_is_clean(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review E #1: the 04:30 Sunday self-check's difference must not vanish with the 06:40
    nightly run that follows it — it reads 自檢發現差異 until a self-check comes back clean,
    below the active, failed and interrupted states."""
    service.start_run(scope="full", reason="button")
    stub.PLAN.append("selfcheck_diff")
    service.start_automatic(scope="selfcheck", reason="selfcheck")
    assert _status(path).state == "selfcheck_diff"
    service.start_automatic(scope="nightly", reason="nightly")  # 06:40, publishes
    assert _status(path).state == "selfcheck_diff"
    stub.PLAN.append("fail")
    service.start_automatic(scope="nightly", reason="nightly")
    assert _status(path).state == "failed"  # a failure outranks it…
    service.start_run(scope="full", reason="button")
    assert _status(path).state == "selfcheck_diff"  # …and it is still there after
    run_id = sup.fake_preparing_run(path)
    with proc_lock.lock(proc_lock.settlement_lock_path(), timeout=2):
        assert _status(path).state == "preparing"  # an active run outranks it
    service.start_run(scope="full", reason="button")  # ends the stale one, publishes
    assert sup.report(path, run_id).summary.status == "interrupted"
    stub.PLAN.append("fail")
    service.start_automatic(scope="selfcheck", reason="selfcheck")  # checked nothing
    service.start_run(scope="full", reason="button")
    assert _status(path).state == "selfcheck_diff"
    service.start_automatic(scope="selfcheck", reason="selfcheck")  # clean this time
    assert _status(path).state == "clean"
    # a RESUMED self-check is a self-check too
    stub.PLAN.extend(["die_after_claim", "selfcheck_diff"])
    service.start_automatic(scope="selfcheck", reason="selfcheck")
    assert sup.runs(path)[-1].reason == "resume"
    assert _status(path).state == "selfcheck_diff"
    del monkeypatch


def test_an_interrupted_run_not_resumed_reads_interrupted(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "_follow_up", lambda ended: None)
    stub.PLAN.append("die_after_claim")
    service.start_run(scope="full", reason="button")
    result = _status(path)
    assert result.state == "interrupted"
    assert result.last_run is not None and result.last_run.status_text == "結算中斷（未續跑）"


def test_a_change_before_the_window_prompts_one_inside_does_not(path: Path) -> None:
    service.start_run(scope="full", reason="button")
    now = service._now()
    inside = st.window_start(now.date()) + timedelta(days=2)
    sup.back_dated_trade(path, inside)
    quiet = _status(path)
    assert quiet.state == "pending" and not quiet.needs_prompt
    early = date(2026, 2, 2)
    sup.back_dated_trade(path, early)
    ask = _status(path)
    assert ask.state == "pending" and ask.needs_prompt
    assert ask.prompt is not None and ask.prompt.from_ == early
    assert ask.prompt.days == (ask.final_through - early).days + 1  # type: ignore[operator]
    assert ask.prompt.est_s is not None  # the last run's speed × days
    assert ask.pending.records_total == 2
    assert [t.table for t in ask.pending.tables] == ["transactions"]
    assert ask.stale_from == early  # served from the previous version, labelled
    body = json.loads(ask.model_dump_json(by_alias=True))
    assert body["prompt"]["from"] == early.isoformat()


def test_no_prompt_while_a_run_is_active_and_its_changes_read_queued(path: Path) -> None:
    service.start_run(scope="full", reason="button")
    sup.fake_preparing_run(path, claim=True)
    sup.back_dated_trade(path, date(2026, 2, 2))
    with proc_lock.lock(proc_lock.settlement_lock_path(), timeout=2):
        result = _status(path)
    assert result.state == "running" and not result.needs_prompt
    assert result.queued is not None and result.queued.records_total == 1


def test_another_viewer_sees_the_login_not_you(path: Path) -> None:
    service.start_run(scope="dirty", reason="prompt", started_by="user:owner")
    mine = _status(path, viewer="user:owner")
    theirs = _status(path, viewer="user:other")
    assert mine.last_run is not None and mine.last_run.trigger_text.startswith("你 於 ")
    assert theirs.last_run is not None and theirs.last_run.trigger_text.startswith("owner 於 ")


def test_seq_tells_a_second_change_from_an_earlier_one_with_the_same_day_and_counts(
    path: Path,
) -> None:
    """Review F #1: the prompt's 稍後 is remembered per ``seq``. Two back-dated changes on
    one day, settled in between, read the same day and the same counts — only ``seq``
    differs, and it must."""
    service.start_run(scope="full", reason="button")
    early = date(2026, 2, 2)
    sup.back_dated_trade(path, early)
    first = _status(path)
    assert first.needs_prompt and first.seq is not None
    service.start_run(scope="dirty", reason="prompt")
    assert not _status(path).needs_prompt
    sup.back_dated_trade(path, early)
    second = _status(path)
    assert second.needs_prompt and second.seq is not None
    assert (second.dirty_from, second.pending.records_total) == (
        first.dirty_from, first.pending.records_total)
    assert second.seq > first.seq
    assert json.loads(second.model_dump_json(by_alias=True))["seq"] == second.seq

