import sqlite3

from portfolio_dash.data_ingestion.schema import create_tables


def test_create_tables_idempotent() -> None:
    c = sqlite3.connect(":memory:")
    create_tables(c)
    create_tables(c)  # second call must not error
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "accounts", "instruments", "transactions", "dividends",
        "fx_conversions", "opening_inventory",
    }.issubset(names)


def test_instruments_has_board_column() -> None:
    c = sqlite3.connect(":memory:")
    create_tables(c)
    cols = {r[1] for r in c.execute("PRAGMA table_info(instruments)")}
    assert "board" in cols


def test_board_migration_idempotent_on_legacy_table() -> None:
    c = sqlite3.connect(":memory:")
    c.execute(
        "CREATE TABLE instruments (symbol TEXT PRIMARY KEY, market TEXT NOT NULL, "
        "quote_ccy TEXT NOT NULL, sector TEXT, name TEXT)"
    )  # legacy schema, no board column
    create_tables(c)  # must ALTER-add board
    create_tables(c)  # must be idempotent (no error)
    cols = {r[1] for r in c.execute("PRAGMA table_info(instruments)")}
    assert "board" in cols
