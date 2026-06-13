from fastapi.testclient import TestClient


def test_dashboard_money_fields_are_strings(api_client: TestClient) -> None:
    r = api_client.get("/api/dashboard")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["kpis"]["total_market_value"], str)
    assert body["kpis"]["total_market_value"] == "639600"      # 2330 600k + AAPL 1200@33
    assert body["reporting_currency"] == "TWD"
    assert body["as_of"].startswith("2026-06-11T14:30")        # frozen clock, +08:00


def test_dashboard_holdings_enriched_and_llm_quota_present(api_client: TestClient) -> None:
    body = api_client.get("/api/dashboard").json()
    by_symbol = {h["symbol"]: h for h in body["holdings"]}
    assert by_symbol["2330"]["name"] == "TSMC"
    assert by_symbol["2330"]["market_value"] == "600000"
    assert isinstance(by_symbol["2330"]["spark_30d"], list)
    assert "llm_quota" in body


def test_dashboard_freshness_and_currency_kept_uppercase(api_client: TestClient) -> None:
    body = api_client.get("/api/dashboard").json()
    assert body["currency_view"]["by_currency_value"]["USD"] == "1200"   # Currency stays UPPER
    assert body["freshness"]["missing_prices"] == []
