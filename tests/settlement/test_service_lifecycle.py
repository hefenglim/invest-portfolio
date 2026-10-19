"""The settlement service's run lifecycle, inline (plan §6.2 "Lifecycle", spec §9.4).

The worker is the stub (``tests/settlement/stub_settle_worker.py``); every test asserts what
the PARENT does around it: one commit of the report + ``job_runs`` row before the worker
starts, the DEF-067 verdict and zh detail of the run's row, the end of a run its worker left
unfinished, and the follow-up (chained / resume / queued).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from portfolio_dash.api import settlement_service as service
from portfolio_dash.portfolio import settle_worker
from portfolio_dash.portfolio import settlement_store as st
from portfolio_dash.shared import flight, result_cache
from portfolio_dash.shared.config import get_settings
from tests.contract.test_def073_job_details_speak_chinese import english_words
from tests.reference.corpora import open_db
from tests.settlement import service_support as sup
from tests.settlement import stub_settle_worker as stub
from tests.settlement.conftest import CorpusFactory

_DETAIL = re.compile(r"^重新結算 \d{4}-\d{2}-\d{2} 起 \d+ 天，耗時")


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
    stub.PLAN.append("fail")
    started = service.start_run(scope="full", reason="button", started_by="user:guest")
    assert started.ended is not None and started.ended.status == "failed"
    row = sup.job_row(path, started.run_id)
    assert row["status"] == "error"
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
    for plan in (["publish"], ["fail"], ["die_before_claim"], ["die_after_claim", "publish"],
                 ["selfcheck_diff"]):
        stub.PLAN.extend(plan)
        service.start_run(scope="full", reason="button")
    conn = open_db(path)
    try:
        details = [str(r[0]) for r in conn.execute(
            "SELECT detail FROM job_runs WHERE job_id = 'settlement'")]
    finally:
        conn.close()
    assert len(details) == 6
    for detail in details:
        words = [w for w in english_words(detail) if w not in ("StubDied", "settled_day")]
        assert not words, detail
        assert "," not in detail and ":" not in detail.replace("：", ""), detail



def test_a_job_that_changed_history_before_the_window_starts_a_run(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §9.1: "after a scheduled job whose run raised seq with dirty_from < W"."""
    service.start_run(scope="full", reason="button")
    after = service.after_job_hook("quotes_tw")
    assert after is not None
    after()  # the job changed nothing: no run
    assert len(sup.runs(path)) == 1
    window = st.window_start(service._now().date())
    seq = sup.state(path).seq
    sup.back_dated_trade(path, window + timedelta(days=1))  # inside the window: shown live
    assert service.settle_after_job("quotes_tw", seq_before=seq) is None
    seq = sup.state(path).seq
    sup.back_dated_trade(path, date(2026, 2, 2))  # before the window
    started = service.settle_after_job("quotes_tw", seq_before=seq)
    assert started is not None and started.run_id is not None
    last = sup.runs(path)[-1]
    assert last.reason == "job:quotes_tw" and last.status == "published"
    assert sup.report(path, last.run_id).summary.trigger_text.startswith("排程工作「quotes_tw」")
    monkeypatch.setenv("PD_SETTLE", "off")
    assert service.after_job_hook("quotes_tw") is None
