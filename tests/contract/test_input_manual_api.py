from fastapi.testclient import TestClient


def test_manual_preview_buy_computes_fee_and_total(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-11", "shares": "1000", "price": "612.5"})
    assert r.status_code == 200
    b = r.json()
    assert b["fee"] == "873" and b["tax"] == "0"
    assert b["gross"] == "612500" and b["total"] == "-613373"
    assert b["fee_overridden"] is False and b["issues"] == []


def test_manual_preview_oversell_soft_issue(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "sell",
        "date": "2026-06-11", "shares": "5000", "price": "600"})
    b = r.json()
    codes = {i["code"]: i for i in b["issues"]}
    assert "sell_exceeds_holdings" in codes
    assert codes["sell_exceeds_holdings"]["sev"] == "warn"
    assert codes["sell_exceeds_holdings"]["field"] == "shares"


def test_manual_preview_fee_override(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-11", "shares": "1000", "price": "612.5",
        "fee_override": "500"})
    b = r.json()
    assert b["fee"] == "500" and b["fee_overridden"] is True
