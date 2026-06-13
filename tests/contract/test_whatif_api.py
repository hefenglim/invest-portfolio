"""Contract tests for POST /api/whatif (spec 03 §3.2) over the golden DB.

Money fields are Decimal strings; account_id is always echoed; oversell is a soft
warning (full numbers still returned); an unheld symbol with no account_id -> 400.
"""

from fastapi.testclient import TestClient


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
