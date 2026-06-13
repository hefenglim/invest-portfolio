from fastapi.testclient import TestClient


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
