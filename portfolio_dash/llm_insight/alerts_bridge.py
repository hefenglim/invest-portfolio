"""on_alert bridge (spec 4.9 R7 / 4.10): alert_events table + the R7 dispatch helpers.

Owns two small tables:

- ``alert_events`` — fired spec-03 alert events (rule_id + symbol + fired_at + consumed).
  The ``alert-scan`` scheduler job COMPUTES alerts (reading the dashboard) and records them
  here; this layer only persists/reads them (it imports no pricing — architecture.md).
- ``alert_dispatch_log`` — a per-(task, rule, symbol) dispatch log for the 24h debounce.

The R7 dispatcher consumes new events and, for each ENABLED on_alert insight_type
subscribing to the fired rule ('all' or a list containing it), runs the supplied runner
ONCE per (task, rule, symbol) — debounced 24h on that key. Multiple on_alert combos may
co-exist: a rule firing produces one card per subscribing combo (each billed/debounced
independently, spec 4.9 R7).

Pure ``llm_insight`` persistence: stdlib + ``shared``/``composer_store`` only.
"""

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta

from pydantic import BaseModel

from portfolio_dash.llm_insight import composer_store as cs

# A runner: ``fn(conn, insight_type_id, *, now, fired_rule, fired_symbol, ...)``. Kept
# duck-typed so the scheduler/api seam can register ``insight_service.run_for_id`` without
# this layer importing api.
AlertRunner = Callable[..., object]

_DEBOUNCE_HOURS = 24


class AlertEvent(BaseModel):
    """One fired alert event (an alert-scan observation)."""

    id: int
    rule_id: str
    symbol: str | None
    fired_at: str
    consumed: bool


_DDL = """
CREATE TABLE IF NOT EXISTS alert_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id TEXT NOT NULL,
    symbol TEXT,
    fired_at TEXT NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS alert_dispatch_log (
    debounce_key TEXT NOT NULL,
    dispatched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alert_events_consumed ON alert_events (consumed);
CREATE INDEX IF NOT EXISTS idx_alert_dispatch_key ON alert_dispatch_log (debounce_key);
"""


def ensure_tables(conn: sqlite3.Connection) -> None:
    """Create the alert_events + dispatch-log tables idempotently."""
    conn.executescript(_DDL)
    conn.commit()


# --- events -------------------------------------------------------------------


def _today(now: datetime) -> str:
    return now.date().isoformat()


def record_event(
    conn: sqlite3.Connection, *, rule_id: str, symbol: str | None, now: datetime
) -> int:
    """Append a fired alert event; idempotent per (rule, symbol) PER DAY. Returns the id.

    A re-scan the same day for the same (rule, symbol) does not duplicate an UNCONSUMED
    event — the 24h dispatch debounce is the dispatcher's concern, but this avoids a pile
    of identical rows when the scan runs repeatedly intraday.
    """
    event_id, _ = record_event_ex(conn, rule_id=rule_id, symbol=symbol, now=now)
    return event_id


def record_event_ex(
    conn: sqlite3.Connection, *, rule_id: str, symbol: str | None, now: datetime
) -> tuple[int, bool]:
    """Like :func:`record_event`, but also reports whether a NEW row was written.

    Returns ``(event_id, inserted)`` — ``inserted`` is ``False`` when an existing same-day
    UNCONSUMED event for this (rule, symbol) was reused (coalesced) instead of a new row
    being written. Intra-day repeated flips therefore coalesce to ≤1 event per
    (rule, symbol, day) by design. The signal-scan uses ``inserted`` to count only
    genuinely-recorded events in its ``job_runs.detail`` (deep review 2026-07-10 F2).
    """
    existing = conn.execute(
        "SELECT id FROM alert_events WHERE rule_id = ? AND IFNULL(symbol, '') = IFNULL(?, '') "
        "AND consumed = 0 AND substr(fired_at, 1, 10) = ? ORDER BY id DESC LIMIT 1",
        (rule_id, symbol, _today(now)),
    ).fetchone()
    if existing is not None:
        return int(existing["id"]), False
    cur = conn.execute(
        "INSERT INTO alert_events (rule_id, symbol, fired_at, consumed) VALUES (?, ?, ?, 0)",
        (rule_id, symbol, now.isoformat()),
    )
    conn.commit()
    return int(cur.lastrowid or 0), True


def unconsumed_events(conn: sqlite3.Connection) -> list[AlertEvent]:
    """All not-yet-consumed alert events, oldest first."""
    rows = conn.execute(
        "SELECT id, rule_id, symbol, fired_at, consumed FROM alert_events "
        "WHERE consumed = 0 ORDER BY id"
    ).fetchall()
    return [
        AlertEvent(
            id=r["id"], rule_id=r["rule_id"], symbol=r["symbol"], fired_at=r["fired_at"],
            consumed=bool(r["consumed"]),
        )
        for r in rows
    ]


def mark_consumed(conn: sqlite3.Connection, event_id: int) -> None:
    """Mark an alert event consumed (the dispatcher processed it)."""
    conn.execute("UPDATE alert_events SET consumed = 1 WHERE id = ?", (event_id,))
    conn.commit()


# --- subscribers (R7 filter) --------------------------------------------------


# Signal-transition rule ids (strategy.signal_states EVENT_*) are a technical
# state-transition, NOT a spec-03 RISK alert. The 'all' wildcard means "all RISK alerts",
# so it must NOT pull these in implicitly — a combo subscribes to them ONLY by listing them
# explicitly (deep review 2026-07-10 F4). Mirrors gating._SIGNAL_RULE_PREFIX.
_SIGNAL_RULE_PREFIX = "signal_"


def _subscribes(alert_rules: object, rule_id: str) -> bool:
    """True when a combo's ``alert_rules`` subscribes to *rule_id*.

    An explicit list subscribes to exactly its members (including ``signal_*``). The 'all'
    wildcard subscribes to every RISK alert but EXCLUDES ``signal_*`` transition rules —
    those are opt-in only (deep review 2026-07-10 F4).
    """
    if isinstance(alert_rules, list):
        return rule_id in alert_rules
    if alert_rules == "all":
        return not rule_id.startswith(_SIGNAL_RULE_PREFIX)
    return False


def on_alert_subscribers(conn: sqlite3.Connection, rule_id: str) -> list[cs.InsightType]:
    """ENABLED, non-archived on_alert insight_types subscribing to *rule_id* (R7 filter).

    A combo subscribes when its ``alert_rules`` is ``'all'`` or a list containing the rule.
    """
    out: list[cs.InsightType] = []
    for it in cs.list_insight_types(conn):
        if it.scope != "on_alert" or not it.enabled:
            continue
        if _subscribes(it.alert_rules, rule_id):
            out.append(it)
    return out


# --- 24h debounce on (task, rule, symbol) -------------------------------------


def debounce_key(insight_type_id: int, rule_id: str, symbol: str | None) -> str:
    """The (task, rule, symbol) debounce key (spec 4.9 R7)."""
    return f"{insight_type_id}|{rule_id}|{symbol or ''}"


def recently_dispatched(conn: sqlite3.Connection, key: str, *, now: datetime) -> bool:
    """True when *key* was dispatched within the last 24h (the R7 debounce window)."""
    cutoff = (now - timedelta(hours=_DEBOUNCE_HOURS)).isoformat()
    row = conn.execute(
        "SELECT 1 FROM alert_dispatch_log WHERE debounce_key = ? AND dispatched_at > ? "
        "LIMIT 1",
        (key, cutoff),
    ).fetchone()
    return row is not None


def record_dispatch(conn: sqlite3.Connection, key: str, *, now: datetime) -> None:
    """Log a dispatch for *key* (feeds the 24h debounce window)."""
    conn.execute(
        "INSERT INTO alert_dispatch_log (debounce_key, dispatched_at) VALUES (?, ?)",
        (key, now.isoformat()),
    )
    conn.commit()


# --- dispatcher (R7) ----------------------------------------------------------


def dispatch_alert_events(
    conn: sqlite3.Connection, runner: AlertRunner, *, now: datetime
) -> int:
    """Process unconsumed alert events → run subscribing on_alert combos (R7). Return count.

    For each new event, each ENABLED subscribing on_alert combo runs the supplied runner
    ONCE per (task, rule, symbol), 24h-debounced on that key. Multiple combos each produce
    their own card (billed/debounced independently). The event is marked consumed after all
    its subscribers have been considered. A runner failure for one combo never aborts the
    rest (degrade, never crash).
    """
    dispatched = 0
    for event in unconsumed_events(conn):
        for it in on_alert_subscribers(conn, event.rule_id):
            key = debounce_key(it.id, event.rule_id, event.symbol)
            if recently_dispatched(conn, key, now=now):
                continue
            try:
                runner(
                    conn, it.id, now=now, fired_rule=event.rule_id,
                    fired_symbol=event.symbol,
                )
                record_dispatch(conn, key, now=now)
                dispatched += 1
            except Exception:  # noqa: BLE001 — one combo failing must not abort the rest
                continue
        mark_consumed(conn, event.id)
    return dispatched
