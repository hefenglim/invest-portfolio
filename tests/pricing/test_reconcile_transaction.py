"""The price-basis reconcile reads and writes in ONE transaction (review of the split-window fix).

``reconcile_prices`` used to SELECT a symbol's rows in autocommit and UPDATE them afterwards. A
quote or history write landing in between left the row derived from the OLD raw value beside
the NEW one — measured: ``close`` 400 over ``close_raw`` 20 / ``split_basis`` 1, breaking
``close = cap(close_raw × split_basis)`` until the next fetch or reconcile. With no transaction
open on the connection, the reconcile now takes the write lock (``BEGIN IMMEDIATE``) BEFORE the
read, so a concurrent writer waits until it commits. A caller that already holds a transaction
keeps it: nothing is begun inside it.

Real files in WAL mode (``shared.db.connect_path``), because the race needs two connections.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.pricing.reconcile import reconcile_prices
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.schema import create_tables
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.corporate_actions import (
    ActionIndex,
    CorporateAction,
    CorporateActionKind,
    split_factor,
)
from portfolio_dash.shared.db import connect_path
from portfolio_dash.shared.enums import Market
from portfolio_dash.shared.money import cap_dp, from_db

TAIPEI = ZoneInfo("Asia/Taipei")
NEW_YORK = ZoneInfo("America/New_York")
_EX = date(2026, 6, 16)
_LAST = date(2026, 6, 15)
_SPLIT = CorporateAction(account_id="schwab", date=_EX, kind=CorporateActionKind.SPLIT,
                         from_symbol="AAA", to_symbol="AAA", ratio_to=Decimal("20"),
                         ratio_from=Decimal("1"))


def _ny(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=NEW_YORK).astimezone(TAIPEI)


def _real(symbol: str, *, after: date, through: date) -> Decimal:
    return split_factor(ActionIndex.build([_SPLIT]), symbol, after=after, through=through)


def _row(symbol: str, close: str) -> PriceRow:
    return PriceRow(instrument=symbol, market=Market.US, as_of=_LAST, close=Decimal(close),
                    source="test")


def _other(path: Path) -> sqlite3.Connection:
    """A second writer that gives up quickly instead of waiting the default 5 s."""
    conn = sqlite3.connect(path, timeout=0.05)
    conn.row_factory = sqlite3.Row
    return conn


def _consistent(path: Path) -> tuple[str, str, str]:
    """The AAA row's (close, close_raw, split_basis), asserting the stored invariant."""
    conn = sqlite3.connect(path)
    try:
        close, raw, basis = conn.execute(
            "SELECT close, close_raw, split_basis FROM prices WHERE instrument='AAA'").fetchone()
    finally:
        conn.close()
    assert from_db(close) == cap_dp(from_db(raw) * from_db(basis), 4), (close, raw, basis)
    return str(close), str(raw), str(basis)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Path]:
    """AAA's last pre-split session stored under the OLD Taipei-date window: 8000 / 20."""
    path = tmp_path / "prices.db"
    conn = connect_path(path)
    create_tables(conn)

    def old_window(symbol: str, *, after: date, through: date) -> Decimal:
        return _real(symbol, after=after, through=_EX)

    upsert_prices(conn, [_row("AAA", "400")], fetched_at=_ny(_LAST, 16, 30),
                  factor_of=old_window)
    conn.close()
    yield path


def test_a_concurrent_price_write_waits_until_the_restatement_commits(db: Path) -> None:
    conn = connect_path(db)
    refused: list[str] = []

    def racing(symbol: str, *, after: date, through: date) -> Decimal:
        # "The app": a history re-fetch after the ex-date, writing the restated close —
        # attempted between the reconcile's read and its write.
        if not refused:
            other = _other(db)
            try:
                upsert_prices(other, [_row("AAA", "20")], fetched_at=_ny(_EX, 16, 30),
                              factor_of=_real)
                refused.append("landed")
            except sqlite3.OperationalError as exc:
                refused.append(str(exc))
            finally:
                other.close()
        return _real(symbol, after=after, through=through)

    assert reconcile_prices(conn, ["AAA"], factor_of=racing) == 1
    assert refused == ["database is locked"]
    assert _consistent(db) == ("400", "400", "1")
    assert not conn.in_transaction

    # Retried once the restatement has committed, the write lands — and is consistent too.
    other = _other(db)
    upsert_prices(other, [_row("AAA", "20")], fetched_at=_ny(_EX, 16, 30), factor_of=_real)
    other.close()
    assert _consistent(db) == ("400", "20", "20")
    conn.close()


def test_nothing_to_change_still_releases_the_write_lock(db: Path) -> None:
    conn = connect_path(db)
    assert reconcile_prices(conn, ["ZZZ"], factor_of=_real) == 0
    assert not conn.in_transaction
    other = _other(db)
    upsert_prices(other, [_row("BBB", "1")], fetched_at=_ny(_LAST, 16, 30))  # no wait
    other.close()
    conn.close()


def test_an_error_mid_reconcile_rolls_back_and_releases_the_write_lock(db: Path) -> None:
    conn = connect_path(db)

    def broken(symbol: str, *, after: date, through: date) -> Decimal:
        raise RuntimeError("ledger unreadable")

    with pytest.raises(RuntimeError):
        reconcile_prices(conn, ["AAA"], factor_of=broken)
    assert not conn.in_transaction
    assert _consistent(db) == ("8000", "400", "20")
    other = _other(db)
    upsert_prices(other, [_row("BBB", "1")], fetched_at=_ny(_LAST, 16, 30))
    other.close()
    conn.close()


def test_no_symbols_takes_no_lock(db: Path) -> None:
    holder = connect_path(db)
    holder.execute("BEGIN IMMEDIATE")
    conn = sqlite3.connect(db, timeout=0.05)
    try:
        assert reconcile_prices(conn, [], factor_of=_real) == 0
    finally:
        conn.close()
        holder.rollback()
        holder.close()


def test_a_caller_that_holds_a_transaction_keeps_it(db: Path) -> None:
    """Nothing is begun inside the caller's transaction (SQLite does not nest); the restatement
    runs in it and commits as before — the caller's own write with it."""
    conn = connect_path(db)
    conn.execute("BEGIN")
    conn.execute("INSERT INTO prices (instrument, market, as_of_date, close, source, fetched_at, "
                 "close_raw) VALUES ('BBB', 'US', ?, '7', 'test', ?, '7')",
                 (_LAST.isoformat(), _ny(_LAST, 16, 30).isoformat()))
    assert reconcile_prices(conn, ["AAA"], factor_of=_real) == 1
    assert not conn.in_transaction
    reader = sqlite3.connect(db)
    got = dict(reader.execute("SELECT instrument, close FROM prices").fetchall())
    reader.close()
    assert got == {"AAA": "400", "BBB": "7"}
    conn.close()
