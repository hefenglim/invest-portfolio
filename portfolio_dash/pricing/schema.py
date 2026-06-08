import sqlite3

_DDL = """
CREATE TABLE IF NOT EXISTS prices (
    instrument TEXT NOT NULL, market TEXT NOT NULL, as_of_date TEXT NOT NULL,
    close TEXT NOT NULL, open TEXT, high TEXT, low TEXT, volume TEXT,
    source TEXT NOT NULL, fetched_at TEXT NOT NULL,
    PRIMARY KEY (instrument, as_of_date)
);
CREATE TABLE IF NOT EXISTS fx_rates (
    base TEXT NOT NULL, quote TEXT NOT NULL, as_of_date TEXT NOT NULL,
    rate TEXT NOT NULL, source TEXT NOT NULL, fetched_at TEXT NOT NULL,
    PRIMARY KEY (base, quote, as_of_date)
);
CREATE TABLE IF NOT EXISTS dividend_events (
    instrument TEXT NOT NULL, market TEXT NOT NULL, ex_date TEXT NOT NULL,
    pay_date TEXT, cash_amount TEXT, stock_amount TEXT, currency TEXT,
    source TEXT NOT NULL, fetched_at TEXT NOT NULL,
    PRIMARY KEY (instrument, ex_date)
);
"""


def create_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    conn.commit()
