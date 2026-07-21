"""Contract tests for POST /api/whatif (spec 03 §3.2) over the golden DB.

Money fields are Decimal strings; account_id is always echoed; oversell is a soft
warning (full numbers still returned); an unheld symbol with no account_id -> 400.
"""

import sqlite3
from decimal import Decimal

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import upsert_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument


def test_whatif_buy_existing(api_client: TestClient) -> None:
    r = api_client.post("/api/whatif", json={
        "symbol": "2330", "side": "buy", "shares": "1000", "price": "600",
        "account_id": "tw_broker"})
    assert r.status_code == 200
    body = r.json()
    assert body["amount"] == "600000"
    assert "fee" in body and "tax" in body
    assert body["new_shares"] == "2000"
    assert body["new_weight"] is not None
    assert body["account_id"] == "tw_broker"


def test_whatif_buy_old_fields_and_weight(api_client: TestClient) -> None:
    """R7 A4 (additive): the OLD triple reflects the golden dividend-adjusted basis —
    old_original_avg 500 ≠ old_adjusted_avg 495 (golden 2330 held 1,000, div 5,000). old_weight
    is present and a valid ratio, consistent with new_weight's dashboard denominator."""
    body = api_client.post("/api/whatif", json={
        "symbol": "2330", "side": "buy", "shares": "1000", "price": "600",
        "account_id": "tw_broker"}).json()
    assert Decimal(body["old_shares"]) == Decimal("1000")
    assert Decimal(body["old_original_avg"]) == Decimal("500")
    assert Decimal(body["old_adjusted_avg"]) == Decimal("495")
    assert body["old_weight"] is not None
    assert Decimal("0") < Decimal(body["old_weight"]) <= Decimal("1")


def test_whatif_sell_remaining_market_value(api_client: TestClient) -> None:
    """R7 A4: SELL reply carries remaining_market_value = remaining_shares × current price
    (600). remaining 500 × 600 = 300,000 — server-side (keeps the drawer's no-local-math rule)."""
    body = api_client.post("/api/whatif", json={
        "symbol": "2330", "side": "sell", "shares": "500", "price": "600",
        "account_id": "tw_broker"}).json()
    assert Decimal(body["remaining_market_value"]) == Decimal("300000")
    assert Decimal(body["remaining_market_value"]) == (
        Decimal(body["remaining_shares"]) * Decimal("600"))
    assert Decimal(body["old_shares"]) == Decimal("1000")
    assert body["old_weight"] is not None


def test_whatif_old_fields_null_for_unheld(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """A registered-but-unheld symbol → old_* null (nothing held) and old_weight null (no
    current price to weight)."""
    upsert_instrument(golden_db, Instrument(
        symbol="2454", market=Market.TW, quote_ccy=Currency.TWD,
        sector="Semiconductors", name="MediaTek", board="TWSE"))
    body = api_client.post("/api/whatif", json={
        "symbol": "2454", "side": "buy", "shares": "10", "price": "100",
        "account_id": "tw_broker"}).json()
    assert body["old_shares"] is None
    assert body["old_original_avg"] is None
    assert body["old_adjusted_avg"] is None
    assert body["old_weight"] is None


def test_whatif_sell_account_inferred(api_client: TestClient) -> None:
    r = api_client.post("/api/whatif", json={
        "symbol": "2330", "side": "sell", "shares": "500", "price": "600"})
    assert r.status_code == 200
    body = r.json()
    assert body["account_id"] == "tw_broker"
    assert body["oversell"] is False
    assert body["remaining_shares"] == "500"


def test_whatif_sell_oversell(api_client: TestClient) -> None:
    r = api_client.post("/api/whatif", json={
        "symbol": "2330", "side": "sell", "shares": "5000", "price": "600",
        "account_id": "tw_broker"})
    assert r.status_code == 200
    assert r.json()["oversell"] is True


def test_whatif_unheld_no_account_400(api_client: TestClient) -> None:
    r = api_client.post("/api/whatif", json={
        "symbol": "ZZZZ", "side": "buy", "shares": "1", "price": "1"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "validation_error"
