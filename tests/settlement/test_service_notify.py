"""The failed-settlement push (plan P4, owner ruling Q-P1 = (a): send once when quiet hours end).

Sent the way ``digest_service._push`` sends — guest / demo suppressed, the subscription, quiet
hours — through a new subscription ``settlement_failed`` (「重新結算失敗」, warn, on by default).
Inside quiet hours the report records ``notify_pending`` and a one-shot job at the window's end
sends it once; boot sends a still-pending one when it boots outside quiet hours.

The waiter itself pushes (a failed AUTOMATIC run, here the nightly one); the clock it reads and
the channel it sends to are replaced, so nothing leaves the machine.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from portfolio_dash.api import settlement_service as service
from portfolio_dash.ops import notify
from portfolio_dash.portfolio.settlement_models import RunReport, RunRequest, RunScope, RunSummary
from portfolio_dash.portfolio.settlement_store import TriggerCheck
from portfolio_dash.shared.alert_rule_names import rule_name
from portfolio_dash.shared.config import get_settings
from tests.reference.corpora import open_db
from tests.settlement import service_support as sup
from tests.settlement import stub_settle_worker as stub
from tests.settlement.conftest import CorpusFactory

NIGHT = datetime(2026, 10, 19, 6, 41, tzinfo=sup.TPE)  # inside 22:00–08:00
MORNING = datetime(2026, 10, 19, 8, 0, 5, tzinfo=sup.TPE)
NOON = datetime(2026, 10, 19, 12, 0, tzinfo=sup.TPE)
WINDOW_END = datetime(2026, 10, 19, 8, 0, tzinfo=sup.TPE)


class _Sender:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str | None]] = []

    def __call__(self, channels: list[Any], title: str, body: str, severity: str,
                 link: str | None) -> dict[str, str]:
        self.calls.append((title, body, severity, link))
        return {ch.name: "ok" for ch in channels}


class _Scheduler:
    def __init__(self) -> None:
        self.jobs: list[tuple[Any, Any, str]] = []
        self.options: list[dict[str, Any]] = []

    def add_job(self, fn: Any, trigger: Any, *, id: str,  # noqa: A002 — APScheduler's name
                replace_existing: bool, **options: Any) -> None:
        assert replace_existing
        self.jobs.append((fn, trigger, id))
        self.options.append(options)


class _World:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.now = NIGHT
        self.sent = _Sender()
        self.scheduler = _Scheduler()


@pytest.fixture
def world(corpus_db: CorpusFactory, monkeypatch: pytest.MonkeyPatch) -> Iterator[_World]:
    w = _World(sup.prepare(corpus_db("golden").path, monkeypatch))
    sup.protect(w.path)
    _configure(w.path, quiet=True, subscribed=True)
    monkeypatch.setattr(service, "_now", lambda: w.now)
    monkeypatch.setattr(notify, "dispatch", w.sent)
    service.bind_scheduler(w.scheduler)
    yield w
    service.reset_for_tests()
    get_settings.cache_clear()


def _configure(path: Path, *, quiet: bool, subscribed: bool) -> None:
    conn = open_db(path)
    try:
        cfg = notify.load_config(conn)
        cfg.ntfy.enabled = True
        cfg.quiet_hours = notify.QuietHours(enabled=quiet, start="22:00", end="08:00")
        cfg.subscriptions[notify.SETTLEMENT_FAILED] = subscribed
        notify.save_config(conn, cfg, now=NOON)
    finally:
        conn.close()


def _failed_nightly() -> int:
    stub.PLAN.append("fail")
    started = service.start_automatic(scope="nightly", reason="nightly")
    assert started.ended is not None and started.ended.status == "failed"
    return started.ended.run_id


def _pending(w: _World, run_id: int) -> bool:
    return bool(sup.report(w.path, run_id).notify_pending)


def test_the_subscription_is_named_warned_and_on_by_default(world: _World) -> None:
    assert rule_name(notify.SETTLEMENT_FAILED) == "重新結算失敗"
    assert ("settlement_failed", "重新結算失敗", "warn") in notify.RULE_CATALOG
    conn = open_db(world.path)
    try:
        cfg = notify.load_config(conn)
        cfg.subscriptions.pop(notify.SETTLEMENT_FAILED)
        notify.save_config(conn, cfg, now=NOON)
        assert notify.load_config(conn).subscriptions[notify.SETTLEMENT_FAILED] is True
    finally:
        conn.close()


def test_quiet_hours_end_is_the_next_window_edge() -> None:
    quiet = notify.QuietHours(enabled=True, start="22:00", end="08:00")
    assert notify.quiet_hours_end(quiet, NIGHT) == WINDOW_END
    late = datetime(2026, 10, 19, 23, 0, tzinfo=sup.TPE)
    assert notify.quiet_hours_end(quiet, late) == WINDOW_END + timedelta(days=1)
    assert notify.quiet_hours_end(quiet, NOON) is None
    assert notify.quiet_hours_end(notify.QuietHours(enabled=False), NIGHT) is None


def test_outside_quiet_hours_it_is_sent_now(world: _World) -> None:
    world.now = NOON
    run_id = _failed_nightly()
    [(title, body, severity, _link)] = world.sent.calls
    assert title == "portfolio-dash · 重新結算失敗" and severity == "warn"
    assert body == (f"重新結算失敗（#{run_id}，每晚例行結算）：測試：逐日估值失敗。"
                    "歷史走勢與月度成績維持前一版本，帳戶即時數字不受影響。")
    assert not _pending(world, run_id) and world.scheduler.jobs == []


def test_inside_quiet_hours_it_is_held_then_sent_once_at_the_end(world: _World) -> None:
    run_id = _failed_nightly()
    assert world.sent.calls == [] and _pending(world, run_id)
    [(fn, trigger, job_id)] = world.scheduler.jobs
    assert job_id == service.NOTIFY_JOB_ID
    assert trigger.run_date == WINDOW_END + timedelta(seconds=1)
    # a throttled VM that wakes late still sends it (APScheduler's default grace is 1 s)
    assert world.scheduler.options == [{"misfire_grace_time": None, "coalesce": True}]
    world.now = MORNING  # the window's end: the one-shot job fires
    fn()
    assert len(world.sent.calls) == 1 and not _pending(world, run_id)
    fn()
    assert service.send_pending_notifications() == 0
    assert len(world.sent.calls) == 1


def test_a_boot_inside_quiet_hours_holds_it_again(world: _World) -> None:
    run_id = _failed_nightly()
    service.start_after_boot(service.BootPlan())
    assert world.sent.calls == [] and _pending(world, run_id)
    assert len(world.scheduler.jobs) == 2  # rescheduled for the window's end


def test_two_boots_outside_quiet_hours_send_one_push(world: _World) -> None:
    run_id = _failed_nightly()
    world.now = NOON
    service.start_after_boot(service.BootPlan())
    service.start_after_boot(service.BootPlan())
    assert len(world.sent.calls) == 1 and not _pending(world, run_id)


def test_guest_or_demo_never_pushes(world: _World) -> None:
    conn = open_db(world.path)
    conn.execute("DELETE FROM auth_users")
    conn.commit()
    conn.close()
    held = _failed_nightly()
    world.now = NOON
    sent = _failed_nightly()
    assert service.notify_failure(sent, now=NOON) == "suppressed"
    assert world.sent.calls == [] and world.scheduler.jobs == []
    assert not _pending(world, held) and not _pending(world, sent)


def test_an_unsubscribed_owner_is_never_pushed(world: _World) -> None:
    _configure(world.path, quiet=True, subscribed=False)
    run_id = _failed_nightly()
    world.now = NOON
    assert service.notify_failure(run_id) == "unsubscribed"
    assert world.sent.calls == [] and not _pending(world, run_id)


def test_a_held_push_is_dropped_when_the_owner_unsubscribes_meanwhile(world: _World) -> None:
    run_id = _failed_nightly()
    _configure(world.path, quiet=True, subscribed=False)
    world.now = MORNING
    assert service.send_pending_notifications() == 0
    assert world.sent.calls == [] and not _pending(world, run_id)


def test_only_a_failed_automatic_run_is_pushed(world: _World) -> None:
    world.now = NOON
    stub.PLAN.append("fail")
    service.start_run(scope="full", reason="button", started_by="user:owner")
    assert world.sent.calls == []  # a person started it and sees the failure
    service.start_automatic(scope="nightly", reason="nightly")  # it publishes
    assert world.sent.calls == []
    _failed_nightly()
    assert len(world.sent.calls) == 1


def test_a_held_push_is_dropped_once_a_later_run_published(world: _World) -> None:
    """Review E #7: 「歷史走勢與月度成績維持前一版本」 is no longer true once a later run
    published, so the held push is not sent at the window's end — and its flag is cleared."""
    run_id = _failed_nightly()
    assert _pending(world, run_id)
    service.start_run(scope="full", reason="button")  # the owner settles at 07:00
    world.now = MORNING
    assert service.send_pending_notifications() == 0
    assert world.sent.calls == [] and not _pending(world, run_id)


def test_a_real_scheduler_keeps_the_one_shot_job_through_a_late_wake(world: _World) -> None:
    from apscheduler.schedulers.background import BackgroundScheduler

    real = BackgroundScheduler()
    service.bind_scheduler(real)
    _failed_nightly()
    [job] = real.get_jobs()
    assert job.id == service.NOTIFY_JOB_ID
    assert job.misfire_grace_time is None and job.coalesce is True


SUNDAY_0431 = datetime(2026, 10, 18, 4, 31, tzinfo=sup.TPE)
SUNDAY_0641 = datetime(2026, 10, 18, 6, 41, tzinfo=sup.TPE)


def _failed_selfcheck(world: _World) -> int:
    world.now = SUNDAY_0431  # quiet hours
    stub.PLAN.append("fail")
    started = service.start_automatic(scope="selfcheck", reason="selfcheck")
    assert started.ended is not None and started.ended.status == "failed"
    return started.ended.run_id


def test_a_failed_selfcheck_push_survives_the_nightly(world: _World) -> None:
    """Fold #1 (owner ruling Q-P1 names the self-check): the 06:41 nightly that publishes
    every morning does not do what the 04:31 self-check would have, so the held push is still
    sent — once — when quiet hours end."""
    service.start_run(scope="full", reason="button")  # generation 1
    run_id = _failed_selfcheck(world)
    assert _pending(world, run_id) and world.sent.calls == []
    world.now = SUNDAY_0641
    night = service.start_automatic(scope="nightly", reason="nightly")
    assert night.ended is not None and night.ended.status == "published"
    world.now = MORNING
    assert service.send_pending_notifications() == 1
    [(_title, body, _sev, _link)] = world.sent.calls
    assert f"#{run_id}，每週自檢" in body
    assert service.send_pending_notifications() == 0 and not _pending(world, run_id)


def test_a_failed_selfcheck_is_mooted_only_by_a_completed_selfcheck(world: _World) -> None:
    service.start_run(scope="full", reason="button")
    run_id = _failed_selfcheck(world)
    world.now = SUNDAY_0641
    service.start_run(scope="full", reason="button")  # even a full run is no self-check
    service.start_automatic(scope="selfcheck", reason="selfcheck")  # completes, clean
    world.now = MORNING
    assert service.send_pending_notifications() == 0
    assert world.sent.calls == [] and not _pending(world, run_id)


def test_a_failed_nightly_is_mooted_only_by_a_run_that_covers_its_range(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later PUBLISHED run must hold the failed run's range (``_covers``): a self-check
    publishes but settles no dirt, so it moots nothing; the dirty run that settles the same
    dirt from the same day does."""
    service.start_run(scope="full", reason="button")
    sup.back_dated_trade(world.path, date(2026, 2, 2))
    run_id = _failed_nightly()  # its range starts 2026-02-02
    assert sup.report(world.path, run_id).summary.range_from == date(2026, 2, 2)
    with monkeypatch.context() as m:
        m.setattr(service, "_follow_up", lambda ended: None)  # no chained run after it
        service.start_automatic(scope="selfcheck", reason="selfcheck")
    world.now = MORNING
    assert service.send_pending_notifications() == 1  # the self-check mooted nothing
    world.now = NIGHT
    run_id = _failed_nightly()
    service.start_automatic(scope="nightly", reason="nightly")  # same dirt, same range
    world.now = MORNING
    assert service.send_pending_notifications() == 0 and not _pending(world, run_id)
    assert len(world.sent.calls) == 1


def test_covers_reads_the_ranges() -> None:
    def report(scope: RunScope, lo: date | None, hi: date | None) -> RunReport:
        summary = RunSummary(run_id=1, status="published", status_text="", reason="nightly",
                             trigger_text="t", started_at=NIGHT, range_from=lo, range_to=hi)
        return RunReport(summary=summary, inputs=RunRequest(scope=scope, reason="nightly",
                                                            through=date(2026, 10, 18)))

    failed = report("nightly", date(2026, 3, 1), date(2026, 10, 18))
    assert service._covers(report("full", date(2026, 9, 1), date(2026, 9, 2)), failed)
    assert service._covers(report("dirty", date(2026, 2, 1), date(2026, 10, 18)), failed)
    assert not service._covers(report("dirty", date(2026, 3, 2), date(2026, 10, 18)), failed)
    assert not service._covers(report("dirty", date(2026, 2, 1), date(2026, 10, 17)), failed)
    unclaimed = report("nightly", None, None)  # it never claimed: its through counts
    assert service._covers(report("dirty", date(2026, 9, 1), date(2026, 10, 18)), unclaimed)


def test_boot_pushes_an_automatic_failure_whose_waiter_never_closed_it(
    world: _World,
) -> None:
    """Fold #3 at boot: the worker ended the run as failed, the web process died before the
    waiter closed its job row or pushed — boot closes the row and sends the push."""
    world.now = NOON
    service.start_run(scope="full", reason="button")
    stub.PLAN.append("fail")
    failed = service.start_automatic(scope="nightly", reason="nightly").run_id
    assert failed is not None
    world.sent.calls.clear()
    conn = open_db(world.path)
    conn.execute("UPDATE job_runs SET finished_at = NULL, status = 'running' WHERE id = ?",
                 (failed,))
    conn.commit()
    plan = service.on_boot(conn, trigger_check=TriggerCheck())
    conn.commit()
    conn.close()
    assert plan.push == (failed,)
    service.start_after_boot(plan)
    assert len(world.sent.calls) == 1
    assert sup.job_row(world.path, failed)["status"] == "error"
