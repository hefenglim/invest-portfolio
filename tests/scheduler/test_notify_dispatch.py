"""Dispatch-semantics tests (WP 3B): unnotified alert_events -> enabled channels.

Exercises the marker / quiet-hours / subscription / cap logic of
``ops.notify_dispatch.dispatch_notifications`` against a real in-memory DB, with the
SENDER injected (a recording fake) so nothing touches the network. The ``notified_at``
column is created by ``alerts_bridge.ensure_tables`` (the owner) — this proves the two
markers (consumed vs notified_at) are independent and idempotent.
"""

import sqlite3
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.llm_insight import alerts_bridge
from portfolio_dash.ops import notify, notify_dispatch

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
    assert _notified(conn, ev) is None  # retry next scan


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


def test_signal_event_included_when_subscribed(conn: sqlite3.Connection) -> None:
    _enable_ntfy(conn)
    ev = _add_event(conn, "signal_trend", "2330")
    sender = _Sender()
    notify_dispatch.dispatch_notifications(conn, now=NOON, sender=sender)
    assert sender.messages and "趨勢反轉" in sender.messages[0][0]
    assert _notified(conn, ev) is not None
