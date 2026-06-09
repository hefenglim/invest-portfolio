import sqlite3

_DDL = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY, name TEXT NOT NULL, broker TEXT NOT NULL,
    settlement_ccy TEXT NOT NULL, funding_ccy TEXT NOT NULL,
    fee_rule_set TEXT NOT NULL, dividend_model TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS instruments (
    symbol TEXT PRIMARY KEY, market TEXT NOT NULL, quote_ccy TEXT NOT NULL,
    sector TEXT, name TEXT
);
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
    quantity TEXT NOT NULL, price TEXT NOT NULL, fees TEXT NOT NULL, tax TEXT NOT NULL,
    trade_date TEXT NOT NULL, fee_rule_snapshot TEXT, note TEXT
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
    shares TEXT NOT NULL, original_avg_cost TEXT NOT NULL, original_cost_total TEXT NOT NULL,
    build_date TEXT NOT NULL,
    PRIMARY KEY (account_id, symbol)
);
"""


def create_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    conn.commit()
