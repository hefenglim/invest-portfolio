"""Contract: GET/PUT/reset /api/fee-rules (FU-D1/FU-D2) + the conn-aware end-to-end proof.

The critical assertion is the LAST one: after PUT bumps a rate, the REAL trade-preview fee
changes — proving the overlay is resolved on the money path (not silently ignored). Runs on
the full app + golden DB via the shared ``api_client`` fixture.
"""

from typing import Any

from fastapi.testclient import TestClient


def _sets(client: TestClient) -> dict[str, dict[str, Any]]:
    r = client.get("/api/fee-rules")
    assert r.status_code == 200
    body = r.json()
    return {rs["name"]: rs for rs in body["rule_sets"]}


def _field(rs: dict[str, Any], key: str) -> dict[str, Any]:
    return next(f for f in rs["fields"] if f["key"] == key)


def test_get_shape(api_client: TestClient) -> None:
    sets = _sets(api_client)
    assert set(sets) == {"tw", "schwab", "moomoo_us", "moomoo_my"}
    tw = sets["tw"]
    assert tw["market"] == "TW"
    assert tw["updated_at"] is None  # no overlay yet
    brokerage = _field(tw, "brokerage")
    assert brokerage["default"] == "0.001425"
    assert brokerage["effective"] == "0.001425"
    assert brokerage["overridden"] is False
    # Null caps serialize as JSON null (Schwab has no MY stamp).
    assert _field(sets["schwab"], "stamp_cap_stock")["default"] is None


def test_put_marks_overridden_and_updates_effective(api_client: TestClient) -> None:
    r = api_client.put("/api/fee-rules/tw", json={"overrides": {"brokerage": "0.003"}})
    assert r.status_code == 200
    tw = {f["key"]: f for f in r.json()["fields"]}
    assert tw["brokerage"]["effective"] == "0.003"
    assert tw["brokerage"]["overridden"] is True
    assert tw["brokerage"]["default"] == "0.001425"  # default preserved
    # updated_at now present.
    assert _sets(api_client)["tw"]["updated_at"] is not None


def test_put_null_reverts(api_client: TestClient) -> None:
    api_client.put("/api/fee-rules/tw", json={"overrides": {"brokerage": "0.003"}})
    r = api_client.put("/api/fee-rules/tw", json={"overrides": {"brokerage": None}})
    tw = {f["key"]: f for f in r.json()["fields"]}
    assert tw["brokerage"]["effective"] == "0.001425"
    assert tw["brokerage"]["overridden"] is False


def test_reset_one(api_client: TestClient) -> None:
    api_client.put("/api/fee-rules/tw", json={"overrides": {"brokerage": "0.003"}})
    r = api_client.post("/api/fee-rules/tw/reset")
    assert r.status_code == 200
    assert _field(r.json(), "brokerage")["overridden"] is False


def test_reset_all(api_client: TestClient) -> None:
    api_client.put("/api/fee-rules/tw", json={"overrides": {"brokerage": "0.003"}})
    api_client.put("/api/fee-rules/moomoo_my", json={"overrides": {"sst_rate": "0.06"}})
    r = api_client.post("/api/fee-rules/reset-all")
    assert r.status_code == 200
    for rs in r.json()["rule_sets"]:
        assert all(f["overridden"] is False for f in rs["fields"])


def test_validation_rejects(api_client: TestClient) -> None:
    # rate > 1
    r = api_client.put("/api/fee-rules/tw", json={"overrides": {"brokerage": "2"}})
    assert r.status_code == 400 and r.json()["error"]["field"] == "brokerage"
    # unknown field
    r = api_client.put("/api/fee-rules/tw", json={"overrides": {"bogus": "1"}})
    assert r.status_code == 400
    # bad rounding
    r = api_client.put("/api/fee-rules/tw", json={"overrides": {"rounding": "x"}})
    assert r.status_code == 400
    # unknown rule set
    r = api_client.put("/api/fee-rules/nope", json={"overrides": {"brokerage": "0.001"}})
    assert r.status_code == 404


def _preview_fee(client: TestClient, **over: str) -> str:
    body = {
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-10", "shares": "1000", "price": "500",
    }
    r = client.post("/api/input/manual/preview", json=body)
    assert r.status_code == 200
    return str(r.json()["fee"])


def test_put_changes_real_trade_preview_fee(api_client: TestClient) -> None:
    """END-TO-END conn-aware proof: bumping tw brokerage changes the computed preview fee."""
    # Default: floor(0.001425 * 500000) = floor(712.5) = 712.
    assert _preview_fee(api_client) == "712"
    api_client.put("/api/fee-rules/tw", json={"overrides": {"brokerage": "0.003"}})
    # Now: floor(0.003 * 500000) = 1500.
    assert _preview_fee(api_client) == "1500"
    api_client.post("/api/fee-rules/tw/reset")
    assert _preview_fee(api_client) == "712"


def test_rebate_forecast_honors_edited_rate(api_client: TestClient) -> None:
    """The TW rebate preview hint uses the EFFECTIVE rebate_rate (conn-aware)."""
    body = {
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-10", "shares": "1000", "price": "500",
    }
    r = api_client.post("/api/input/manual/preview", json=body)
    # Default: floor(712 * 0.77) = floor(548.24) = 548.
    assert r.json()["rebate_estimate"] == "548"
    api_client.put("/api/fee-rules/tw", json={"overrides": {"rebate_rate": "0.5"}})
    r = api_client.post("/api/input/manual/preview", json=body)
    # floor(712 * 0.5) = 356.
    assert r.json()["rebate_estimate"] == "356"
