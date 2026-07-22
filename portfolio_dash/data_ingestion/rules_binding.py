"""Resolvers over the (account, market) rule binding table (``account_market_rules``).

Batch B, Wave 1 — the INERT foundation. Today fee/dividend rules bind to the ACCOUNT
(``accounts.fee_rule_set`` / ``accounts.dividend_model`` scalars). The later merged
dual-market Moomoo account needs rules bound to (account, market), so ``seed_accounts``
mirrors each account's current scalars into ONE binding row for its single market and these
resolvers read that table.

Every resolver FALLS BACK to the accounts-table scalars when a market has no binding row,
so existing single-market accounts behave identically (zero behavior change). NOTHING
consumes these resolvers yet — later tasks wire them in.

Pure DB reads. An unknown ``account_id`` raises :class:`KeyError` (loud), mirroring the
repo's dict-style account lookups.
"""

import sqlite3

from portfolio_dash.data_ingestion.markets import market_for_settlement_ccy
from portfolio_dash.shared.enums import Market


def _account_row(conn: sqlite3.Connection, account_id: str) -> sqlite3.Row:
    """The accounts row for *account_id*; raise ``KeyError`` if it does not exist.

    Every resolver goes through here first, so an unknown account fails loudly and
    uniformly (the accounts table is the authority on which accounts exist).
    """
    row: sqlite3.Row | None = conn.execute(
        "SELECT settlement_ccy, fee_rule_set, dividend_model "
        "FROM accounts WHERE account_id = ?",
        (account_id,),
    ).fetchone()
    if row is None:
        raise KeyError(account_id)
    return row


def fee_rule_for(conn: sqlite3.Connection, account_id: str, market: Market) -> str:
    """Fee rule set bound to (*account_id*, *market*); else ``accounts.fee_rule_set``."""
    acct = _account_row(conn, account_id)  # KeyError if unknown account
    row = conn.execute(
        "SELECT fee_rule_set FROM account_market_rules "
        "WHERE account_id = ? AND market = ?",
        (account_id, market.value),
    ).fetchone()
    return str(row["fee_rule_set"]) if row is not None else str(acct["fee_rule_set"])


def dividend_model_for(conn: sqlite3.Connection, account_id: str, market: Market) -> str:
    """Dividend model bound to (*account_id*, *market*); else ``accounts.dividend_model``."""
    acct = _account_row(conn, account_id)  # KeyError if unknown account
    row = conn.execute(
        "SELECT dividend_model FROM account_market_rules "
        "WHERE account_id = ? AND market = ?",
        (account_id, market.value),
    ).fetchone()
    return str(row["dividend_model"]) if row is not None else str(acct["dividend_model"])


def allowed_markets(conn: sqlite3.Connection, account_id: str) -> frozenset[Market]:
    """Markets bound for *account_id* in the binding table.

    Empty (no binding rows) -> the singleton derived from the account's settlement ccy,
    so a single-market account with no bindings still reports its one market.
    """
    acct = _account_row(conn, account_id)  # KeyError if unknown account
    rows = conn.execute(
        "SELECT market FROM account_market_rules WHERE account_id = ?",
        (account_id,),
    ).fetchall()
    if rows:
        return frozenset(Market(r["market"]) for r in rows)
    market = market_for_settlement_ccy(acct["settlement_ccy"])
    if market is None:  # unreachable for a valid account (3 mapped ccys); loud if not
        raise ValueError(
            f"account {account_id!r} has unmapped settlement_ccy "
            f"{acct['settlement_ccy']!r}"
        )
    return frozenset({market})


def rule_sets_for(conn: sqlite3.Connection, account_id: str) -> list[str]:
    """Distinct fee rule sets bound for *account_id*, in stable (alphabetical) order.

    Empty (no binding rows) -> ``[accounts.fee_rule_set]``.
    """
    acct = _account_row(conn, account_id)  # KeyError if unknown account
    rows = conn.execute(
        "SELECT DISTINCT fee_rule_set FROM account_market_rules "
        "WHERE account_id = ? ORDER BY fee_rule_set",
        (account_id,),
    ).fetchall()
    if rows:
        return [str(r["fee_rule_set"]) for r in rows]
    return [str(acct["fee_rule_set"])]


__all__ = [
    "fee_rule_for",
    "dividend_model_for",
    "allowed_markets",
    "rule_sets_for",
]
