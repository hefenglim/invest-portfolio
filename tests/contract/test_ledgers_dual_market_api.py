"""Batch B contract: ledger-edit coherence guard relaxed to allowed-market SETS, and the
transaction-edit fee recompute bound to the (account, market) rule set.

A merged Moomoo account (US + MY bindings) must let a ledger edit RE-KEY a row to either
bound market, reject an unbound one (TW), and — on a market-changing re-key — recompute the
fee from the market's OWN rule set (the T3 fee-site swap: ``fee_rule_for(conn, acct, mkt)``),
not the account-level scalar.
"""

import sqlite3
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.config_seed import get_fee_rule_set, seed_accounts
from portfolio_dash.data_ingestion.fees import compute_fees
from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.wire import decimal_str
from tests.conftest import DashboardClientFactory

_MERGED = "moomoo_my"  # the merged dual-market account (US + MY bindings seeded, Batch B)


def _seed_merged(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)  # seeds moomoo_my with BOTH the US and MY market bindings
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US,
                                       quote_ccy=Currency.USD, sector="Tech", name="Apple"))
    upsert_instrument(conn, Instrument(symbol="1155", market=Market.MY,
                                       quote_ccy=Currency.MYR, sector="Financials",
                                       name="Maybank"))
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW,
                                       quote_ccy=Currency.TWD, sector="Tech", name="TSMC"))
    # Two starting rows in the merged account, one per bound market, to re-key in the tests.
    insert_transaction(conn, account_id=_MERGED, symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("100"), price=Decimal("10"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 10))
    insert_transaction(conn, account_id=_MERGED, symbol="1155", side=Side.BUY,
                       quantity=Decimal("100"), price=Decimal("10"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 11))


def _tx_id(client: TestClient, symbol: str) -> int:
    rows = client.get("/api/ledgers/transactions", params={"limit": 500}).json()["rows"]
    return int(next(r for r in rows if r["symbol"] == symbol
                    and r["account_id"] == _MERGED)["id"])


def _edit_body(symbol: str) -> dict[str, object]:
    return {"account_id": _MERGED, "symbol": symbol, "side": "buy",
            "date": "2026-01-10", "shares": "100", "price": "10", "fee": "0", "tax": "0"}


# --- accepts a re-key into EITHER bound market -----------------------------


def test_edit_rekey_to_us_instrument_accepted(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed_merged)
    txn_id = _tx_id(client, "1155")  # start MY, re-key -> US (both bound)
    r = client.put(f"/api/ledgers/transactions/{txn_id}", json=_edit_body("AAPL"))
    assert r.status_code == 200, r.text


def test_edit_rekey_to_my_instrument_accepted_and_fee_market_bound(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """Re-key US -> MY is accepted, AND the recomputed fee/tax come from the MY rule set
    (``moomoo_my``) — the market-bound fee-site swap — not the account scalar (``moomoo_us``)."""
    client = dashboard_client_factory(_seed_merged)
    txn_id = _tx_id(client, "AAPL")  # start US, re-key -> MY (both bound)
    r = client.put(f"/api/ledgers/transactions/{txn_id}", json=_edit_body("1155"))
    assert r.status_code == 200, r.text

    # Independent oracle: the MY rule set is what a market-bound lookup must select.
    # Pure defaults (no conn -> no user overlay) match the seeded DB, which has no overrides.
    expect_my = compute_fees(get_fee_rule_set("moomoo_my"), Side.BUY, Decimal("100"), Decimal("10"))
    expect_us = compute_fees(get_fee_rule_set("moomoo_us"), Side.BUY, Decimal("100"), Decimal("10"))
    assert r.json()["fee"] == decimal_str(expect_my.fee)
    assert r.json()["tax"] == decimal_str(expect_my.tax)
    # Guard the assertion's teeth: MY and US rule sets must differ here, else the test is vacuous.
    assert expect_my.fee != expect_us.fee


# --- still rejects a re-key into an UNbound market -------------------------


def test_edit_rekey_to_tw_instrument_rejected(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed_merged)
    txn_id = _tx_id(client, "AAPL")  # re-key US -> TW (TW not bound)
    r = client.put(f"/api/ledgers/transactions/{txn_id}", json=_edit_body("2330"))
    assert r.status_code == 400
    assert "屬 TW 市場" in r.json()["error"]["message"]
