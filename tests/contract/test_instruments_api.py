import pytest
from fastapi.testclient import TestClient

from portfolio_dash.api.routers import instruments as instruments_router


def test_instruments_list_shape_and_enrichment(api_client: TestClient) -> None:
    r = api_client.get("/api/instruments")
    assert r.status_code == 200
    body = r.json()
    assert "as_of" in body
    by_symbol = {i["symbol"]: i for i in body["list"]}
    tsmc = by_symbol["2330"]
    assert tsmc["name"] == "TSMC" and tsmc["market"] == "TW" and tsmc["board"] == "TWSE"
    assert tsmc["ccy"] == "TWD" and tsmc["held"] is True
    assert tsmc["last"] == "600"
    assert tsmc["target_low"] is None
    aapl = by_symbol["AAPL"]
    assert aapl["board"] == "" and aapl["held"] is True and aapl["last"] == "120"


def test_probe_returns_board(api_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(instruments_router, "probe_tw_board", lambda s, **k: "TPEx")
    r = api_client.post("/api/instruments/probe", json={"symbol": "6488"})
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "6488" and body["board"] == "TPEx"
    assert body["board_label"] == "TPEx 上櫃"


def test_probe_unresolved(api_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(instruments_router, "probe_tw_board", lambda s, **k: None)
    r = api_client.post("/api/instruments/probe", json={"symbol": "9999"})
    assert r.status_code == 200
    assert r.json()["board"] is None and r.json()["board_label"] == "未解析"


def test_probe_blank_symbol_400(api_client: TestClient) -> None:
    r = api_client.post("/api/instruments/probe", json={"symbol": "  "})
    assert r.status_code == 400
