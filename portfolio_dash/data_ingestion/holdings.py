"""Holdings computation: aggregate current shares across ALL share-bearing ledgers.

Fixed 2026-07-02: the original implementation summed only the transactions table,
so a position held as opening inventory (期初) or grown by stock/DRIP dividends
looked smaller than it is — selling it raised FALSE oversell warnings, and the
instruments/input "held" flags undercounted. Shares come from four places and all
must count: opening inventory + buys − sells + non-cash dividend shares.

2026-07-03 (R4 dividend inbox): added the dated variant ``shares_on`` — shares
held going INTO a date (events strictly earlier count), the ex-date entitlement
rule for dividend detection.
"""

import sqlite3
from datetime import date
from decimal import Decimal

from portfolio_dash.shared.models.enums import Side

_ZERO = Decimal("0")


def _shares_until(
    conn: sqlite3.Connection, account_id: str, symbol: str, before: date | None
) -> Decimal:
    """Net shares from all four share sources, counting events dated < *before*
    (or every event when *before* is None)."""
    cut = before.isoformat() if before is not None else None
    total = _ZERO
    opening_sql = "SELECT shares FROM opening_inventory WHERE account_id=? AND symbol=?"
    tx_sql = "SELECT side, quantity FROM transactions WHERE account_id=? AND symbol=?"
    div_sql = (
        "SELECT reinvest_shares FROM dividends WHERE account_id=? AND symbol=? "
        "AND type != 'CASH' AND reinvest_shares IS NOT NULL"
    )
    params: tuple[str, ...] = (account_id, symbol)
    if cut is not None:
        opening_sql += " AND build_date < ?"
        tx_sql += " AND trade_date < ?"
        div_sql += " AND date < ?"
        params = (account_id, symbol, cut)
    opening = conn.execute(opening_sql, params).fetchone()
    if opening is not None:
        total += Decimal(opening["shares"])
    for r in conn.execute(tx_sql, params):
        q = Decimal(r["quantity"])
        total += q if r["side"] == Side.BUY.value else -q
    for r in conn.execute(div_sql, params):
        total += Decimal(r["reinvest_shares"])
    return total


def current_shares(conn: sqlite3.Connection, account_id: str, symbol: str) -> Decimal:
    """Return the net shares currently held for *account_id* / *symbol*.

    opening_inventory shares + BUY − SELL + stock/DRIP ``reinvest_shares``
    (zero-cost shares, same replay rule as ``portfolio.cost_basis.build_book``).
    Returns ``Decimal("0")`` for no position.
    """
    return _shares_until(conn, account_id, symbol, None)


def shares_on(
    conn: sqlite3.Connection, account_id: str, symbol: str, *, before: date
) -> Decimal:
    """Shares held going INTO *before* — events dated strictly earlier count.

    The dividend-entitlement rule: a holder receives a distribution when the
    position exists before the ex-date (buying ON the ex-date does not qualify).
    """
    return _shares_until(conn, account_id, symbol, before)
