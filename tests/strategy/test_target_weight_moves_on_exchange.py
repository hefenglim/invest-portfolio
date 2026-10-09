"""An EXCHANGE re-keys the owner's target weight, so it does not strand on a dead ticker.

`target_weights_config` is a single-row JSON map keyed by SYMBOL STRING, and nothing re-keyed
it until 2026-08-16. A merger or ticker rename therefore left the target filed under a symbol
the ledger no longer holds, where it could never be met — it surfaced only as a permanent
`rebalance.excluded_with_target` entry, one indirection away from the event that caused it.

**Why it was missed when D47 did the same thing for the price-alert band:** a weight is
config that nothing recomputes. There is no second number for it to disagree with, so no
reconciliation goes red and no replay notices. The band at least fires an alert.

SPLIT is immune and stays untested-by-omission on purpose: a ratio is unitless, so a 7-for-1
leaves 「25% of the portfolio」 meaning exactly what it meant. Only the ticker-changing kinds
can strand a key, and of those only EXCHANGE re-keys a position — see the SPINOFF case below.
"""

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.strategy.target_weights import (
    load_target_weights,
    move_target_weight,
    save_target_weights,
)

D = Decimal
NOW = datetime(2026, 8, 16, tzinfo=UTC)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    return c


def _set(conn: sqlite3.Connection, **weights: str) -> None:
    save_target_weights(conn, {s: D(w) for s, w in weights.items()}, now=NOW)


def test_the_weight_moves_and_the_dead_ticker_keeps_none(conn: sqlite3.Connection) -> None:
    """★ Clearing the source is the half that fixes the defect rather than duplicating it."""
    _set(conn, OLD="0.25", KEEP="0.10")
    assert move_target_weight(conn, from_symbol="OLD", to_symbol="NEW", now=NOW) == D("0.25")

    after = load_target_weights(conn)
    assert after == {"NEW": D("0.25"), "KEEP": D("0.10")}
    assert "OLD" not in after


def test_the_value_is_not_rescaled(conn: sqlite3.Connection) -> None:
    """An EXCHANGE re-keys a position; it does not change what share of the portfolio the
    owner wants in it. (A ratio is also immune to the ratio terms on the action row — that
    is why a SPLIT needs nothing here at all.)"""
    _set(conn, OLD="0.3333")
    move_target_weight(conn, from_symbol="OLD", to_symbol="NEW", now=NOW)
    assert load_target_weights(conn)["NEW"] == D("0.3333")


def test_a_destination_the_owner_already_targeted_is_never_overwritten(
    conn: sqlite3.Connection,
) -> None:
    """D47 parity. That number is their judgement about the destination security, and no
    merge rule (sum? max? replace?) could be right for every merger.

    ⚠ The consequence is stated rather than hidden: the SOURCE's weight stays, and stays
    stranded. Deleting it silently would be worse — it is a value the owner typed, and
    `excluded_with_target` is where an unsatisfiable target belongs.
    """
    _set(conn, OLD="0.25", NEW="0.40")
    assert move_target_weight(conn, from_symbol="OLD", to_symbol="NEW", now=NOW) is None
    assert load_target_weights(conn) == {"OLD": D("0.25"), "NEW": D("0.40")}


def test_nothing_to_move_is_a_quiet_no_op(conn: sqlite3.Connection) -> None:
    _set(conn, OTHER="0.10")
    assert move_target_weight(conn, from_symbol="OLD", to_symbol="NEW", now=NOW) is None
    # Same symbol both ends is a SPLIT's shape (E20) and never a re-key.
    _set(conn, OLD="0.25")
    assert move_target_weight(conn, from_symbol="OLD", to_symbol="OLD", now=NOW) is None
    assert load_target_weights(conn)["OLD"] == D("0.25")


def test_moving_twice_is_idempotent(conn: sqlite3.Connection) -> None:
    """One event, N account rows (D13). The CSV door dedups by pair, and this makes that
    dedup a belt rather than the only thing standing between the owner and a lost weight."""
    _set(conn, OLD="0.25")
    assert move_target_weight(conn, from_symbol="OLD", to_symbol="NEW", now=NOW) is not None
    assert move_target_weight(conn, from_symbol="OLD", to_symbol="NEW", now=NOW) is None
    assert load_target_weights(conn) == {"NEW": D("0.25")}


def test_the_sum_is_preserved_so_no_validation_can_start_failing(
    conn: sqlite3.Connection,
) -> None:
    """The write seam validates Σ ≤ 1. A re-key moves one entry without changing its value,
    so the sum is invariant by construction — a move can never make a previously-valid
    config unsavable."""
    _set(conn, OLD="0.60", KEEP="0.40")
    before = sum(load_target_weights(conn).values())
    move_target_weight(conn, from_symbol="OLD", to_symbol="NEW", now=NOW)
    assert sum(load_target_weights(conn).values()) == before == D("1.00")


def test_a_ledger_with_no_targets_is_untouched(conn: sqlite3.Connection) -> None:
    """The common case: most ledgers set no targets at all, and this must not create the
    config row as a side effect of recording a merger."""
    assert move_target_weight(conn, from_symbol="OLD", to_symbol="NEW", now=NOW) is None
    assert load_target_weights(conn) == {}
