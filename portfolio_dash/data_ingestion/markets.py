"""Account market inference from settlement currency (single source of truth).

The account's settlement currency uniquely determines the market a bare symbol
entered under it belongs to (TW broker → TWD/TW, Schwab & Moomoo-US → USD/US,
Moomoo-MY → MYR/MY). This is the basis for BOTH auto-registering unknown symbols
from trade input AND the account↔instrument coherence guard (audit H1): a symbol's
own market must match the account's market, or the ledger row is incoherent
(a US stock booked in a TWD/TW account cannot settle there).

Previously the map + lookup lived privately in ``api/routers/input_center.py``; it is
hoisted here so the data_ingestion validation layer and the api layer share ONE map.
"""

import sqlite3

from portfolio_dash.shared.enums import Currency, Market

# Settlement currency → market. One entry per supported currency.
CCY_MARKET: dict[str, Market] = {
    Currency.TWD.value: Market.TW,
    Currency.USD.value: Market.US,
    Currency.MYR.value: Market.MY,
}

# Market → zh-TW label (names the account side in coherence messages).
MARKET_ZH: dict[Market, str] = {
    Market.TW: "台股",
    Market.US: "美股",
    Market.MY: "馬股",
}


def market_for_settlement_ccy(settlement_ccy: str) -> Market | None:
    """Market implied by an account's settlement currency, or None if unrecognized."""
    return CCY_MARKET.get(settlement_ccy)


def account_market(conn: sqlite3.Connection, account_id: str) -> Market | None:
    """Market of *account_id*, inferred from its settlement currency (None if unknown)."""
    row = conn.execute(
        "SELECT settlement_ccy FROM accounts WHERE account_id=?", (account_id,)
    ).fetchone()
    return CCY_MARKET.get(row["settlement_ccy"]) if row is not None else None
