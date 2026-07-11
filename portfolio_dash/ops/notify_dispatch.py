"""Event → phone dispatch: unnotified ``alert_events`` → enabled push channels (WP 3B).

The conn-taking service the scheduler calls at the TAIL of ``alert_scan`` (so it also
covers the ``signal_scan`` events recorded at 14:55, since alert_scan runs at 15:00). It
stays out of ``scheduler/`` (which holds no business logic) and out of ``ops/notify.py``
(the pure channel layer). It imports ONLY ``ops.notify`` + stdlib — never ``llm_insight``/
``api``/``strategy`` — and reaches the ``alert_events`` table by RAW SQL on the passed
connection, which is the established cross-module convention here ("sharing a table is not
importing a module"; cf. ``scheduler.jobs`` writing ``job_runs`` and
``llm_insight.generate`` writing the same). The ``notified_at`` column it reads/writes is
OWNED and created by ``llm_insight.alerts_bridge.ensure_tables`` (the alert-scan job runs
that first) — this module only sets its value.

Dispatch semantics (idempotent, flood-safe):

- Select up to :data:`_CAP` (10) OLDEST events with ``notified_at IS NULL`` — a backlog
  after an outage drips out, never floods the phone.
- Skip when no channel is enabled, or when *now* is inside quiet hours (Asia/Taipei) — the
  events stay unmarked and send on the next scan outside the window.
- Unsubscribed rules are marked handled immediately (they never send, never linger).
- A subscribed event is formatted (zh-TW, from the rule id + symbol only) and fanned out to
  every enabled channel; ``notified_at`` is set ONLY when at least one channel returned ok
  (all-fail → left unmarked to retry next scan; partial success → marked, failure logged).
"""

import logging
import sqlite3
from datetime import datetime

from portfolio_dash.ops import notify

logger = logging.getLogger(__name__)

# At most this many events per dispatch run (oldest first); the rest go next run.
_CAP = 10


def dispatch_notifications(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    sender: notify.Sender = notify.dispatch,
) -> str:
    """Deliver unnotified alert events to the enabled channels; return a short summary.

    ``sender`` is injectable (defaults to :func:`notify.dispatch`) so tests exercise the
    marker/quiet-hours/subscription/cap logic without any network. Never raises for a
    channel failure (the sender isolates those); a summary string is always returned.
    """
    cfg = notify.load_config(conn)
    channels = notify.build_enabled_channels(cfg)
    if not channels:
        return "notify: 無啟用通道"
    if notify.in_quiet_hours(cfg.quiet_hours, now):
        return "notify: 靜音時段"

    rows = conn.execute(
        "SELECT id, rule_id, symbol FROM alert_events WHERE notified_at IS NULL "
        "ORDER BY id LIMIT ?",
        (_CAP,),
    ).fetchall()

    sent = 0
    failed_channels: set[str] = set()
    for row in rows:
        event_id = int(row["id"])
        rule_id = str(row["rule_id"])
        symbol = row["symbol"]
        if not cfg.subscriptions.get(rule_id, True):
            _mark_notified(conn, event_id, now=now)  # unsubscribed → handled, don't linger
            continue
        title, body, severity = notify.format_event(rule_id, symbol)
        outcome = sender(channels, title, body, severity, None)
        if any(result == "ok" for result in outcome.values()):
            _mark_notified(conn, event_id, now=now)
            sent += 1
        for name, result in outcome.items():
            if result != "ok":
                failed_channels.add(name)

    detail = f"notify: {sent} 送出 / {len(rows)} 待送"
    if failed_channels:
        detail += f" (通道異常: {', '.join(sorted(failed_channels))})"
    return detail


def _mark_notified(conn: sqlite3.Connection, event_id: int, *, now: datetime) -> None:
    """Stamp ``alert_events.notified_at`` so this event never dispatches again (idempotent)."""
    conn.execute(
        "UPDATE alert_events SET notified_at = ? WHERE id = ?", (now.isoformat(), event_id)
    )
    conn.commit()


__all__ = ["dispatch_notifications"]
