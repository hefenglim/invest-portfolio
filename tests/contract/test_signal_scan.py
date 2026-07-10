"""Integration: signals_service.scan_signals over the golden DB.

Covers the scan discipline: first run seeds silently (ZERO events), a manufactured state
change emits exactly one event, a same-day re-scan is idempotent, a params_version change
reseeds silently, and the cache is rebuildable (wipe → rescan → equivalent state).
"""

import sqlite3

from portfolio_dash.api.signals_service import scan_signals
from portfolio_dash.llm_insight import alerts_bridge as ab
from portfolio_dash.strategy import signal_states as ss
from portfolio_dash.strategy.rules.params import PARAMS_VERSION
from tests.conftest import GOLDEN_NOW

# The golden DB holds two positions (2330, AAPL), each with a single stored price → the
# live derived state is all-None for both. That is enough to exercise seed/idempotency and,
# with a manufactured stored row, a controlled single transition.


def _events(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT rule_id, symbol FROM alert_events ORDER BY id"
    ).fetchall()


def test_first_scan_seeds_zero_events(golden_db: sqlite3.Connection) -> None:
    detail = scan_signals(golden_db, now=GOLDEN_NOW)
    states = ss.all_states(golden_db)
    assert {s.symbol for s in states} == {"2330", "AAPL"}
    assert all(s.params_version == PARAMS_VERSION for s in states)
    assert _events(golden_db) == []  # NO event storm on first deploy
    assert "2 seeded" in detail


def test_rescan_same_day_idempotent(golden_db: sqlite3.Connection) -> None:
    scan_signals(golden_db, now=GOLDEN_NOW)
    scan_signals(golden_db, now=GOLDEN_NOW)  # re-scan: no new state → no events
    assert _events(golden_db) == []
    assert len(ss.all_states(golden_db)) == 2


def test_manufactured_change_emits_exactly_one_event(
    golden_db: sqlite3.Connection,
) -> None:
    # Seed a stored row for 2330 whose ONLY difference from the live all-None state is a
    # confirmed uptrend → the scan sees trend up→neutral and fires exactly signal_trend.
    ss.upsert_state(
        golden_db, "2330",
        ss.DerivedState("above_confirmed", None, None, None, None, None),
        params_version=PARAMS_VERSION, as_of="2026-06-10",
        updated_at="2026-06-10T14:30:00+08:00",
    )
    scan_signals(golden_db, now=GOLDEN_NOW)
    rows = _events(golden_db)
    assert len(rows) == 1
    assert rows[0]["rule_id"] == ss.EVENT_TREND
    assert rows[0]["symbol"] == "2330"


def test_manufactured_change_is_idempotent_same_day(
    golden_db: sqlite3.Connection,
) -> None:
    ss.upsert_state(
        golden_db, "2330",
        ss.DerivedState("above_confirmed", None, None, None, None, None),
        params_version=PARAMS_VERSION, as_of="2026-06-10", updated_at="t",
    )
    scan_signals(golden_db, now=GOLDEN_NOW)
    scan_signals(golden_db, now=GOLDEN_NOW)  # state already refreshed → no second event
    rows = _events(golden_db)
    assert len(rows) == 1  # exactly one, not two


def test_params_version_change_reseeds_silently(
    golden_db: sqlite3.Connection,
) -> None:
    # A stored row under an OLD params_version whose state WOULD trigger a transition must
    # be reseeded silently (a recalibration is not a market event).
    ss.upsert_state(
        golden_db, "2330",
        ss.DerivedState("above_confirmed", "golden", 1, "positive", None, None),
        params_version="rules-v0", as_of="2026-06-10", updated_at="t",
    )
    scan_signals(golden_db, now=GOLDEN_NOW)
    assert _events(golden_db) == []  # reseed, no events
    refreshed = ss.get_state(golden_db, "2330")
    assert refreshed is not None
    assert refreshed.params_version == PARAMS_VERSION


def test_rebuildability_wipe_rescan_equivalent(
    golden_db: sqlite3.Connection,
) -> None:
    scan_signals(golden_db, now=GOLDEN_NOW)
    before = {s.symbol: s.derived for s in ss.all_states(golden_db)}
    ss.clear_all(golden_db)
    assert ss.all_states(golden_db) == []
    scan_signals(golden_db, now=GOLDEN_NOW)  # rebuild from prices (the truth)
    after = {s.symbol: s.derived for s in ss.all_states(golden_db)}
    assert before == after  # derived state reproduced exactly
    assert _events(golden_db) == []  # a rebuild seeds silently, never fires


def test_scan_records_event_in_alert_events_feed(
    golden_db: sqlite3.Connection,
) -> None:
    # The transition event lands in the SAME alert_events feed the on_alert dispatcher and
    # bell consume — proving reuse of the existing event conventions (rule_id + symbol).
    ss.upsert_state(
        golden_db, "AAPL",
        ss.DerivedState("below_confirmed", None, None, None, None, None),
        params_version=PARAMS_VERSION, as_of="2026-06-10", updated_at="t",
    )
    scan_signals(golden_db, now=GOLDEN_NOW)
    unconsumed = ab.unconsumed_events(golden_db)
    assert any(e.rule_id == ss.EVENT_TREND and e.symbol == "AAPL" for e in unconsumed)
