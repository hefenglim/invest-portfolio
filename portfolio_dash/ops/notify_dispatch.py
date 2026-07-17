"""Event → phone dispatch: unnotified ``alert_events`` → enabled push channels (WP 3B).

The conn-taking service the scheduler calls at the TAIL of ``alert_scan`` (so it also
covers the ``signal_scan`` events recorded at 14:55, since alert_scan runs at 15:00). It
stays out of ``scheduler/`` (which holds no business logic) and out of ``ops/notify.py``
(the pure channel layer). It imports ONLY ``ops.notify`` + stdlib — never ``llm_insight``/
``api``/``strategy`` — and reaches the ``alert_events`` table by RAW SQL on the passed
connection, which is the established cross-module convention here ("sharing a table is not
importing a module"; cf. ``scheduler.jobs`` writing ``job_runs`` and
``llm_insight.generate`` writing the same). The ``notified_at`` + ``notify_attempts``
columns it reads/writes are OWNED and created by ``llm_insight.alerts_bridge
.ensure_tables`` (the alert-scan job runs that first) — this module only sets values.

Dispatch semantics (idempotent, flood-safe, starvation-free — security review F2):

- Skip when no channel is enabled, or when *now* is inside quiet hours (Asia/Taipei) — the
  events stay unmarked (and un-attempted) and send on the next scan outside the window.
- Select up to :data:`_CAP` (10) OLDEST events with ``notified_at IS NULL AND
  notify_attempts < 3`` — a backlog after an outage drips out, never floods the phone, and
  a permanently-failing head-of-queue drops out of the candidate set after 3 attempts so
  it can never starve newer events.
- Per-event state machine (``notified_at`` × ``notify_attempts``):

  1. **Claim (atomic, BEFORE sending):** ``UPDATE ... SET notified_at = ? WHERE id = ?
     AND notified_at IS NULL``. Rowcount 0 → another runner (cron vs manual run_job)
     claimed it between our SELECT and UPDATE → skip, no double-send.
  2. **Unsubscribed rule:** stays claimed (marked handled) — never sends, never lingers.
  3. **Send** to every enabled channel. ≥1 channel ok → the claim stands (partial
     failure is logged in the summary but the event is done).
  4. **All channels failed:** release the claim and bump the counter
     (``SET notified_at = NULL, notify_attempts = notify_attempts + 1``) → retried next
     scan. The bump that reaches 3 leaves the event permanently unclaimed-but-excluded
     by the ``notify_attempts < 3`` filter (give-up) — observable via the run detail
     (``gave up on N event(s)``) and a warning log.
"""

import logging
import sqlite3
from datetime import datetime

from portfolio_dash.ops import notify

logger = logging.getLogger(__name__)

# At most this many events per dispatch run (oldest first); the rest go next run.
_CAP = 10

# All-channels-failed retries per event; the attempt that reaches this gives up.
_MAX_ATTEMPTS = 3


def dispatch_notifications(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    sender: notify.Sender = notify.dispatch,
) -> str:
    """Deliver unnotified alert events to the enabled channels; return a short summary.

    ``sender`` is injectable (defaults to :func:`notify.dispatch`) so tests exercise the
    claim/attempts/quiet-hours/subscription/cap logic without any network. Never raises
    for a channel failure (the sender isolates those); a summary string is always
    returned. See the module docstring for the per-event state machine.
    """
    cfg = notify.load_config(conn)
    channels = notify.build_enabled_channels(cfg)
    if not channels:
        return "notify: 無啟用通道"
    if notify.in_quiet_hours(cfg.quiet_hours, now):
        return "notify: 靜音時段"
    base = cfg.public_base_url  # FU-D17: empty ⇒ frontend_url returns None ⇒ legacy text

    rows = conn.execute(
        "SELECT id, rule_id, symbol, href FROM alert_events "
        "WHERE notified_at IS NULL AND notify_attempts < ? ORDER BY id LIMIT ?",
        (_MAX_ATTEMPTS, _CAP),
    ).fetchall()

    sent = 0
    gave_up = 0
    failed_channels: set[str] = set()
    for row in rows:
        event_id = int(row["id"])
        if not _claim(conn, event_id, now=now):
            continue  # another runner claimed it between SELECT and UPDATE — skip
        rule_id = str(row["rule_id"])
        if not cfg.subscriptions.get(rule_id, True):
            continue  # unsubscribed → stays claimed (handled), never sends
        # FU-D17: build a clickable deep link from the event's stored href (None ⇒ the
        # dashboard fallback inside frontend_url). Empty base URL ⇒ link is None ⇒ the
        # body keeps its legacy 「請至儀表板查看詳情」 tail (byte-identical legacy behaviour).
        link = notify.frontend_url(base, row["href"])
        title, body, severity = notify.format_event(
            rule_id, row["symbol"], linked=link is not None
        )
        outcome = sender(channels, title, body, severity, link)
        if any(result == "ok" for result in outcome.values()):
            sent += 1  # ≥1 ok → claim stands (partial failure logged below)
        else:
            attempts = _release_and_bump(conn, event_id)
            if attempts >= _MAX_ATTEMPTS:
                gave_up += 1
                logger.warning(
                    "notify dispatch gave up on alert event %d after %d failed attempts",
                    event_id, attempts,
                )
        for name, result in outcome.items():
            if result != "ok":
                failed_channels.add(name)

    detail = f"notify: {sent} 送出 / {len(rows)} 待送"
    if failed_channels:
        detail += f" (通道異常: {', '.join(sorted(failed_channels))})"
    if gave_up:
        detail += f"; gave up on {gave_up} event(s)"
    return detail


def _claim(conn: sqlite3.Connection, event_id: int, *, now: datetime) -> bool:
    """Atomically claim an event for THIS runner (False = someone else already did).

    The ``notified_at IS NULL`` predicate makes the claim a compare-and-set: of two
    concurrent dispatch runs (cron firing while a manual ``run_job`` is in flight) exactly
    one sees rowcount 1 and sends; the other skips — no double push.
    """
    cur = conn.execute(
        "UPDATE alert_events SET notified_at = ? WHERE id = ? AND notified_at IS NULL",
        (now.isoformat(), event_id),
    )
    conn.commit()
    return cur.rowcount > 0


def _release_and_bump(conn: sqlite3.Connection, event_id: int) -> int:
    """All channels failed: release the claim + count the attempt; return the new count."""
    conn.execute(
        "UPDATE alert_events SET notified_at = NULL, "
        "notify_attempts = notify_attempts + 1 WHERE id = ?",
        (event_id,),
    )
    conn.commit()
    row = conn.execute(
        "SELECT notify_attempts FROM alert_events WHERE id = ?", (event_id,)
    ).fetchone()
    return int(row["notify_attempts"]) if row is not None else _MAX_ATTEMPTS


__all__ = ["dispatch_notifications"]
