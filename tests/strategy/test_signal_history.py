"""Store + extractor pins for the signal_history derived cache (W6, AI-D27/AI-D28).

The scan/replay ORCHESTRATION that produces these rows is covered by
tests/contract/test_signal_scan.py and test_signal_history_invalidation.py; here we pin the
conn-bearing store (natural-key idempotency, compare-then-skip, pruning, degrade) and the
None-safe extractor.
"""

import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.strategy import signal_history as sh
from portfolio_dash.strategy.rules.types import Composite, RuleState, SymbolSignals

D = Decimal


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    sh.ensure_table(c)
    yield c
    c.close()


def _rule(state: str, score: str) -> RuleState:
    return RuleState(state=state, score=D(score), evidence={}, window_days=10)


def _signals(
    *,
    trend: tuple[str, str] | None = ("above_confirmed", "1"),
    cross: tuple[str, str] | None = ("golden", "0.85"),
    momentum: tuple[str, str] | None = ("positive", "0.5"),
    rsi: tuple[str, str] | None = ("neutral", "0"),
    composite: bool = True,
) -> SymbolSignals:
    rules: dict[str, RuleState | None] = {
        "trend_filter": _rule(*trend) if trend else None,
        "ma_cross": _rule(*cross) if cross else None,
        "momentum_12_1": _rule(*momentum) if momentum else None,
        "rsi_regime": _rule(*rsi) if rsi else None,
    }
    comp = (
        Composite(
            tech_score=D("72.5"), contributions={}, weights_applied={},
            coverage="4/4", missing=(), evaluation_context="strong_uptrend",
            context_note="x",
        )
        if composite
        else None
    )
    return SymbolSignals(rules=rules, composite=comp, params_version="rules-v1")


def _row(
    symbol: str = "AAA", day: date = date(2026, 6, 1), *, stamp: str = "t0",
    momentum_score: str = "0.5",
) -> sh.SignalHistoryRow:
    signals = _signals(momentum=("positive", momentum_score))
    return sh.row_from_signals(symbol, signals, as_of=day, updated_at=stamp)


# --- extractor ---------------------------------------------------------------


def test_row_from_signals_full_vector() -> None:
    row = sh.row_from_signals(
        "AAA", _signals(), as_of=date(2026, 6, 1), updated_at="t0"
    )
    assert row.trend_state == "above_confirmed"
    assert row.trend_score == D("1")
    assert row.cross_state == "golden"
    assert row.cross_score == D("0.85")
    assert row.momentum_state == "positive"
    assert row.momentum_score == D("0.5")
    assert row.rsi_state == "neutral"
    assert row.rsi_score == D("0")
    assert row.tech_score == D("72.5")
    assert row.evaluation_context == "strong_uptrend"
    assert row.params_version == "rules-v1"


def test_row_from_signals_partial_and_all_none_are_honest_nulls() -> None:
    # rsi not evaluable that day → NULL state AND NULL score, never a fabricated 0.
    row = sh.row_from_signals(
        "AAA", _signals(rsi=None, composite=False),
        as_of=date(2026, 6, 1), updated_at="t0",
    )
    assert row.rsi_state is None and row.rsi_score is None
    assert row.tech_score is None and row.evaluation_context is None
    assert row.trend_score == D("1")

    empty = SymbolSignals(
        rules=dict.fromkeys(
            ("trend_filter", "ma_cross", "momentum_12_1", "rsi_regime"), None
        ),
        composite=None, params_version="rules-v1",
    )
    row2 = sh.row_from_signals(
        "AAA", empty, as_of=date(2026, 6, 1), updated_at="t0"
    )
    assert row2.trend_state is None and row2.momentum_score is None
    assert row2.tech_score is None
    assert row2.params_version == "rules-v1"  # the vintage stamp survives a thin day


# --- store --------------------------------------------------------------------


def test_ensure_table_is_idempotent(conn: sqlite3.Connection) -> None:
    sh.ensure_table(conn)  # second call must not raise
    sh.ensure_table(conn)


def test_upsert_rows_batch_insert_then_natural_key_update(
    conn: sqlite3.Connection,
) -> None:
    assert sh.upsert_rows(conn, []) == 0
    rows = [_row(day=date(2026, 6, i)) for i in (1, 2, 3)]
    assert sh.upsert_rows(conn, rows) == 3
    assert [r.as_of for r in sh.list_rows(conn, "AAA")] == [
        date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3),
    ]
    # Re-upsert the same (symbol, as_of) with changed content → ONE row, new content.
    changed = _row(day=date(2026, 6, 2), stamp="t9", momentum_score="-0.25")
    assert sh.upsert_rows(conn, [changed]) == 1
    stored = sh.list_rows(conn, "AAA")
    assert len(stored) == 3
    middle = stored[1]
    assert middle.momentum_score == D("-0.25")
    assert middle.updated_at == "t9"


def test_upsert_row_if_changed_skips_identical_content(
    conn: sqlite3.Connection,
) -> None:
    assert sh.upsert_row_if_changed(conn, _row()) is True
    # Same content, fresh stamp → skipped, and the stored row keeps the ORIGINAL stamp.
    assert sh.upsert_row_if_changed(conn, _row(stamp="t1")) is False
    stored = sh.list_rows(conn, "AAA")
    assert len(stored) == 1
    assert stored[0].updated_at == "t0"
    # A content change writes and moves the stamp.
    assert sh.upsert_row_if_changed(
        conn, _row(stamp="t2", momentum_score="0.75")
    ) is True
    stored2 = sh.list_rows(conn, "AAA")
    assert stored2[0].momentum_score == D("0.75")
    assert stored2[0].updated_at == "t2"


def test_scores_round_trip_at_full_precision(conn: sqlite3.Connection) -> None:
    sh.upsert_rows(conn, [_row(momentum_score="0.123456789012345678")])
    stored = sh.list_rows(conn, "AAA")
    assert stored[0].momentum_score == D("0.123456789012345678")


def test_list_rows_filters_params_version(conn: sqlite3.Connection) -> None:
    sh.upsert_rows(conn, [_row(day=date(2026, 6, 1))])
    future = _row(day=date(2026, 6, 2))
    object.__setattr__(future, "params_version", "rules-v9")  # frozen dataclass surgery
    sh.upsert_rows(conn, [future])
    assert len(sh.list_rows(conn, "AAA")) == 2
    current = sh.list_rows(conn, "AAA", params_version="rules-v1")
    assert [r.as_of for r in current] == [date(2026, 6, 1)]


def test_as_of_set(conn: sqlite3.Connection) -> None:
    sh.upsert_rows(conn, [_row(day=date(2026, 6, i)) for i in (1, 3)])
    assert sh.as_of_set(conn, "AAA") == {date(2026, 6, 1), date(2026, 6, 3)}
    assert sh.as_of_set(conn, "NOPE") == set()


def test_delete_symbol(conn: sqlite3.Connection) -> None:
    sh.upsert_rows(conn, [_row("AAA", date(2026, 6, 1)), _row("AAA", date(2026, 6, 2)),
                          _row("BBB", date(2026, 6, 1))])
    assert sh.delete_symbol(conn, "AAA") == 2
    assert sh.list_rows(conn, "AAA") == []
    assert len(sh.list_rows(conn, "BBB")) == 1
    assert sh.delete_symbol(conn, "AAA") == 0  # already gone


def test_delete_symbol_degrades_on_a_ledger_only_db() -> None:
    bare = sqlite3.connect(":memory:")  # table never created
    try:
        assert sh.delete_symbol(bare, "AAA") == 0  # no OperationalError escapes
    finally:
        bare.close()


def test_prune_params_version(conn: sqlite3.Connection) -> None:
    sh.upsert_rows(conn, [_row(day=date(2026, 6, 1)), _row(day=date(2026, 6, 2))])
    stale = _row(day=date(2026, 6, 3))
    object.__setattr__(stale, "params_version", "rules-v0")
    sh.upsert_rows(conn, [stale])
    assert sh.prune_params_version(conn, "rules-v1") == 1
    assert [r.as_of for r in sh.list_rows(conn, "AAA")] == [
        date(2026, 6, 1), date(2026, 6, 2),
    ]
    assert sh.prune_params_version(conn, "rules-v1") == 0  # no-op on the current vintage
