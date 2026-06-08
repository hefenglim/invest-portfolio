"""Instruments persistence helpers (instruments store)."""

import sqlite3

from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument


def upsert_instrument(conn: sqlite3.Connection, inst: Instrument) -> None:
    """Insert or update an instrument row (idempotent)."""
    conn.execute(
        """INSERT INTO instruments (symbol, market, quote_ccy, sector, name)
           VALUES (?,?,?,?,?)
           ON CONFLICT(symbol) DO UPDATE SET
               market=excluded.market, quote_ccy=excluded.quote_ccy,
               sector=excluded.sector, name=excluded.name""",
        (inst.symbol, inst.market.value, inst.quote_ccy.value, inst.sector, inst.name),
    )
    conn.commit()


def _row_to_instrument(row: sqlite3.Row) -> Instrument:
    return Instrument(
        symbol=row["symbol"],
        market=Market(row["market"]),
        quote_ccy=Currency(row["quote_ccy"]),
        sector=row["sector"],
        name=row["name"],
    )


def get_instrument(conn: sqlite3.Connection, symbol: str) -> Instrument | None:
    """Return a single instrument by exact symbol, or None if not found."""
    row = conn.execute(
        "SELECT symbol, market, quote_ccy, sector, name FROM instruments WHERE symbol=?",
        (symbol,),
    ).fetchone()
    return _row_to_instrument(row) if row is not None else None


def list_instruments(conn: sqlite3.Connection) -> list[Instrument]:
    """Return all instruments in the database."""
    rows = conn.execute(
        "SELECT symbol, market, quote_ccy, sector, name FROM instruments"
    ).fetchall()
    return [_row_to_instrument(r) for r in rows]
