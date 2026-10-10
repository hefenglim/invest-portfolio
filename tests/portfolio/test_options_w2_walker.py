"""Options W2 — the SQL share walker and the "position opened by" rule follow ``OPTION_ADJUST``.

Spec ``docs/spec/2026-10-09-options-ledger.md`` §4.3: the walker (``holdings._delta_of``)
moves the SIGNED count × ``ratio_to``, shorts included; opposite signs at the two ends → skip
and note (D33's sink). It does not replay §4.4, so it is compared with ``build_book`` only on
ledgers with no refused close (the spec's permitted divergence on a 待釐清 ledger).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.holdings import (
    current_shares,
    load_action_index,
    position_exists_by,
    shares_through,
)
from portfolio_dash.data_ingestion.register import register_option_contract
from portfolio_dash.data_ingestion.store import (
    insert_corporate_action,
    insert_transaction,
    load_ledger_bundle,
)
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.models.enums import Side

D = Decimal
ACC = "schwab"
SRC = "XYZ261218P00060000"
DST = "XYZ261218P00020000"


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    seed_accounts(c)
    register_option_contract(c, SRC)
    register_option_contract(c, DST)
    yield c
    c.close()


def _trade(conn: sqlite3.Connection, symbol: str, side: Side, qty: str, day: date, *,
           short: bool = False) -> None:
    insert_transaction(conn, account_id=ACC, symbol=symbol, side=side, quantity=D(qty),
                       price=D("2"), fees=D("0"), tax=D("0"), trade_date=day,
                       short_sale=short)


def _adjust(conn: sqlite3.Connection, ratio_to: str = "3") -> None:
    insert_corporate_action(conn, account_id=ACC, action_date=date(2026, 6, 1),
                            kind=CorporateActionKind.OPTION_ADJUST, from_symbol=SRC,
                            to_symbol=DST, ratio_to=D(ratio_to), ratio_from=D(1))


def _book_shares(conn: sqlite3.Connection) -> dict[str, Decimal]:
    book = build_book(load_ledger_bundle(conn), allow_oversell=True)
    return {o.symbol: o.shares for o in book.opened_positions}


def test_a_short_moves_times_ratio_to_and_agrees_with_the_replay(
    conn: sqlite3.Connection,
) -> None:
    _trade(conn, SRC, Side.SELL, "100", date(2026, 3, 2), short=True)
    _adjust(conn)
    assert current_shares(conn, ACC, SRC) == D(0)
    assert current_shares(conn, ACC, DST) == D(-300)
    assert shares_through(conn, ACC, DST, on=date(2026, 5, 31)) == D(0)
    assert shares_through(conn, ACC, DST, on=date(2026, 6, 1)) == D(-300)
    assert _book_shares(conn) == {SRC: D(0), DST: D(-300)}


def test_a_long_moves_times_ratio_to_and_merges_same_direction(
    conn: sqlite3.Connection,
) -> None:
    _trade(conn, SRC, Side.BUY, "100", date(2026, 3, 2))
    _trade(conn, DST, Side.BUY, "100", date(2026, 3, 3))
    _adjust(conn)
    assert current_shares(conn, ACC, DST) == D(400)
    assert _book_shares(conn) == {SRC: D(0), DST: D(400)}


def test_opposite_signs_are_skipped_and_noted(conn: sqlite3.Connection) -> None:
    _trade(conn, SRC, Side.BUY, "100", date(2026, 3, 2))
    _trade(conn, DST, Side.SELL, "100", date(2026, 3, 3), short=True)
    _adjust(conn)
    index = load_action_index(conn)
    assert current_shares(conn, ACC, SRC, index=index) == D(100)
    assert current_shares(conn, ACC, DST, index=index) == D(-100)
    assert {(ACC, SRC), (ACC, DST)} <= index.negative_side_skips()
    # the replay refuses the same action, so the two still agree
    assert _book_shares(conn) == {SRC: D(100), DST: D(-100)}


def test_an_adjustment_opens_its_destination_in_both_forms(conn: sqlite3.Connection) -> None:
    """``position_exists_by`` (SQL) and ``LedgerBundle.position_opened_by`` — one rule, two
    forms (R9) — both count an ``OPTION_ADJUST`` into the symbol, as the replay does."""
    _trade(conn, SRC, Side.SELL, "100", date(2026, 3, 2), short=True)
    _adjust(conn)
    bundle = load_ledger_bundle(conn)
    for day, expected in ((date(2026, 5, 31), False), (date(2026, 6, 1), True)):
        assert position_exists_by(conn, ACC, DST, day) is expected
        assert bundle.position_opened_by(ACC, DST, day) is expected
