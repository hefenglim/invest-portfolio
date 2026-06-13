"""Contract tests for POST /api/rebalance/preview (spec 03 §3.3).

Compute-only target-weight rebalance against the golden DB. Money fields are Decimal
strings; missing-price symbols are excluded (never faked); the route never writes.
"""

from fastapi.testclient import TestClient


def test_rebalance_two_symbol_target(api_client: TestClient) -> None:
    r = api_client.post("/api/rebalance/preview",
                        json={"targets": {"2330": "0.30", "AAPL": "0.70"}})
    assert r.status_code == 200
    body = r.json()
    assert body["rows"], "expected non-empty rows for a two-symbol rebalance"
    summary = body["summary"]
    for key in ("turnover_reporting", "total_fees_reporting", "cash_after", "excluded"):
        assert key in summary
    by_sym = {row["symbol"]: row for row in body["rows"]}
    assert by_sym["2330"]["side"] == "sell"


def test_rebalance_missing_price_excluded(api_client: TestClient) -> None:
    r = api_client.post("/api/rebalance/preview", json={"targets": {"NOPRICE": "0.5"}})
    assert r.status_code == 200
    body = r.json()
    assert "NOPRICE" in body["summary"]["excluded"]
    assert body["rows"] == []


def test_rebalance_negative_ratio_400(api_client: TestClient) -> None:
    r = api_client.post("/api/rebalance/preview", json={"targets": {"2330": "-0.1"}})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "validation_error"
