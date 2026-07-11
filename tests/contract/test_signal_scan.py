"""Integration: signals_service.scan_signals over the golden DB.

Covers the scan discipline (deep review 2026-07-10 rulings): first run seeds silently
(ZERO events, hold columns seeded from the evaluation), a manufactured HOLD state fires a
single genuine reversal, a same-day re-scan is idempotent, a same-day genuine SECOND
transition is coalesced (inserted-only count), a params_version change reseeds silently,
and the cache is rebuildable (wipe → rescan → equivalent state).
"""

import sqlite3
from datetime import timedelta
from decimal import Decimal

from portfolio_dash.api.signals_service import scan_signals
from portfolio_dash.llm_insight import alerts_bridge as ab
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Market
from portfolio_dash.strategy import signal_states as ss
from portfolio_dash.strategy.rules.params import PARAMS_VERSION
from tests.conftest import GOLDEN_NOW

# The golden DB holds two positions (2330, AAPL), each with a single stored price → the
# live derived state is all-None for both. Seeding a long monotonic series gives a symbol a
# CONFIRMED trend + non-flat momentum, so a manufactured hold column drives a controlled
# reversal through the scan.


def _events(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT rule_id, symbol FROM alert_events ORDER BY id").fetchall()


def _count(conn: sqlite3.Connection, rule_id: str, symbol: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM alert_events WHERE rule_id = ? AND symbol = ?",
        (rule_id, symbol),
    ).fetchone()
    return int(row["c"])


def _seed_series(
    conn: sqlite3.Connection, symbol: str, market: Market, *, ascending: bool, n: int = 320
) -> None:
    """Seed ``n`` consecutive daily closes ending at the golden clock date. Ascending →
    confirmed uptrend + positive 12-1 momentum; descending → confirmed downtrend + negative.
    Idempotent upsert on (instrument, date) so a re-seed OVERWRITES the same window."""
    end = GOLDEN_NOW.date()
    rows = [
        PriceRow(
            instrument=symbol, market=market, as_of=end - timedelta(days=n - 1 - i),
            close=(Decimal(100) + Decimal(i) * Decimal("2")) if ascending
            else (Decimal(1000) - Decimal(i) * Decimal("2")),
            source="test",
        )
        for i in range(n)
    ]
    upsert_prices(conn, rows, fetched_at=GOLDEN_NOW)


def _manufacture(
    conn: sqlite3.Connection, symbol: str, hold: ss.HoldState, *, version: str = PARAMS_VERSION
) -> None:
    ss.upsert_state(
        conn, symbol, ss.DerivedState(None, None, None, None, None, None),
        hold=hold, params_version=version, as_of="2026-06-10",
        updated_at="2026-06-10T14:30:00+08:00",
    )


def test_first_scan_seeds_zero_events(golden_db: sqlite3.Connection) -> None:
    detail = scan_signals(golden_db, now=GOLDEN_NOW)
    states = ss.all_states(golden_db)
    assert {s.symbol for s in states} == {"2330", "AAPL"}
    assert all(s.params_version == PARAMS_VERSION for s in states)
    # Golden one-price → all-None evaluation → hold columns seeded NULL (silent seed).
    assert all(s.hold == ss.HoldState(None, None) for s in states)
    assert _events(golden_db) == []  # NO event storm on first deploy
    assert "2 seeded" in detail


def test_rescan_same_day_idempotent(golden_db: sqlite3.Connection) -> None:
    scan_signals(golden_db, now=GOLDEN_NOW)
    scan_signals(golden_db, now=GOLDEN_NOW)  # re-scan: no new state → no events
    assert _events(golden_db) == []
    assert len(ss.all_states(golden_db)) == 2


def test_manufactured_reversal_emits_exactly_one_event(golden_db: sqlite3.Connection) -> None:
    # Live 2330 = confirmed uptrend + positive momentum. A stored hold of trend_last_dir=
    # 'down' → the scan sees a genuine down→up reversal and fires signal_trend; the matching
    # momentum_last_sign='positive' is silent. Exactly one event.
    _seed_series(golden_db, "2330", Market.TW, ascending=True)
    _manufacture(golden_db, "2330", ss.HoldState("down", "positive"))
    scan_signals(golden_db, now=GOLDEN_NOW)
    rows = _events(golden_db)
    assert len(rows) == 1
    assert rows[0]["rule_id"] == ss.EVENT_TREND
    assert rows[0]["symbol"] == "2330"


def test_manufactured_reversal_is_idempotent_same_day(golden_db: sqlite3.Connection) -> None:
    _seed_series(golden_db, "2330", Market.TW, ascending=True)
    _manufacture(golden_db, "2330", ss.HoldState("down", "positive"))
    scan_signals(golden_db, now=GOLDEN_NOW)
    scan_signals(golden_db, now=GOLDEN_NOW)  # hold now 'up' → up vs up → no second event
    assert len(_events(golden_db)) == 1


def test_same_day_second_transition_locked_and_counted_once(
    golden_db: sqlite3.Connection,
) -> None:
    # deep review 2026-07-10 F2: a genuine same-day SECOND transition of the same
    # (rule, symbol) inserts nothing (coalesced), and the run detail counts INSERTED only.
    _seed_series(golden_db, "2330", Market.TW, ascending=True)  # live up + positive
    _manufacture(golden_db, "2330", ss.HoldState("down", "positive"))
    detail1 = scan_signals(golden_db, now=GOLDEN_NOW)
    assert "1 transition event(s)" in detail1          # signal_trend inserted
    assert _count(golden_db, ss.EVENT_TREND, "2330") == 1

    # Flip live 2330 to a confirmed DOWNtrend + negative momentum (same day). Trend re-fires
    # up→down but it is LOCKED (already recorded today → no insert); momentum positive→
    # negative is a NEW (rule, symbol) today → inserts. Detected 2, inserted 1.
    _seed_series(golden_db, "2330", Market.TW, ascending=False)  # overwrite window
    detail2 = scan_signals(golden_db, now=GOLDEN_NOW)
    assert "1 transition event(s)" in detail2                    # inserted-only, NOT 2
    assert _count(golden_db, ss.EVENT_TREND, "2330") == 1        # locked: no duplicate row
    assert _count(golden_db, ss.EVENT_MOMENTUM, "2330") == 1     # the genuinely-new insert


def test_params_version_change_reseeds_silently(golden_db: sqlite3.Connection) -> None:
    # A stored row under an OLD params_version whose hold WOULD trigger a transition must be
    # reseeded silently (a recalibration is not a market event); hold resets from the new eval.
    ss.upsert_state(
        golden_db, "2330",
        ss.DerivedState("above_confirmed", "golden", 1, "positive", None, None),
        hold=ss.HoldState("down", "negative"),
        params_version="rules-v0", as_of="2026-06-10", updated_at="t",
    )
    scan_signals(golden_db, now=GOLDEN_NOW)
    assert _events(golden_db) == []  # reseed, no events
    refreshed = ss.get_state(golden_db, "2330")
    assert refreshed is not None
    assert refreshed.params_version == PARAMS_VERSION
    # golden one-price → all-None eval → hold reset to NULL (full silent reseed).
    assert refreshed.hold == ss.HoldState(None, None)


def test_rebuildability_wipe_rescan_equivalent(golden_db: sqlite3.Connection) -> None:
    _seed_series(golden_db, "2330", Market.TW, ascending=True)  # a real (dir, sign) to rebuild
    scan_signals(golden_db, now=GOLDEN_NOW)
    before = {s.symbol: (s.derived, s.hold) for s in ss.all_states(golden_db)}
    ss.clear_all(golden_db)
    assert ss.all_states(golden_db) == []
    scan_signals(golden_db, now=GOLDEN_NOW)  # rebuild from prices (the truth)
    after = {s.symbol: (s.derived, s.hold) for s in ss.all_states(golden_db)}
    assert before == after  # derived state AND hold columns reproduced exactly
    assert _events(golden_db) == []  # a rebuild seeds silently, never fires


def test_scan_records_event_in_alert_events_feed(golden_db: sqlite3.Connection) -> None:
    # The transition event lands in the SAME alert_events feed the on_alert dispatcher and
    # bell consume — proving reuse of the existing event conventions (rule_id + symbol).
    _seed_series(golden_db, "AAPL", Market.US, ascending=True)  # live confirmed uptrend
    _manufacture(golden_db, "AAPL", ss.HoldState("down", "positive"))
    scan_signals(golden_db, now=GOLDEN_NOW)
    unconsumed = ab.unconsumed_events(golden_db)
    assert any(e.rule_id == ss.EVENT_TREND and e.symbol == "AAPL" for e in unconsumed)
