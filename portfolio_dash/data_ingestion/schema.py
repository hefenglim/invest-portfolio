import sqlite3

_DDL = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY, name TEXT NOT NULL, broker TEXT NOT NULL,
    settlement_ccy TEXT NOT NULL, funding_ccy TEXT NOT NULL,
    fee_rule_set TEXT NOT NULL, dividend_model TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS instruments (
    symbol TEXT PRIMARY KEY, market TEXT NOT NULL, quote_ccy TEXT NOT NULL,
    sector TEXT, name TEXT, board TEXT,
    target_low TEXT, board_status TEXT NOT NULL DEFAULT 'resolved',
    is_etf INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    target_high TEXT,
    industry TEXT
);
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
    quantity TEXT NOT NULL, price TEXT NOT NULL, fees TEXT NOT NULL, tax TEXT NOT NULL,
    trade_date TEXT NOT NULL, fee_rule_snapshot TEXT, note TEXT,
    daytrade INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS dividends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL, symbol TEXT NOT NULL, date TEXT NOT NULL, type TEXT NOT NULL,
    gross TEXT, withholding TEXT, net TEXT, reinvest_shares TEXT, reinvest_price TEXT
);
CREATE TABLE IF NOT EXISTS fx_conversions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL, date TEXT NOT NULL,
    from_ccy TEXT NOT NULL, from_amount TEXT NOT NULL,
    to_ccy TEXT NOT NULL, to_amount TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS opening_inventory (
    account_id TEXT NOT NULL, symbol TEXT NOT NULL,
    shares TEXT NOT NULL, original_cost_total TEXT NOT NULL,
    build_date TEXT NOT NULL,
    PRIMARY KEY (account_id, symbol)
);
CREATE TABLE IF NOT EXISTS cash_movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL, date TEXT NOT NULL,
    kind TEXT NOT NULL,
    ccy TEXT NOT NULL, amount TEXT NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS ledger_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name TEXT NOT NULL,
    row_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('update','delete')),
    before_json TEXT NOT NULL,
    at TEXT NOT NULL
);
"""


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, decl: str
) -> None:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _drop_column_if_present(
    conn: sqlite3.Connection, table: str, column: str
) -> None:
    """Idempotent DROP COLUMN migration (SQLite >= 3.35; bundled 3.49). The repo's FIRST
    destructive migration (A6, 2026-07-21). Guarded by ``pragma table_info`` so it is a no-op
    once the column is gone; the column must be non-PK and unindexed (opening_inventory's
    ``original_avg_cost`` is both). Fresh DBs never have the column (the DDL omits it), so this
    only fires for a legacy DB migrating in."""
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column in cols:
        conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")


def create_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    _add_column_if_missing(conn, "instruments", "board", "TEXT")  # migrate legacy DBs
    _add_column_if_missing(conn, "instruments", "target_low", "TEXT")
    _add_column_if_missing(conn, "instruments", "board_status", "TEXT NOT NULL DEFAULT 'resolved'")
    _add_column_if_missing(conn, "instruments", "is_etf", "INTEGER NOT NULL DEFAULT 0")
    # archived (FU-D13): a closed-with-history symbol the user stopped tracking. Excluded
    # from quote/signal/news fetch scopes but stays REGISTERED, so no money figure changes.
    _add_column_if_missing(conn, "instruments", "archived", "INTEGER NOT NULL DEFAULT 0")
    # target_high (FU-D28): the price-alert CEILING, joining target_low (the floor). Both feed
    # the target_cross rule; additive, so an existing DB migrates in without touching any row.
    _add_column_if_missing(conn, "instruments", "target_high", "TEXT")
    # industry (R6, 2026-07-19): nullable GICS industry, filled by the next wave's AI service.
    # Backend plumbing only this wave; additive, so an existing DB migrates in untouched.
    _add_column_if_missing(conn, "instruments", "industry", "TEXT")
    _add_column_if_missing(conn, "transactions", "daytrade", "INTEGER NOT NULL DEFAULT 0")
    # original_avg_cost drop (A6, 2026-07-21): the stored rounded average is retired — cost
    # basis / XIRR key off original_cost_total only, and the average is computed on read. A
    # legacy DB carried a NOT NULL original_avg_cost column that upsert_opening no longer fills,
    # so it MUST be dropped (else a new insert would violate the NOT NULL constraint).
    _drop_column_if_present(conn, "opening_inventory", "original_avg_cost")
    conn.commit()
    # One-time idempotent sector rewrite to the canonical GICS vocabulary (R6). Runs after the
    # column adds + commit so it sees the final schema; a no-op when every value is already
    # canonical. Local import keeps schema.py free of a module-load dependency on store.py.
    from portfolio_dash.data_ingestion.store import migrate_instrument_sectors

    migrate_instrument_sectors(conn)
