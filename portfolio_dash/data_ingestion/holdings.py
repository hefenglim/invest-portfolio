"""Holdings computation: aggregate current shares across ALL share-bearing ledgers.

Fixed 2026-07-02: the original implementation summed only the transactions table,
so a position held as opening inventory (期初) or grown by stock/DRIP dividends
looked smaller than it is — selling it raised FALSE oversell warnings, and the
instruments/input "held" flags undercounted. Shares come from four places and all
must count: opening inventory + buys − sells + non-cash dividend shares.
"""

import sqlite3
from decimal import Decimal

from portfolio_dash.shared.models.enums import Side

_ZERO = Decimal("0")


def current_shares(conn: sqlite3.Connection, account_id: str, symbol: str) -> Decimal:
    """Return the net shares currently held for *account_id* / *symbol*.

    opening_inventory shares + BUY − SELL + stock/DRIP ``reinvest_shares``
    (zero-cost shares, same replay rule as ``portfolio.cost_basis.build_book``).
    Returns ``Decimal("0")`` for no position.
    """
    total = _ZERO
    opening = conn.execute(
        "SELECT shares FROM opening_inventory WHERE account_id=? AND symbol=?",
        (account_id, symbol),
    ).fetchone()
    if opening is not None:
        total += Decimal(opening["shares"])
    for r in conn.execute(
        "SELECT side, quantity FROM transactions WHERE account_id=? AND symbol=?",
        (account_id, symbol),
    ):
        q = Decimal(r["quantity"])
        total += q if r["side"] == Side.BUY.value else -q
    for r in conn.execute(
        "SELECT reinvest_shares FROM dividends "
        "WHERE account_id=? AND symbol=? AND type != 'CASH' "
        "AND reinvest_shares IS NOT NULL",
        (account_id, symbol),
    ):
        total += Decimal(r["reinvest_shares"])
    return total
