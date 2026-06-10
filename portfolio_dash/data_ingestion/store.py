"""Instruments and transactions persistence helpers (data ingestion store)."""

import json
import sqlite3
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field

from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Account, Instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.money import from_db, to_db


def upsert_instrument(conn: sqlite3.Connection, inst: Instrument) -> None:
    """Insert or update an instrument row (idempotent)."""
    conn.execute(
        """INSERT INTO instruments (symbol, market, quote_ccy, sector, name, board)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(symbol) DO UPDATE SET
               market=excluded.market, quote_ccy=excluded.quote_ccy,
               sector=excluded.sector, name=excluded.name, board=excluded.board""",
        (
            inst.symbol, inst.market.value, inst.quote_ccy.value,
            inst.sector, inst.name, inst.board,
        ),
    )
    conn.commit()


def _row_to_instrument(row: sqlite3.Row) -> Instrument:
    return Instrument(
        symbol=row["symbol"],
        market=Market(row["market"]),
        quote_ccy=Currency(row["quote_ccy"]),
        sector=row["sector"],
        name=row["name"],
        board=row["board"] or "",
    )


def get_instrument(conn: sqlite3.Connection, symbol: str) -> Instrument | None:
    """Return a single instrument by exact symbol, or None if not found."""
    row = conn.execute(
        "SELECT symbol, market, quote_ccy, sector, name, board FROM instruments WHERE symbol=?",
        (symbol,),
    ).fetchone()
    return _row_to_instrument(row) if row is not None else None


def list_instruments(conn: sqlite3.Connection) -> list[Instrument]:
    """Return all instruments in the database."""
    rows = conn.execute(
        "SELECT symbol, market, quote_ccy, sector, name, board FROM instruments"
    ).fetchall()
    return [_row_to_instrument(r) for r in rows]


def list_accounts(conn: sqlite3.Connection) -> list[Account]:
    """Return all broker accounts (seeded by ``config_seed.seed_accounts``)."""
    rows = conn.execute(
        "SELECT account_id, name, broker, settlement_ccy, funding_ccy "
        "FROM accounts ORDER BY account_id"
    ).fetchall()
    return [
        Account(
            account_id=r["account_id"], name=r["name"], broker=r["broker"],
            settlement_ccy=Currency(r["settlement_ccy"]),
            funding_ccy=Currency(r["funding_ccy"]),
        )
        for r in rows
    ]


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


# ---------------------------------------------------------------------------
# Opening inventory
# ---------------------------------------------------------------------------


class StoredOpening(BaseModel):
    """Pydantic model for a persisted opening_inventory row."""

    account_id: str
    symbol: str
    shares: Decimal
    original_avg_cost: Decimal
    original_cost_total: Decimal
    build_date: date


def upsert_opening(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    symbol: str,
    shares: Decimal,
    original_avg_cost: Decimal,
    original_cost_total: Decimal,
    build_date: date,
) -> None:
    """Insert or update an opening_inventory row (idempotent on PK account_id+symbol)."""
    conn.execute(
        """INSERT INTO opening_inventory
               (account_id, symbol, shares, original_avg_cost, original_cost_total, build_date)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(account_id, symbol) DO UPDATE SET
               shares=excluded.shares,
               original_avg_cost=excluded.original_avg_cost,
               original_cost_total=excluded.original_cost_total,
               build_date=excluded.build_date""",
        (
            account_id,
            symbol,
            to_db(shares),
            to_db(original_avg_cost),
            to_db(original_cost_total),
            build_date.isoformat(),
        ),
    )
    conn.commit()


def list_opening(
    conn: sqlite3.Connection,
    *,
    account_id: str | None = None,
) -> list[StoredOpening]:
    """Return opening_inventory rows ordered by account_id, symbol.

    Optionally filter by *account_id*.
    """
    where: str
    params: list[str]
    if account_id is not None:
        where = " WHERE account_id=?"
        params = [account_id]
    else:
        where = ""
        params = []
    rows = conn.execute(
        f"SELECT account_id, symbol, shares, original_avg_cost, original_cost_total, "
        f"build_date FROM opening_inventory{where} ORDER BY account_id, symbol",
        params,
    ).fetchall()
    return [
        StoredOpening(
            account_id=r["account_id"],
            symbol=r["symbol"],
            shares=from_db(r["shares"]),
            original_avg_cost=from_db(r["original_avg_cost"]),
            original_cost_total=from_db(r["original_cost_total"]),
            build_date=date.fromisoformat(r["build_date"]),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Dividends
# ---------------------------------------------------------------------------


class StoredDividend(BaseModel):
    """Pydantic model for a persisted dividends row."""

    id: int
    account_id: str
    symbol: str
    date: date
    type: str
    gross: Decimal
    withholding: Decimal
    net: Decimal
    reinvest_shares: Decimal | None = None
    reinvest_price: Decimal | None = None


def insert_dividend(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    symbol: str,
    div_date: date,
    div_type: str,
    gross: Decimal,
    withholding: Decimal,
    net: Decimal,
    reinvest_shares: Decimal | None = None,
    reinvest_price: Decimal | None = None,
) -> int:
    """Insert a dividends row and return its new primary-key id."""
    cur = conn.execute(
        """INSERT INTO dividends
               (account_id, symbol, date, type, gross, withholding, net,
                reinvest_shares, reinvest_price)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            account_id,
            symbol,
            div_date.isoformat(),
            div_type,
            to_db(gross),
            to_db(withholding),
            to_db(net),
            to_db(reinvest_shares) if reinvest_shares is not None else None,
            to_db(reinvest_price) if reinvest_price is not None else None,
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def list_dividends(
    conn: sqlite3.Connection,
    *,
    account_id: str | None = None,
    symbol: str | None = None,
) -> list[StoredDividend]:
    """Return dividends rows ordered by date ASC, id ASC.

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
        f"SELECT id, account_id, symbol, date, type, gross, withholding, net, "
        f"reinvest_shares, reinvest_price FROM dividends{where} ORDER BY date ASC, id ASC",
        params,
    ).fetchall()
    return [
        StoredDividend(
            id=r["id"],
            account_id=r["account_id"],
            symbol=r["symbol"],
            date=date.fromisoformat(r["date"]),
            type=r["type"],
            gross=from_db(r["gross"]),
            withholding=from_db(r["withholding"]),
            net=from_db(r["net"]),
            reinvest_shares=(
                from_db(r["reinvest_shares"]) if r["reinvest_shares"] is not None else None
            ),
            reinvest_price=(
                from_db(r["reinvest_price"]) if r["reinvest_price"] is not None else None
            ),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# FX conversions
# ---------------------------------------------------------------------------


class StoredFxConversion(BaseModel):
    """Pydantic model for a persisted fx_conversions row."""

    id: int
    account_id: str
    date: date
    from_ccy: Currency
    from_amount: Decimal
    to_ccy: Currency
    to_amount: Decimal

    @property
    def implied_rate(self) -> Decimal:
        """Home-currency units per one foreign-currency unit (from_amount / to_amount)."""
        return self.from_amount / self.to_amount


def insert_fx_conversion(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    date: date,
    from_ccy: Currency,
    from_amount: Decimal,
    to_ccy: Currency,
    to_amount: Decimal,
) -> int:
    """Insert an fx_conversions row and return its new primary-key id."""
    cur = conn.execute(
        """INSERT INTO fx_conversions (account_id, date, from_ccy, from_amount, to_ccy,
               to_amount) VALUES (?,?,?,?,?,?)""",
        (
            account_id,
            date.isoformat(),
            from_ccy.value,
            to_db(from_amount),
            to_ccy.value,
            to_db(to_amount),
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def list_fx_conversions(
    conn: sqlite3.Connection,
    *,
    account_id: str | None = None,
) -> list[StoredFxConversion]:
    """Return fx_conversions rows ordered by date ASC, id ASC.

    Optionally filter by *account_id*.
    """
    where = ""
    params: list[str] = []
    if account_id is not None:
        where = " WHERE account_id=?"
        params = [account_id]
    rows = conn.execute(
        f"SELECT id, account_id, date, from_ccy, from_amount, to_ccy, to_amount "
        f"FROM fx_conversions{where} ORDER BY date ASC, id ASC",
        params,
    ).fetchall()
    return [
        StoredFxConversion(
            id=r["id"],
            account_id=r["account_id"],
            date=date.fromisoformat(r["date"]),
            from_ccy=Currency(r["from_ccy"]),
            from_amount=from_db(r["from_amount"]),
            to_ccy=Currency(r["to_ccy"]),
            to_amount=from_db(r["to_amount"]),
        )
        for r in rows
    ]


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
