"""``on_boot`` + ``start_after_boot`` (plan §6.2 "on_boot", spec §9.1 boot row, §9.6).

``on_boot`` runs on the lifespan's boot session and only DECIDES; the start happens after the
session committed (``start_after_boot``), because the worker reads its request from another
connection. Order: (1) always end the orphans — even with ``PD_SETTLE=off``; (2) calc version
or recreated triggers → full; (3) resume an interrupted range; (4) empty store → full unless
the ledger is empty; (5) an identical boot starts nothing.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from portfolio_dash.api import settlement_service as service
from portfolio_dash.portfolio import settlement_store as st
from portfolio_dash.portfolio.settlement_store import TriggerCheck
from portfolio_dash.scheduler import jobs
from portfolio_dash.shared.config import get_settings
from tests.reference.corpora import open_db
from tests.settlement import service_support as sup
from tests.settlement.conftest import CorpusFactory


@pytest.fixture
def path(corpus_db: CorpusFactory, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    yield sup.prepare(corpus_db("golden").path, monkeypatch)
    service.reset_for_tests()
    get_settings.cache_clear()


def _boot(path: Path, check: TriggerCheck | None = None) -> service.BootPlan:
    conn = open_db(path)
    try:
        plan = service.on_boot(conn, trigger_check=check or TriggerCheck())
        conn.commit()
        return plan
    finally:
        conn.close()


def test_orphans_end_even_with_settling_off_and_nothing_starts(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orphan = sup.fake_preparing_run(path)
    monkeypatch.setenv("PD_SETTLE", "off")
    plan = _boot(path)
    assert plan == service.BootPlan(interrupted=(orphan,))
    report = sup.report(path, orphan)
    assert report.summary.status == "interrupted" and not report.summary.resumed
    assert report.summary.status_text == "結算中斷（未續跑）"
    assert report.failure is not None
    assert report.failure.reason_text == "系統重新啟動時結算尚未完成"
    row = sup.job_row(path, orphan)
    assert (row["status"], row["reason"]) == ("error", "interrupted")
    assert row["detail"].startswith("重新結算中斷")


def test_an_empty_store_settles_in_full_after_the_boot_session(path: Path) -> None:
    plan = _boot(path)
    assert plan.start == service.BootStart(scope="full", reason="boot", why="首次結算")
    run_id = service.start_after_boot(plan)
    assert run_id is not None
    report = sup.report(path, run_id)
    assert report.summary.status == "published" and report.summary.reason == "boot"
    assert report.summary.trigger_text == "系統啟動時自動執行（首次結算）"
    assert sup.state(path).generation == 1


def test_an_empty_ledger_starts_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.conftest import init_golden_base

    empty = tmp_path / "empty.db"
    conn = open_db(empty)
    init_golden_base(conn)
    conn.commit()
    conn.close()
    sup.prepare(empty, monkeypatch)
    assert _boot(empty) == service.BootPlan()
    assert service.start_after_boot(service.BootPlan()) is None


def test_an_identical_boot_starts_nothing(path: Path) -> None:
    service.start_after_boot(_boot(path))
    assert _boot(path) == service.BootPlan()
    assert _boot(path) == service.BootPlan()


def test_a_calc_version_change_settles_in_full_and_the_interrupted_run_is_not_resumed(
    path: Path,
) -> None:
    service.start_after_boot(_boot(path))
    orphan = sup.fake_preparing_run(path)
    conn = open_db(path)
    conn.execute("UPDATE settle_meta SET calc_version = 'older-code' WHERE id = 1")
    conn.commit()
    conn.close()
    plan = _boot(path)
    assert plan.start == service.BootStart(scope="full", reason="boot", why="計算規則更新")
    assert plan.interrupted == (orphan,)
    failure = sup.report(path, orphan).failure
    assert failure is not None
    assert failure.consequence == "未續跑：系統啟動時改為完整重新結算（計算規則更新）"


def test_a_recreated_trigger_settles_in_full(path: Path) -> None:
    service.start_after_boot(_boot(path))
    check = TriggerCheck(created=("settle_trg_transactions_ins",), tables=("transactions",))
    plan = _boot(path, check)
    assert plan.start == service.BootStart(scope="full", reason="boot",
                                           why="觸發器重建：交易")


def test_an_interrupted_range_resumes_and_is_marked_resumed(path: Path) -> None:
    service.start_after_boot(_boot(path))
    sup.back_dated_trade(path, sup.state(path).settled_through.replace(day=1))  # type: ignore[union-attr]
    orphan = sup.fake_preparing_run(path, claim=True)  # claimed, no worker: a dead child
    plan = _boot(path)
    assert plan.interrupted == (orphan,)
    assert plan.start == service.BootStart(scope="dirty", reason="resume",
                                           previous_run_id=orphan, mark_resumed=True)
    resumed = service.start_after_boot(plan)
    assert resumed is not None
    before = sup.report(path, orphan)
    assert before.summary.resumed and before.summary.next_run_id == resumed
    assert before.summary.status_text == f"中斷，已續跑（#{resumed}）"
    after = sup.report(path, resumed)
    assert after.summary.previous_run_id == orphan and after.summary.status == "published"
    assert sup.state(path).dirty_from is None
    # the next boot finds nothing to resume
    assert _boot(path) == service.BootPlan()


def test_a_settlement_job_row_left_open_is_closed(path: Path) -> None:
    """A web process that died after its run ended (or before its report existed)."""
    from portfolio_dash.shared.clock import app_now

    conn = open_db(path)
    lone = jobs.start_job_run(conn, jobs.SETTLEMENT_JOB_ID, now=app_now())
    conn.close()
    service.start_run(scope="full", reason="button")
    finished = sup.runs(path)[-1].run_id
    conn = open_db(path)
    conn.execute("UPDATE job_runs SET finished_at = NULL, status = 'running' WHERE id = ?",
                 (finished,))
    conn.commit()
    conn.close()
    _boot(path)
    assert sup.job_row(path, lone)["status"] == "error"
    assert sup.job_row(path, lone)["detail"] == "重新結算未能開始：系統在建立紀錄前中斷"
    assert sup.job_row(path, finished)["status"] == "ok"
    assert sup.job_row(path, finished)["finished_at"] is not None


def test_boot_writes_only_rows_never_the_schema(path: Path) -> None:
    """on_boot runs before trigger_schema_version is recorded: it must not move it."""
    sup.fake_preparing_run(path)
    conn = open_db(path)
    try:
        before = int(conn.execute("PRAGMA schema_version").fetchone()[0])
        service.on_boot(conn, trigger_check=TriggerCheck())
        conn.commit()
        assert int(conn.execute("PRAGMA schema_version").fetchone()[0]) == before
        assert st.triggers_verified(conn)
    finally:
        conn.close()
