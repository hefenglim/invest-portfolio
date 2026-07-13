"""Contract tests for POST /api/export/symbol-detail (dividend-history reconciliation CSV)."""

from fastapi.testclient import TestClient

_HEADER = "date,type,gross,withholding,net,reinvest_shares,reinvest_price,ccy"


def test_export_symbol_detail_known_symbol(api_client: TestClient) -> None:
    r = api_client.post("/api/export/symbol-detail", json={"symbol": "2330"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "2330_dividends.csv" in r.headers["content-disposition"]
    assert r.content[:3] == b"\xef\xbb\xbf"
    text = r.content[3:].decode("utf-8")
    assert text.split("\r\n", 1)[0] == _HEADER
    # golden 2330 cash dividend at source precision.
    assert "2026-03-01,cash,5000,0,5000,,,TWD" in text


def test_export_symbol_detail_unknown_symbol_400(api_client: TestClient) -> None:
    r = api_client.post("/api/export/symbol-detail", json={"symbol": "ZZZZ"})
    assert r.status_code == 400
    assert r.json()["error"]["field"] == "symbol"


def test_export_symbol_detail_missing_symbol_400(api_client: TestClient) -> None:
    # symbol is required — the app's RequestValidationError handler answers 400 field=symbol.
    r = api_client.post("/api/export/symbol-detail", json={})
    assert r.status_code == 400
    body = r.json()["error"]
    assert body["code"] == "validation_error" and body["field"] == "symbol"
