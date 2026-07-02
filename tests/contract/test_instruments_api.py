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


def test_register_new_instrument(api_client: TestClient) -> None:
    r = api_client.post("/api/instruments", json={
        "symbol": "6488", "market": "TW", "name": "環球晶", "sector": "Semis",
        "board": "TPEx", "quote_ccy": "TWD", "target_low": "450"})
    assert r.status_code == 201
    body = r.json()
    assert body["symbol"] == "6488" and body["board"] == "TPEx" and body["target_low"] == "450"


def test_register_triggers_initial_quote_fetch(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Registration immediately fetches the symbol's quote (2026-07-02) so the user
    is not price-less until the market's next post-close cron."""
    calls: list[str] = []

    def _record(conn: object, *, symbol: str, market: object, board: object,
                now: object) -> str:
        calls.append(symbol)
        return "1 ok"

    monkeypatch.setattr(instruments_router, "refresh_instrument_quote", _record)
    r = api_client.post("/api/instruments", json={
        "symbol": "TSLA", "market": "US", "name": "Tesla"})
    assert r.status_code == 201
    assert calls == ["TSLA"]


def test_register_survives_quote_fetch_failure(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The initial fetch is best-effort: a provider failure NEVER fails registration."""
    def _boom(conn: object, **kw: object) -> str:
        raise RuntimeError("provider down")

    monkeypatch.setattr(instruments_router, "refresh_instrument_quote", _boom)
    r = api_client.post("/api/instruments", json={
        "symbol": "TSLA", "market": "US", "name": "Tesla"})
    assert r.status_code == 201
    assert r.json()["symbol"] == "TSLA" and r.json()["last"] is None


def test_register_duplicate_409(api_client: TestClient) -> None:
    r = api_client.post("/api/instruments", json={"symbol": "2330", "market": "TW",
                                                  "name": "x", "sector": "y", "board": "TWSE"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "duplicate_symbol"


def test_register_us_with_tw_board_400(api_client: TestClient) -> None:
    r = api_client.post("/api/instruments", json={"symbol": "TSLA", "market": "US",
                                                  "name": "Tesla", "board": "TWSE"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "validation_error"


def test_put_updates_target_low(api_client: TestClient) -> None:
    r = api_client.put("/api/instruments/2330", json={"target_low": "550"})
    assert r.status_code == 200 and r.json()["target_low"] == "550"


def test_put_missing_404(api_client: TestClient) -> None:
    r = api_client.put("/api/instruments/NOPE", json={"sector": "z"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
