"""Dispatch-semantics tests (WP 3B): unnotified alert_events -> enabled channels.

Exercises the claim / attempts / quiet-hours / subscription / cap logic of
``ops.notify_dispatch.dispatch_notifications`` against a real in-memory DB, with the
SENDER injected (a recording fake) so nothing touches the network. The ``notified_at`` +
``notify_attempts`` columns are created by ``alerts_bridge.ensure_tables`` (the owner) —
this proves the two markers (consumed vs notified_at) are independent and idempotent.
Also covers the security-review F2/F3 semantics: atomic claim (no cron-vs-manual double
send), 3-attempt give-up (no head-of-queue starvation), and the alert_scan tail wrap.
"""

import sqlite3
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.llm_insight import alerts_bridge
from portfolio_dash.ops import notify, notify_dispatch
from portfolio_dash.scheduler import jobs

TAIPEI = ZoneInfo("Asia/Taipei")
NOON = datetime(2026, 7, 12, 12, 0, tzinfo=TAIPEI)  # outside the default 22:00-08:00 window


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    alerts_bridge.ensure_tables(c)
    notify.ensure_seeded(c)
    return c


def _enable_ntfy(conn: sqlite3.Connection) -> None:
    cfg = notify.load_config(conn)
    cfg.ntfy.enabled = True  # topic is seeded; that makes it a buildable enabled channel
    notify.save_config(conn, cfg, now=NOON)


def _add_event(conn: sqlite3.Connection, rule_id: str, symbol: str | None) -> int:
    return alerts_bridge.record_event(conn, rule_id=rule_id, symbol=symbol, now=NOON)


def _notified(conn: sqlite3.Connection, event_id: int) -> str | None:
    row = conn.execute(
        "SELECT notified_at FROM alert_events WHERE id = ?", (event_id,)
    ).fetchone()
    value = row["notified_at"]
    return None if value is None else str(value)


def _attempts(conn: sqlite3.Connection, event_id: int) -> int:
    row = conn.execute(
        "SELECT notify_attempts FROM alert_events WHERE id = ?", (event_id,)
    ).fetchone()
    return int(row["notify_attempts"])


class _Sender:
    """A recording fake sender; reports every channel ok, or every channel error."""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.messages: list[tuple[str, str, str]] = []

    def __call__(
        self, channels: list[Any], title: str, body: str, severity: str, link: str | None
    ) -> dict[str, str]:
        self.messages.append((title, body, severity))
        return {ch.name: ("ok" if self.ok else "error: down") for ch in channels}


def test_unmarked_sent_then_marked_and_not_resent(conn: sqlite3.Connection) -> None:
    _enable_ntfy(conn)
    ev = _add_event(conn, "single_weight", "2330")
    sender = _Sender()
    detail = notify_dispatch.dispatch_notifications(conn, now=NOON, sender=sender)
    assert "1 送出" in detail
    assert _notified(conn, ev) is not None
    assert sender.messages and "2330" in sender.messages[0][1]
    # re-scan: already marked -> never resent
    sender2 = _Sender()
    notify_dispatch.dispatch_notifications(conn, now=NOON, sender=sender2)
    assert sender2.messages == []


def test_no_channels_enabled_is_noop(conn: sqlite3.Connection) -> None:
    ev = _add_event(conn, "single_weight", "2330")
    sender = _Sender()
    detail = notify_dispatch.dispatch_notifications(conn, now=NOON, sender=sender)
    assert "無啟用通道" in detail
    assert sender.messages == [] and _notified(conn, ev) is None


def test_subscription_filter_marks_but_does_not_send(conn: sqlite3.Connection) -> None:
    _enable_ntfy(conn)
    cfg = notify.load_config(conn)
    cfg.subscriptions["fx_drift"] = False
    notify.save_config(conn, cfg, now=NOON)
    ev = _add_event(conn, "fx_drift", "schwab")
    sender = _Sender()
    notify_dispatch.dispatch_notifications(conn, now=NOON, sender=sender)
    assert sender.messages == []  # unsubscribed -> not sent
    assert _notified(conn, ev) is not None  # but marked handled so it never lingers


def test_quiet_hours_holds_then_releases(conn: sqlite3.Connection) -> None:
    _enable_ntfy(conn)
    cfg = notify.load_config(conn)
    cfg.quiet_hours.enabled = True
    cfg.quiet_hours.start = "22:00"
    cfg.quiet_hours.end = "08:00"
    notify.save_config(conn, cfg, now=NOON)
    ev = _add_event(conn, "single_weight", "2330")
    inside = datetime(2026, 7, 12, 23, 0, tzinfo=TAIPEI)
    detail = notify_dispatch.dispatch_notifications(conn, now=inside, sender=_Sender())
    assert "靜音時段" in detail and _notified(conn, ev) is None
    # outside the window on the next scan -> delivered
    outside = datetime(2026, 7, 13, 9, 0, tzinfo=TAIPEI)
    notify_dispatch.dispatch_notifications(conn, now=outside, sender=_Sender())
    assert _notified(conn, ev) is not None


def test_all_channels_fail_stays_unmarked(conn: sqlite3.Connection) -> None:
    _enable_ntfy(conn)
    ev = _add_event(conn, "single_weight", "2330")
    detail = notify_dispatch.dispatch_notifications(conn, now=NOON, sender=_Sender(ok=False))
    assert "通道異常" in detail
    assert _notified(conn, ev) is None  # claim released -> retry next scan
    assert _attempts(conn, ev) == 1  # ... with the failed attempt counted (F2)


def test_cap_limits_events_per_run(conn: sqlite3.Connection) -> None:
    _enable_ntfy(conn)
    for i in range(15):
        _add_event(conn, "single_weight", f"S{i}")
    d1 = notify_dispatch.dispatch_notifications(conn, now=NOON, sender=_Sender())
    assert "10 送出" in d1
    remaining = conn.execute(
        "SELECT COUNT(*) AS n FROM alert_events WHERE notified_at IS NULL"
    ).fetchone()["n"]
    assert remaining == 5
    d2 = notify_dispatch.dispatch_notifications(conn, now=NOON, sender=_Sender())
    assert "5 送出" in d2


# --- FU-D17: push deep links --------------------------------------------------


class _CapturingSender:
    """Records the (body, link) the dispatcher passes to the channel fan-out."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def __call__(
        self, channels: list[Any], title: str, body: str, severity: str, link: str | None
    ) -> dict[str, str]:
        self.calls.append((body, link))
        return {ch.name: "ok" for ch in channels}


def _set_base(conn: sqlite3.Connection, base: str) -> None:
    cfg = notify.load_config(conn)
    cfg.public_base_url = base
    notify.save_config(conn, cfg, now=NOON)


def test_deep_link_built_and_passed_when_base_configured(conn: sqlite3.Connection) -> None:
    _enable_ntfy(conn)
    _set_base(conn, "https://invest.example.com")
    # record the event WITH its href, exactly as alert_scan now does.
    alerts_bridge.record_event(
        conn, rule_id="single_weight", symbol="2330", now=NOON, href="/symbol/2330"
    )
    sender = _CapturingSender()
    notify_dispatch.dispatch_notifications(conn, now=NOON, sender=sender)
    body, link = sender.calls[0]
    assert link == "https://invest.example.com/index.html#sym=2330"
    assert "請至儀表板查看詳情" not in body  # linked body drops the redundant tail


def test_no_link_and_legacy_text_when_base_empty(conn: sqlite3.Connection) -> None:
    _enable_ntfy(conn)  # default config → public_base_url == "" (deep links off)
    alerts_bridge.record_event(
        conn, rule_id="single_weight", symbol="2330", now=NOON, href="/symbol/2330"
    )
    sender = _CapturingSender()
    notify_dispatch.dispatch_notifications(conn, now=NOON, sender=sender)
    body, link = sender.calls[0]
    assert link is None  # no base URL → no deep link
    assert "請至儀表板查看詳情" in body  # byte-identical legacy text


def test_link_falls_back_to_dashboard_for_hrefless_event(conn: sqlite3.Connection) -> None:
    # a global/legacy event with no stored href still gets a dashboard deep link.
    _enable_ntfy(conn)
    _set_base(conn, "https://invest.example.com")
    alerts_bridge.record_event(conn, rule_id="quota_low", symbol=None, now=NOON)  # no href
    sender = _CapturingSender()
    notify_dispatch.dispatch_notifications(conn, now=NOON, sender=sender)
    _, link = sender.calls[0]
    assert link == "https://invest.example.com/index.html"


def test_signal_event_included_when_subscribed(conn: sqlite3.Connection) -> None:
    _enable_ntfy(conn)
    ev = _add_event(conn, "signal_trend", "2330")
    sender = _Sender()
    notify_dispatch.dispatch_notifications(conn, now=NOON, sender=sender)
    assert sender.messages and "趨勢反轉" in sender.messages[0][0]
    assert _notified(conn, ev) is not None


# --- F2/F3: give-up (starvation), claim race, alert_scan tail wrap --------------


def test_starvation_gives_up_after_three_attempts_and_queue_advances(
    conn: sqlite3.Connection,
) -> None:
    """F3.4: a permanently-failing head-of-queue must not starve newer events.

    10 old events + a permanently-down channel: after 3 scans their ``notify_attempts``
    hit 3 and the ``< 3`` filter excludes them, so scan 4 (channel recovered) reaches the
    NEWER events. The give-up is observable in the scan-3 run detail.
    """
    _enable_ntfy(conn)
    old_ids = [_add_event(conn, "single_weight", f"OLD{i}") for i in range(10)]
    new_ids = [_add_event(conn, "single_weight", f"NEW{i}") for i in range(2)]

    details = [
        notify_dispatch.dispatch_notifications(conn, now=NOON, sender=_Sender(ok=False))
        for _ in range(3)
    ]
    assert all("0 送出 / 10 待送" in d for d in details)  # oldest 10 selected every scan
    assert "gave up" not in details[0] and "gave up" not in details[1]
    assert "gave up on 10 event(s)" in details[2]  # the attempt that reaches 3 gives up
    assert [_attempts(conn, i) for i in old_ids] == [3] * 10
    assert all(_notified(conn, i) is None for i in old_ids)  # unclaimed-but-excluded

    # scan 4: channel recovered -> the queue has ADVANCED past the poisoned head
    sender = _Sender()
    d4 = notify_dispatch.dispatch_notifications(conn, now=NOON, sender=sender)
    assert "2 送出 / 2 待送" in d4
    assert len(sender.messages) == 2
    assert "NEW0" in sender.messages[0][1] and "NEW1" in sender.messages[1][1]
    assert all(_notified(conn, i) is not None for i in new_ids)
    assert all(_notified(conn, i) is None for i in old_ids)  # given up, never re-tried


def test_claim_race_second_runner_skips_no_double_send(conn: sqlite3.Connection) -> None:
    """F3.5: the atomic claim closes the cron-vs-manual race — rowcount 0 -> skip.

    Simulated by a sender that, while event A is being sent, claims event B directly
    (the "other runner" landing between our SELECT and our claim UPDATE). The dispatch
    loop must then see rowcount 0 for B and skip it without sending.
    """
    _enable_ntfy(conn)
    _add_event(conn, "single_weight", "AAA")
    b = _add_event(conn, "single_weight", "BBB")

    class _RacingSender(_Sender):
        def __call__(
            self, channels: list[Any], title: str, body: str, severity: str,
            link: str | None,
        ) -> dict[str, str]:
            conn.execute(
                "UPDATE alert_events SET notified_at = 'other-runner' "
                "WHERE id = ? AND notified_at IS NULL",
                (b,),
            )
            conn.commit()
            return super().__call__(channels, title, body, severity, link)

    sender = _RacingSender()
    detail = notify_dispatch.dispatch_notifications(conn, now=NOON, sender=sender)
    assert len(sender.messages) == 1  # only A sent; B's claim was lost -> skipped
    assert "AAA" in sender.messages[0][1]
    assert _notified(conn, b) == "other-runner"  # the other runner's stamp stands
    assert "1 送出 / 2 待送" in detail


def test_alert_scan_survives_dispatch_failure(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3.6: a notify-dispatch crash degrades to ``notify: error`` — the scan stays ok."""
    jobs.create_scheduler_tables(conn)
    monkeypatch.setattr(jobs, "_INSIGHT_RUNNER", None)
    monkeypatch.setattr(jobs, "_compute_alerts_for_scan", lambda conn, *, now: [])

    def _boom(conn: sqlite3.Connection, *, now: datetime, sender: Any = None) -> str:
        raise RuntimeError("push path exploded")

    monkeypatch.setattr(jobs.notify_dispatch, "dispatch_notifications", _boom)
    run_id = jobs.run_job(conn, "alert_scan", now=NOON)
    row = conn.execute(
        "SELECT status, detail FROM job_runs WHERE id = ?", (run_id,)
    ).fetchone()
    assert row["status"] == "ok"  # the scan itself never fails over the push path
    assert "notify: error" in row["detail"]
    assert "push path exploded" not in row["detail"]  # wrapped, not propagated
