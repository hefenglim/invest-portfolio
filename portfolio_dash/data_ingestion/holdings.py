"""Holdings computation: aggregate current shares from the transaction ledger."""

import sqlite3
from decimal import Decimal

from portfolio_dash.shared.models.enums import Side


def current_shares(conn: sqlite3.Connection, account_id: str, symbol: str) -> Decimal:
    """Return the net shares currently held for *account_id* / *symbol*.

    Sums BUY quantities and subtracts SELL quantities from the transactions table.
    Returns ``Decimal("0")`` when no transactions exist (no position).
    """
    rows = conn.execute(
        "SELECT side, quantity FROM transactions WHERE account_id=? AND symbol=?",
        (account_id, symbol),
    ).fetchall()
    total = Decimal("0")
    for r in rows:
        q = Decimal(r["quantity"])
        total += q if r["side"] == Side.BUY.value else -q
    return total
