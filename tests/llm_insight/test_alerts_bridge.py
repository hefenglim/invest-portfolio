"""Unit tests for the on_alert bridge (spec 04.9 R7 / 4.10).

``alerts_bridge`` owns the ``alert_events`` table + the R7 dispatch helpers: it records
fired alert events, finds the ENABLED on_alert insight_types subscribing to a rule
('all' or a rule list), and 24h-debounces on the (task, rule, symbol) key. PURE
``llm_insight`` (stdlib + shared + composer_store) — it never imports pricing; the alert
COMPUTATION (which reads the dashboard) is the scheduler job's concern.
"""

import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.llm_insight import alerts_bridge as ab
from portfolio_dash.llm_insight import composer_store as cs

NOW = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    cs.ensure_seeded(c)
    ab.ensure_tables(c)
    yield c
    c.close()


def test_ensure_tables_creates_alert_events(conn: sqlite3.Connection) -> None:
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "alert_events" in tables
    ab.ensure_tables(conn)  # idempotent


def test_ensure_tables_migrates_legacy_schema_without_notify_columns() -> None:
    # Deploy-gate regression (2026-07-12): a LIVE DB has alert_events created by the
    # pre-notify schema (no notified_at/notify_attempts). ensure_tables must migrate
    # it without crashing — the notified_at index must be created AFTER the column
    # migration, never inside the initial DDL script. Fresh-DB fixtures cannot see
    # this ordering class, so this test seeds the legacy shape explicitly.
    c = sqlite3.connect(":memory:")
    try:
        c.execute(
            "CREATE TABLE alert_events ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, rule_id TEXT NOT NULL,"
            " symbol TEXT, fired_at TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0)"
        )
        c.execute(
            "INSERT INTO alert_events (rule_id, symbol, fired_at) VALUES ('fx_drift', 's', 'x')"
        )
        ab.ensure_tables(c)  # must not raise on the legacy shape
        cols = {r[1] for r in c.execute("PRAGMA table_info(alert_events)")}
        assert {"notified_at", "notify_attempts"} <= cols
        indexes = {r[1] for r in c.execute("PRAGMA index_list(alert_events)")}
        assert "idx_alert_events_notified" in indexes
        row = c.execute(
            "SELECT notified_at, notify_attempts FROM alert_events"
        ).fetchone()
        assert row == (None, 0)  # legacy row inherits honest defaults
    finally:
        c.close()


# --- record + read events -----------------------------------------------------


def test_record_and_list_unconsumed_events(conn: sqlite3.Connection) -> None:
    e1 = ab.record_event(conn, rule_id="fx_drift", symbol="schwab", now=NOW)
    ab.record_event(conn, rule_id="single_weight", symbol="2330", now=NOW)
    assert e1 > 0
    events = ab.unconsumed_events(conn)
    assert {(e.rule_id, e.symbol) for e in events} == {
        ("fx_drift", "schwab"), ("single_weight", "2330"),
    }


def test_record_event_is_idempotent_same_day(conn: sqlite3.Connection) -> None:
    # The same (rule, symbol) firing again the same day does not duplicate the event row.
    ab.record_event(conn, rule_id="fx_drift", symbol="schwab", now=NOW)
    ab.record_event(conn, rule_id="fx_drift", symbol="schwab", now=NOW + timedelta(minutes=5))
    assert len(ab.unconsumed_events(conn)) == 1


def test_mark_consumed_hides_event(conn: sqlite3.Connection) -> None:
    eid = ab.record_event(conn, rule_id="fx_drift", symbol="schwab", now=NOW)
    ab.mark_consumed(conn, eid)
    assert ab.unconsumed_events(conn) == []


# --- subscribers (R7 filter) --------------------------------------------------


def test_subscribers_match_rule_list_and_all(conn: sqlite3.Connection) -> None:
    sub_all = cs.create_insight_type(
        conn, name="All", scope="on_alert", alert_rules="all", enabled=True, now=NOW
    )
    sub_fx = cs.create_insight_type(
        conn, name="FX", scope="on_alert", alert_rules=["fx_drift"], enabled=True, now=NOW
    )
    cs.create_insight_type(
        conn, name="Other", scope="on_alert", alert_rules=["single_weight"], enabled=True,
        now=NOW,
    )
    ids = {it.id for it in ab.on_alert_subscribers(conn, "fx_drift")}
    assert ids == {sub_all.id, sub_fx.id}


def test_all_wildcard_excludes_signal_rules(conn: sqlite3.Connection) -> None:
    # deep review 2026-07-10 F4: 'all' means "all RISK alerts" — it must NOT implicitly
    # subscribe to signal_* transition rules; an explicit list still does.
    sub_all = cs.create_insight_type(
        conn, name="All", scope="on_alert", alert_rules="all", enabled=True, now=NOW
    )
    sub_signal = cs.create_insight_type(
        conn, name="Sig", scope="on_alert", alert_rules=["signal_trend"], enabled=True,
        now=NOW,
    )
    # 'all' does NOT pull in signal_trend; only the explicit combo does.
    sig_ids = {it.id for it in ab.on_alert_subscribers(conn, "signal_trend")}
    assert sig_ids == {sub_signal.id}
    # 'all' still subscribes to a genuine RISK rule.
    assert sub_all.id in {it.id for it in ab.on_alert_subscribers(conn, "fx_drift")}


def test_subscribers_exclude_disabled_and_non_on_alert(conn: sqlite3.Connection) -> None:
    cs.create_insight_type(
        conn, name="Disabled", scope="on_alert", alert_rules="all", enabled=False, now=NOW
    )
    cs.create_insight_type(
        conn, name="Portfolio", scope="portfolio", enabled=True, now=NOW
    )
    assert ab.on_alert_subscribers(conn, "fx_drift") == []


# --- 24h debounce on (task, rule, symbol) -------------------------------------


def test_debounce_blocks_within_24h(conn: sqlite3.Connection) -> None:
    key = "5|fx_drift|schwab"
    assert ab.recently_dispatched(conn, key, now=NOW) is False
    ab.record_dispatch(conn, key, now=NOW)
    assert ab.recently_dispatched(conn, key, now=NOW + timedelta(hours=1)) is True
    assert ab.recently_dispatched(conn, key, now=NOW + timedelta(hours=23)) is True


def test_debounce_clears_after_24h(conn: sqlite3.Connection) -> None:
    key = "5|fx_drift|schwab"
    ab.record_dispatch(conn, key, now=NOW)
    assert ab.recently_dispatched(conn, key, now=NOW + timedelta(hours=25)) is False


def test_debounce_independent_per_key(conn: sqlite3.Connection) -> None:
    ab.record_dispatch(conn, "5|fx_drift|schwab", now=NOW)
    # a different task/rule/symbol key is unaffected
    assert ab.recently_dispatched(conn, "6|fx_drift|schwab", now=NOW) is False
    assert ab.recently_dispatched(conn, "5|single_weight|schwab", now=NOW) is False


def test_debounce_key_helper() -> None:
    assert ab.debounce_key(5, "fx_drift", "schwab") == "5|fx_drift|schwab"
    assert ab.debounce_key(5, "single_weight", None) == "5|single_weight|"


# --- dispatcher (R7) ----------------------------------------------------------


def test_dispatch_runs_each_subscriber_once_and_debounces(conn: sqlite3.Connection) -> None:
    sub = cs.create_insight_type(
        conn, name="FX", scope="on_alert", alert_rules=["fx_drift"], enabled=True, now=NOW
    )
    ab.record_event(conn, rule_id="fx_drift", symbol="schwab", now=NOW)
    calls: list[tuple[int, str, str]] = []

    def runner(c: sqlite3.Connection, insight_type_id: int, *, now: datetime,
               fired_rule: str, fired_symbol: str) -> None:
        calls.append((insight_type_id, fired_rule, fired_symbol))

    ab.dispatch_alert_events(conn, runner, now=NOW)
    assert calls == [(sub.id, "fx_drift", "schwab")]
    # a second dispatch (same event consumed; debounce holds) does not re-run
    calls.clear()
    ab.record_event(conn, rule_id="fx_drift", symbol="schwab", now=NOW + timedelta(hours=1))
    ab.dispatch_alert_events(conn, runner, now=NOW + timedelta(hours=1))
    assert calls == []  # debounced within 24h


def test_dispatch_no_subscribers_is_noop(conn: sqlite3.Connection) -> None:
    ab.record_event(conn, rule_id="fx_drift", symbol="schwab", now=NOW)
    calls: list[int] = []

    def runner(c: sqlite3.Connection, insight_type_id: int, **kw: object) -> None:
        calls.append(insight_type_id)

    ab.dispatch_alert_events(conn, runner, now=NOW)
    assert calls == []
    # the event is still marked consumed (no subscriber wants it)
    assert ab.unconsumed_events(conn) == []
