"""Instruments and transactions persistence helpers (data ingestion store)."""

import json
import sqlite3
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field

from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.money import from_db, to_db


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


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


class StoredTransaction(BaseModel):
    """Pydantic model for a persisted transaction row."""

    id: int
    account_id: str
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    fees: Decimal
    tax: Decimal
    trade_date: date
    fee_rule_snapshot: dict[str, str] = Field(default_factory=dict)
    note: str | None = None


def insert_transaction(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    symbol: str,
    side: Side,
    quantity: Decimal,
    price: Decimal,
    fees: Decimal,
    tax: Decimal,
    trade_date: date,
    fee_rule_snapshot: dict[str, str] | None = None,
    note: str | None = None,
) -> int:
    """Insert a transaction row and return its new primary-key id."""
    cur = conn.execute(
        """INSERT INTO transactions (account_id, symbol, side, quantity, price, fees, tax,
               trade_date, fee_rule_snapshot, note)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            account_id,
            symbol,
            side.value,
            to_db(quantity),
            to_db(price),
            to_db(fees),
            to_db(tax),
            trade_date.isoformat(),
            json.dumps(fee_rule_snapshot or {}),
            note,
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def list_transactions(
    conn: sqlite3.Connection,
    *,
    account_id: str | None = None,
    symbol: str | None = None,
) -> list[StoredTransaction]:
    """Return transactions ordered by trade_date ASC, id ASC.

    Optionally filter by *account_id* and/or *symbol* (AND logic when both given).
    """
    clauses: list[str] = []
    params: list[str] = []
    if account_id is not None:
        clauses.append("account_id=?")
        params.append(account_id)
    if symbol is not None:
        clauses.append("symbol=?")
        params.append(symbol)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT id, account_id, symbol, side, quantity, price, fees, tax, trade_date, "
        f"fee_rule_snapshot, note FROM transactions{where} ORDER BY trade_date ASC, id ASC",
        params,
    ).fetchall()
    return [
        StoredTransaction(
            id=r["id"],
            account_id=r["account_id"],
            symbol=r["symbol"],
            side=Side(r["side"]),
            quantity=from_db(r["quantity"]),
            price=from_db(r["price"]),
            fees=from_db(r["fees"]),
            tax=from_db(r["tax"]),
            trade_date=date.fromisoformat(r["trade_date"]),
            fee_rule_snapshot=json.loads(r["fee_rule_snapshot"] or "{}"),
            note=r["note"],
        )
        for r in rows
    ]
