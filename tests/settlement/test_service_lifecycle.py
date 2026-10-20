"""The settlement service's run lifecycle, inline (plan §6.2 "Lifecycle", spec §9.4).

The worker is the stub (``tests/settlement/stub_settle_worker.py``); every test asserts what
the PARENT does around it: one commit of the report + ``job_runs`` row before the worker
starts, the DEF-067 verdict and zh detail of the run's row, the end of a run its worker left
unfinished, and the follow-up (chained / resume / queued).
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from portfolio_dash.api import settlement_service as service
from portfolio_dash.data_ingestion.store import insert_transaction
from portfolio_dash.portfolio import settle_worker
from portfolio_dash.portfolio import settlement_store as st
from portfolio_dash.portfolio.settlement_models import (
    RunReport,
    RunRequest,
    RunSummary,
    SelfcheckResult,
)
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared import flight, proc_lock, result_cache
from portfolio_dash.shared.config import get_settings
from portfolio_dash.shared.db import install_actor
from portfolio_dash.shared.enums import Market
from portfolio_dash.shared.models.enums import Side
from tests.contract.test_def073_job_details_speak_chinese import english_words
from tests.reference.corpora import open_db
from tests.settlement import service_support as sup
from tests.settlement import stub_settle_worker as stub
from tests.settlement.conftest import CorpusFactory

_DETAIL = re.compile(r"^重新結算 \d{4}-\d{2}-\d{2} 起 \d+ 天(（結算 \d+ 個走勢日）)?，耗時")


@pytest.fixture
def path(corpus_db: CorpusFactory, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    yield sup.prepare(corpus_db("golden").path, monkeypatch)
    service.reset_for_tests()
    get_settings.cache_clear()


def test_a_run_publishes_and_its_job_row_says_so_in_chinese(path: Path) -> None:
    started = service.start_run(scope="full", reason="button", started_by="user:guest")
    assert started.ended is not None and started.ended.status == "published"
    row = sup.job_row(path, started.run_id)
    assert row["job_id"] == "settlement" and row["status"] == "ok"
    assert row["finished_at"] is not None and row["reason"] is None
    assert _DETAIL.match(row["detail"]), row["detail"]
    assert not english_words(row["detail"])
    report = sup.report(path, started.run_id)
    assert report.summary.status == "published" and report.summary.finished_at is not None
    assert report.summary.trigger_text.startswith(service.STARTED_BY_TOKEN)
    assert sup.state(path).generation == 1
    assert service.active_run_id() is None


def test_the_report_and_job_row_are_committed_before_the_worker_starts(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child reads its RunRequest by run id from ANOTHER connection (spec §9.4)."""
    seen: dict[str, Any] = {}

    def worker(run_id: int, *, db_path: Path) -> None:
        other = open_db(db_path)
        try:
            seen["job"] = other.execute("SELECT status, finished_at FROM job_runs WHERE id = ?",
                                        (run_id,)).fetchone()
            seen["report"] = st.get_run(other, run_id)
        finally:
            other.close()
        stub.run(run_id, db_path=db_path)

    monkeypatch.setattr(settle_worker, "run", worker)
    started = service.start_run(scope="dirty", reason="prompt", started_by="user:guest")
    assert tuple(seen["job"]) == ("running", None)
    assert seen["report"].summary.status == "preparing"
    assert seen["report"].inputs.scope == "dirty"
    assert seen["report"].inputs.started_by == "user:guest"
    assert sup.report(path, started.run_id).summary.status == "published"


def test_a_run_while_another_is_active_is_refused_and_settling_off_starts_nothing(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = sup.fake_preparing_run(path)
    with proc_lock.lock(proc_lock.settlement_lock_path(), timeout=2):  # its child is alive
        with pytest.raises(service.AlreadyRunning) as busy:
            service.start_run(scope="full", reason="button")
        assert busy.value.run_id == active and busy.value.code == "already_running"
        # An automatic start queues behind it instead of failing.
        queued = service.start_automatic(scope="nightly", reason="nightly")
        assert queued.queued_behind == active and queued.run_id is None
    monkeypatch.setenv("PD_SETTLE", "off")
    with pytest.raises(service.SettlementOff):
        service.start_run(scope="full", reason="button")
    assert service.start_automatic(scope="nightly", reason="nightly") == service.AutoStart()


def test_a_failed_run_is_an_error_row_with_its_class_and_phase(path: Path) -> None:
    stub.PLAN.append("fail")  # as the real worker: ends the run itself, returns normally
    started = service.start_run(scope="full", reason="button", started_by="user:guest")
    assert started.ended is not None and started.ended.status == "failed"
    row = sup.job_row(path, started.run_id)
    assert row["status"] == "error"
    # The class comes from the worker's RunFailure.error_class (the child exits 0).
    assert row["reason"] == "failed:RuntimeError"
    assert row["detail"] == "重新結算失敗（逐日估值）：測試：逐日估值失敗"
    failure = sup.report(path, started.run_id).failure
    assert failure is not None and failure.consequence  # derived from the state
    assert "目前尚無結算版本" in failure.consequence
    assert sup.PUSHES == []  # a run a person started is not pushed


def test_a_worker_that_dies_before_claiming_ends_failed(path: Path) -> None:
    stub.PLAN.append("die_before_claim")
    started = service.start_run(scope="full", reason="button")
    report = sup.report(path, started.run_id)
    assert report.summary.status == "failed"
    assert report.failure is not None and "開始結算前" in report.failure.reason_text
    row = sup.job_row(path, started.run_id)
    assert (row["status"], row["reason"]) == ("error", "failed:StubDied")
    assert not english_words(row["detail"].replace("StubDied", ""))
    assert len(sup.runs(path)) == 1  # a failure is not resumed


def test_an_abnormal_exit_after_the_claim_is_interrupted_and_resumed_once(path: Path) -> None:
    stub.PLAN.extend(["die_after_claim", "publish"])
    started = service.start_run(scope="full", reason="button")
    first, second = sup.runs(path)
    assert first.run_id == started.run_id
    assert first.status == "interrupted" and first.resumed and first.next_run_id == second.run_id
    assert first.status_text == f"中斷，已續跑（#{second.run_id}）"
    assert second.reason == "resume" and second.previous_run_id == first.run_id
    assert second.status == "published"
    row = sup.job_row(path, first.run_id)
    assert (row["status"], row["reason"]) == ("error", "interrupted")
    assert row["detail"].startswith("重新結算中斷（逐日估值）：結算程序異常結束")
    assert sup.report(path, second.run_id).summary.trigger_text == \
        f"續跑中斷的結算 #{first.run_id}"


def test_the_waiter_resumes_at_most_once_in_a_row(path: Path) -> None:
    stub.PLAN.extend(["die_after_claim", "die_after_claim", "publish"])
    service.start_run(scope="full", reason="button")
    first, second = sup.runs(path)
    assert first.resumed and second.reason == "resume"
    assert second.status == "interrupted" and not second.resumed
    assert second.status_text == "結算中斷（未續跑）"
    assert sup.PUSHES == [second.run_id]  # automatic and nothing follows: pushed
    assert sup.state(path).dirty_from == date(2026, 1, 5)  # the claimed range is dirty again


def test_a_change_during_a_run_chains_a_follow_up_from_its_day(path: Path) -> None:
    service.start_run(scope="full", reason="button")  # generation 1
    stub.PLAN.extend(["change_during", "publish"])
    started = service.start_run(scope="full", reason="button")
    first, second = sup.runs(path)[1:]
    assert first.run_id == started.run_id and first.status in ("partial", "superseded")
    assert second.reason == "chained" and second.previous_run_id == first.run_id
    assert second.status == "published"
    assert sup.report(path, second.run_id).summary.trigger_text == \
        f"接續 #{first.run_id} 結算期間新增的變更"
    assert sup.state(path).dirty_from is None
    assert sup.job_row(path, first.run_id)["status"] == "ok"  # nothing was lost


def test_a_scheduled_run_while_one_is_active_waits_for_it(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcomes: list[Any] = []

    def worker(run_id: int, *, db_path: Path) -> None:
        if not outcomes:
            outcomes.append(service.scheduled_run(None, "nightly",  # type: ignore[arg-type]
                                                  now=service._now()))
        stub.run(run_id, db_path=db_path)

    monkeypatch.setattr(settle_worker, "run", worker)
    started = service.start_run(scope="full", reason="button")
    [outcome] = outcomes
    assert outcome.status == "ok"
    assert outcome.detail == f"重新結算 #{started.run_id} 正在進行，每晚例行結算排在其後"
    first, second = sup.runs(path)
    assert second.reason == "nightly" and second.status == "published"
    assert second.previous_run_id is None  # a queued run is not a chain link


def test_an_inline_run_cannot_be_cancelled_and_nothing_running_is_refused(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(service.NotRunning):
        service.cancel(by="user:guest")
    raised: list[BaseException] = []

    def worker(run_id: int, *, db_path: Path) -> None:
        try:
            service.cancel(by="user:guest")
        except service.SettlementError as exc:
            raised.append(exc)
        stub.run(run_id, db_path=db_path)

    monkeypatch.setattr(settle_worker, "run", worker)
    service.start_run(scope="full", reason="button")
    assert [type(e) for e in raised] == [service.CannotCancel]


def test_the_waiter_notes_the_write_and_prewarms_once_after_the_last_run(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notes: list[int] = []
    warms: list[float] = []
    real_note = flight.note_write

    def note() -> None:
        notes.append(1)
        real_note()

    def prewarm(fn: object, delay: float) -> bool:
        warms.append(delay)
        return True

    monkeypatch.setattr(flight, "note_write", note)
    monkeypatch.setattr(result_cache, "schedule_prewarm", prewarm)
    monkeypatch.setenv("PD_PREWARM", "1")
    service.start_run(scope="full", reason="button")
    stub.PLAN.extend(["change_during", "publish"])
    service.start_run(scope="full", reason="button")
    assert len(notes) >= 3  # one per finished run (the sessions add their own)
    assert warms == [service.PREWARM_DELAY_S, service.PREWARM_DELAY_S]  # not between a chain


def test_build_request_never_moves_settled_through_back(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job just enabled: final_through lands before settled_through → the request keeps
    the settled days (and the claim clamps the same way)."""
    service.start_run(scope="full", reason="button")
    settled = sup.state(path).settled_through
    assert settled is not None
    early = settled - timedelta(days=10)
    monkeypatch.setattr(service, "final_through",
                        lambda conn, now, *, since=None: service.FinalThrough(through=early))
    conn = open_db(path)
    try:
        request = service.build_request(conn, scope="dirty", reason="prompt", started_by=None,
                                        now=service._now())
        selfcheck = service.build_request(conn, scope="selfcheck", reason="selfcheck",
                                          started_by=None, now=service._now())
    finally:
        conn.close()
    assert request.through == settled
    assert selfcheck.through == early  # the self-check claims nothing; its claim bounds it


def test_the_trigger_sentences(path: Path) -> None:
    del path
    now = datetime(2026, 10, 18, 10, 32, tzinfo=sup.TPE)
    assert service.trigger_text("prompt", now=now, cause="交易變更") == \
        "{started_by} 於 10:32 啟動（確認提示：交易變更）"
    assert service.trigger_text("button", now=now).startswith("{started_by} 於 10:32 啟動")
    assert service.trigger_text("boot", now=now, why="首次結算") == "系統啟動時自動執行（首次結算）"
    assert service.trigger_text("nightly", now=now) == "每晚例行結算（10:32）"
    assert service.trigger_text("job:quotes_tw", now=now).startswith("排程工作「quotes_tw」")
    assert service.render_trigger("{started_by} 於 10:32 啟動", "user:owner", "user:owner") == \
        "你 於 10:32 啟動"
    assert service.render_trigger("{started_by} 於 10:32 啟動", "user:owner", "user:other") == \
        "owner 於 10:32 啟動"
    assert service.render_trigger("{started_by} 於 10:32 啟動", "user:guest", None) == \
        "訪客 於 10:32 啟動"


def test_every_run_detail_is_a_chinese_sentence(path: Path) -> None:
    """DEF-073 over every end status the service can write (the identifiers aside)."""
    service.start_run(scope="full", reason="button")
    for plan in (["fail"], ["die_before_claim"], ["die_after_claim", "publish"]):
        stub.PLAN.extend(plan)
        service.start_run(scope="full", reason="button")
    for plan in (["selfcheck_diff"], ["publish"]):  # a self-check with, then without, a difference
        stub.PLAN.extend(plan)
        service.start_automatic(scope="selfcheck", reason="selfcheck")
    conn = open_db(path)
    try:
        details = [str(r[0]) for r in conn.execute(
            "SELECT detail FROM job_runs WHERE job_id = 'settlement'")]
    finally:
        conn.close()
    assert len(details) == 7
    assert any(d.startswith("每週自檢發現差異：3 處，最早於 ") and "（逐日結算）" in d
               for d in details), details
    assert any(d.startswith("每週自檢 ") and "與已發布版本一致" in d for d in details), details
    for detail in details:
        words = [w for w in english_words(detail) if w != "StubDied"]
        assert not words, detail
        assert "," not in detail and ":" not in detail.replace("：", ""), detail



def _mark(path: Path) -> service.TrailMark:
    conn = open_db(path)
    try:
        mark = service.trail_mark(conn)
    finally:
        conn.close()
    assert mark is not None
    return mark


def _job_writes(path: Path, *, price_day: date | None = None,
                trade_day: date | None = None) -> None:
    """Writes a job's connection makes (actor ``job:history_daily``, as ``trigger_job``)."""
    conn = open_db(path)
    try:
        install_actor(conn, "job:history_daily")
        if price_day is not None:
            upsert_prices(conn, [PriceRow(instrument="2330", market=Market.TW, as_of=price_day,
                                          close=Decimal("612.5"), source="test")],
                          fetched_at=service._now())
        if trade_day is not None:
            insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                               quantity=Decimal("1"), price=Decimal("500"), fees=Decimal("0"),
                               tax=Decimal("0"), trade_date=trade_day)
        conn.commit()
    finally:
        conn.close()


def test_a_quote_dated_after_settled_through_is_live_and_never_recorded(path: Path) -> None:
    """Review E #9, verified: a quote job's own writes are today's (or the last session's)
    closes, after ``settled_through`` — the trail's bound — so they raise no ``seq`` at all."""
    service.start_run(scope="full", reason="button")
    settled = sup.state(path).settled_through
    assert settled is not None
    seq = sup.state(path).seq
    _job_writes(path, price_day=settled + timedelta(days=1))
    assert sup.state(path).seq == seq and sup.state(path).dirty_from is None


def test_a_job_that_changed_history_before_the_window_starts_a_run(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §9.1: "after a scheduled job whose run raised seq with dirty_from < W" — counted
    on the JOB'S OWN changes (review E #9), never the trail's global dirty day."""
    service.start_run(scope="full", reason="button")
    after = service.after_job_hook("history_daily")
    assert after is not None
    after()  # the job changed nothing: no run
    assert len(sup.runs(path)) == 1
    window = st.window_start(service._now().date())
    mark = _mark(path)
    _job_writes(path, price_day=window + timedelta(days=1))  # a revised close inside the window
    assert service.settle_after_job("history_daily", mark=mark) is None
    # The OWNER's pending change before the window is the prompt's, not the job's — made
    # before the job's mark, or WHILE the job runs (after the mark): only the job's own rows
    # count (fold #4, M9b).
    sup.back_dated_trade(path, date(2026, 2, 2))
    mark = _mark(path)
    _job_writes(path, price_day=window + timedelta(days=2))
    assert service.settle_after_job("history_daily", mark=mark) is None
    mark = _mark(path)
    owner = open_db(path)
    try:
        install_actor(owner, "user:owner")
        insert_transaction(owner, account_id="tw_broker", symbol="2330", side=Side.BUY,
                           quantity=Decimal("1"), price=Decimal("500"), fees=Decimal("0"),
                           tax=Decimal("0"), trade_date=date(2026, 2, 4))
        owner.commit()
    finally:
        owner.close()
    _job_writes(path, price_day=window + timedelta(days=3))
    assert service.settle_after_job("history_daily", mark=mark) is None
    assert len(sup.runs(path)) == 1
    # The job revises a close before the window: it starts the run.
    mark = _mark(path)
    _job_writes(path, price_day=date(2026, 3, 2))
    started = service.settle_after_job("history_daily", mark=mark)
    assert started is not None and started.run_id is not None
    last = sup.runs(path)[-1]
    assert last.reason == "job:history_daily" and last.status == "published"
    assert sup.report(path, last.run_id).summary.trigger_text.startswith(
        "排程工作「history_daily」")
    # its own trail rows count too (a ledger write on the job's connection)
    mark = _mark(path)
    _job_writes(path, trade_day=date(2026, 2, 3))
    assert service.settle_after_job("history_daily", mark=mark) is not None
    monkeypatch.setenv("PD_SETTLE", "off")
    assert service.after_job_hook("history_daily") is None


def _owner_price(path: Path, day: date, close: str = "612.5") -> None:
    """The owner revises a 2330 close (a ``prices`` counter: counters carry no actor)."""
    conn = open_db(path)
    try:
        install_actor(conn, "user:owner")
        upsert_prices(conn, [PriceRow(instrument="2330", market=Market.TW, as_of=day,
                                      close=Decimal(close), source="test")],
                      fetched_at=service._now())
        conn.commit()
    finally:
        conn.close()


def _cancel(path: Path, run_id: int) -> None:
    """What the waiter does for a cancelled (or dead) run: the claimed changes go back."""
    conn = open_db(path)
    try:
        assert st.end_without_publish(conn, run_id, "cancelled")
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("merge", [False, True], ids=["unclaimed", "merged"])
def test_a_counter_put_back_after_the_jobs_mark_is_not_the_jobs(
    path: Path, merge: bool
) -> None:
    """Review of the v0.1.32 follow-ups: the owner cancels a re-settlement while a job waits
    behind it. The settle child's turn ends, the job's mark is taken, and only THEN does the
    waiter put the cancelled run's claimed counters back (``on_exit`` runs after the gate is
    released). A mark of PENDING counters alone read the owner's put-back counter as the
    job's change. The mark now sums every unsettled counter per key, claimed ones included,
    so a put-back — un-claimed in place, or merged into a pending counter of the same key —
    changes nothing, while an earlier day the job writes still counts."""
    service.start_run(scope="full", reason="button")
    window = st.window_start(service._now().date())
    _owner_price(path, date(2026, 3, 2))                 # the owner's pending revision ...
    run_id = sup.fake_preparing_run(path, claim=True)    # ... claimed by a re-settlement
    if merge:
        _owner_price(path, date(2026, 4, 1), "613")      # a pending counter of the same key
    mark = _mark(path)                                   # the queued job's turn comes
    _cancel(path, run_id)                                # the waiter puts the claim back
    _job_writes(path, price_day=window + timedelta(days=1))   # the job revises a close
    assert service.settle_after_job("history_daily", mark=mark) is None
    runs = len(sup.runs(path))
    # the job's own revision dated BEFORE every pending day is still the job's
    mark = _mark(path)
    _job_writes(path, price_day=date(2026, 2, 2))
    started = service.settle_after_job("history_daily", mark=mark)
    assert started is not None and started.run_id is not None
    assert len(sup.runs(path)) == runs + 1


def test_a_busy_database_is_retried_by_the_waiter(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review E #4: one ``database is locked`` while finalising does not leave the run open."""
    real = service._finalise
    calls = {"n": 0}

    def flaky(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*args, **kwargs)

    monkeypatch.setattr(service, "_finalise", flaky)
    monkeypatch.setattr(service, "FINALISE_BACKOFF_S", 0.01)
    started = service.start_run(scope="full", reason="button")
    assert calls["n"] == 2
    assert sup.report(path, started.run_id).summary.status == "published"
    assert sup.job_row(path, started.run_id)["status"] == "ok"


def test_a_run_the_waiter_could_not_finish_is_ended_by_the_next_start(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review E #4: a waiter that fails for good leaves a ``running`` report with no run here
    and its lock free; the next start (manual or automatic) ends it 中斷 instead of answering
    409 or queueing behind it forever."""
    service.start_run(scope="full", reason="button")  # generation 1
    real = service._finalise
    failing = {"on": True}

    def broken(*args: Any, **kwargs: Any) -> Any:
        if failing["on"]:
            raise RuntimeError("the waiter's session fails")
        return real(*args, **kwargs)

    monkeypatch.setattr(service, "_finalise", broken)
    stub.PLAN.append("die_after_claim")
    stuck = service.start_run(scope="full", reason="button").run_id
    assert sup.report(path, stuck).summary.status == "running"
    assert service.active_run_id() is None
    failing["on"] = False
    nightly = service.start_automatic(scope="nightly", reason="nightly")
    assert nightly.run_id is not None and nightly.queued_behind is None
    ended = sup.report(path, stuck)
    assert ended.summary.status == "interrupted"
    assert ended.failure is not None
    assert ended.failure.reason_text == "結算程序已不在執行，紀錄未能結束"
    assert sup.job_row(path, stuck)["status"] == "error"
    assert sup.report(path, nightly.run_id).summary.status == "published"


def test_a_cancel_drops_only_its_own_follow_up() -> None:
    """Review E #6: no chained run and no resume after a cancel, but a run QUEUED behind it
    (its job row already said 「排在其後」) still starts."""
    service.reset_for_tests()
    cancelled = service.RunEnd(run_id=4, status="cancelled", job_status="skipped", detail="",
                               reason="button", scope="full", dirty_after=date(2026, 2, 2))
    assert service._follow_up(cancelled) is None  # dirt left: still no chained run
    queued = service._Pending(scope="nightly", reason="nightly")
    service._enqueue(queued)
    assert service._follow_up(cancelled) == queued
    assert service._S.queue == []


def test_a_resumed_selfcheck_names_its_difference() -> None:
    """Review E #5: keyed on the run's scope, not its reason."""
    summary = RunSummary(run_id=7, status="published", status_text="完成", reason="resume",
                         trigger_text="x", started_at=datetime(2026, 10, 18, 4, 30,
                                                               tzinfo=sup.TPE),
                         range_from=date(2025, 1, 2), days=600, duration_s=12.0,
                         selfcheck_diff=True)
    report = RunReport(summary=summary,
                       inputs=RunRequest(scope="selfcheck", reason="resume",
                                         through=date(2026, 10, 17)),
                       selfcheck=SelfcheckResult(differences=2, first_day=date(2025, 3, 3),
                                                 first_table="settled_period"))
    assert service.run_detail(report) == \
        "每週自檢發現差異：2 處，最早於 2025-03-03（月結與年結），與已發布版本不同，耗時 12 秒"
    assert service._job_reason("published", report, None) == "selfcheck_diff"


def _fail_waiter_once(monkeypatch: pytest.MonkeyPatch,
                      before: Any = None) -> dict[str, int]:
    """The waiter's finalisation fails once (the session dies), as for a locked database
    past every retry; *before* runs at that moment."""
    real = service._finalise_with_retry
    calls = {"n": 0}

    def failing(run_id: int, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            if before is not None:
                before()
            return None
        return real(run_id, **kwargs)

    monkeypatch.setattr(service, "_finalise_with_retry", failing)
    return calls


def test_waiter_failure_starts_the_queued_run(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review E #4 (fold #4, M4c): a waiter that cannot finalise still starts the run queued
    behind it, which first closes the job row the waiter left open (fold #3)."""
    service.start_run(scope="full", reason="button")
    _fail_waiter_once(monkeypatch, before=lambda: service._enqueue(
        service._Pending(scope="nightly", reason="nightly")))
    stuck = service.start_run(scope="full", reason="button").run_id
    runs = sup.runs(path)
    assert runs[-1].reason == "nightly" and runs[-1].status == "published"
    assert sup.report(path, stuck).summary.status == "published"  # the worker ended it
    row = sup.job_row(path, stuck)
    assert row["status"] == "ok" and row["finished_at"] is not None
    assert service._S.queue == []


def test_a_job_row_left_running_behind_an_ended_report_is_closed_by_the_next_start(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fold #3: the real worker's normal path ends the report itself; a waiter that fails
    after it leaves the job row ``running``. The next start closes it through
    ``job_status_for`` and sends the push an automatic failure never got."""
    service.start_run(scope="full", reason="button")
    _fail_waiter_once(monkeypatch)
    stub.PLAN.append("fail")
    failed = service.start_automatic(scope="nightly", reason="nightly").run_id
    assert failed is not None
    assert sup.report(path, failed).summary.status == "failed"
    assert sup.job_row(path, failed)["finished_at"] is None
    assert sup.PUSHES == []
    service.start_run(scope="full", reason="button")
    row = sup.job_row(path, failed)
    assert (row["status"], row["reason"]) == ("error", "failed:RuntimeError")
    assert row["detail"] == "重新結算失敗（逐日估值）：測試：逐日估值失敗"
    assert sup.PUSHES == [failed]  # the automatic failure's push, once
    service.start_run(scope="full", reason="button")
    assert sup.PUSHES == [failed]


def test_the_detail_names_the_valued_days_when_the_span_is_longer(path: Path) -> None:
    """A full run's span starts at the ledger's first row (a deposit can precede the first
    trade): the detail gives the calendar span and, when it differs, the trend days valued."""
    started = service.start_run(scope="full", reason="button")
    report = sup.report(path, started.run_id)
    days = report.summary.days
    assert days
    longer = report.model_copy(update={
        "range": report.range.model_copy(update={"valued_days": days - 1})})
    assert f"起 {days} 天（結算 {days - 1} 個走勢日），耗時" in service.run_detail(longer)
    same = report.model_copy(update={
        "range": report.range.model_copy(update={"valued_days": days})})
    assert "走勢日" not in service.run_detail(same)
    assert english_words(service.run_detail(longer)) == []

