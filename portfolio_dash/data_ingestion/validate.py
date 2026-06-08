"""Transaction input validation: structural checks + sell-exceeds-holdings guard."""

import sqlite3
from datetime import date
from decimal import Decimal

from pydantic import BaseModel

from portfolio_dash.data_ingestion.holdings import current_shares
from portfolio_dash.shared.models.enums import Side


class TxnInput(BaseModel):
    """Validated input for a single transaction before it is persisted."""

    account_id: str
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    trade_date: date
    fee: Decimal | None = None
    tax: Decimal | None = None
    daytrade: bool = False
    is_etf: bool = False
    note: str | None = None


class Issue(BaseModel):
    """A validation finding returned by :func:`validate_transaction`."""

    kind: str
    message: str
    needs_confirm: bool = False


def validate_transaction(conn: sqlite3.Connection, inp: TxnInput) -> list[Issue]:
    """Run validation checks on *inp* against the current ledger state.

    Returns a (possibly empty) list of :class:`Issue` objects.  An empty list
    means the transaction is clean.  Issues with ``needs_confirm=True`` require
    explicit user confirmation before the transaction may be persisted (e.g.
    selling more than currently held).
    """
    issues: list[Issue] = []

    # --- account exists ---
    acc = conn.execute(
        "SELECT 1 FROM accounts WHERE account_id=?", (inp.account_id,)
    ).fetchone()
    if acc is None:
        issues.append(
            Issue(kind="unknown_account", message=f"unknown account {inp.account_id!r}")
        )

    # --- quantity and price must be positive ---
    if inp.quantity <= 0:
        issues.append(Issue(kind="non_positive_quantity", message="quantity must be > 0"))
    if inp.price <= 0:
        issues.append(Issue(kind="non_positive_price", message="price must be > 0"))

    # --- sell must not exceed current holdings ---
    if inp.side is Side.SELL and inp.quantity > 0:
        held = current_shares(conn, inp.account_id, inp.symbol)
        if inp.quantity > held:
            issues.append(
                Issue(
                    kind="sell_exceeds_holdings",
                    needs_confirm=True,
                    message=f"sell {inp.quantity} > held {held}",
                )
            )

    return issues
