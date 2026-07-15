"""Contract tests for POST /api/rebalance/preview (spec 03 §3.3).

Compute-only target-weight rebalance against the golden DB. Money fields are Decimal
strings; missing-price symbols are excluded (never faked); the route never writes.

Combined cross-account engine (owner ruling 2026-07-13): a symbol held in >1 account is
ONE row over the combined position; the response adds per-row `accounts` (constituents) +
`legs` (executing trades) and summary flags `over_allocated` / `excluded_with_target`.
The wire REQUEST is unchanged: still `{"targets": {symbol: ratio_string}}`.
"""

from decimal import Decimal

from fastapi.testclient import TestClient

from tests.conftest import DashboardClientFactory, _seed_dual_account


def test_rebalance_rebate_estimate_total_present_for_tw_leg(api_client: TestClient) -> None:
    """FE-D1 forecast hint: a TW sell leg contributes Σ floor(fee × 0.77) to the summary
    (reporting-ccy Decimal string, 不計入成本)."""
    body = api_client.post("/api/rebalance/preview",
                           json={"targets": {"2330": "0.30", "AAPL": "0.70"}}).json()
    ret = body["summary"]["rebate_estimate_total"]
    assert ret is not None and Decimal(ret) > 0


def test_rebalance_rebate_estimate_total_null_without_tw_leg(api_client: TestClient) -> None:
    """A US-only rebalance produces no TW leg -> rebate_estimate_total is null (N/A)."""
    body = api_client.post("/api/rebalance/preview",
                           json={"targets": {"AAPL": "0.70"}}).json()
    assert body["summary"]["rebate_estimate_total"] is None


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


def test_rebalance_dual_account_one_row_with_accounts_and_legs(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """A dual-account symbol (AAPL in schwab + moomoo_my_us) is ONE row carrying the
    `accounts` constituents + `legs`; money stays Decimal strings; the request is unchanged."""
    client = dashboard_client_factory(_seed_dual_account)
    # Same wire shape as before: {"targets": {symbol: ratio-string}} — no new request fields.
    r = client.post("/api/rebalance/preview",
                    json={"targets": {"2330": "0.6", "AAPL": "0.4"}})
    assert r.status_code == 200
    body = r.json()

    # exactly ONE row per symbol (AAPL's two account holdings collapse into one row).
    rows = body["rows"]
    by_sym = {row["symbol"]: row for row in rows}
    assert [s for s in by_sym] == list(by_sym)  # unique keys
    assert len([row for row in rows if row["symbol"] == "AAPL"]) == 1

    aapl = by_sym["AAPL"]
    assert aapl["side"] == "buy"  # combined ~0.209 -> 0.40
    # accounts: both constituents, most-shares first, shares as STRINGS
    accts = aapl["accounts"]
    assert [a["account_id"] for a in accts] == ["schwab", "moomoo_my_us"]
    assert all(isinstance(a["shares"], str) for a in accts)
    # legs: one buy leg to the most-shares account; money as STRINGS + odd_lot bool
    legs = aapl["legs"]
    assert len(legs) == 1 and legs[0]["account_id"] == "schwab"
    leg = legs[0]
    assert isinstance(leg["shares"], str) and isinstance(leg["amount"], str)
    assert isinstance(leg["fee"], str) and isinstance(leg["tax"], str)
    assert isinstance(leg["odd_lot"], bool)
    # combined current/new weight + aggregate money all STRINGS
    for key in ("current_weight", "new_weight", "shares", "amount", "fee", "tax"):
        assert isinstance(aapl[key], str)

    summary = body["summary"]
    assert summary["over_allocated"] is False  # 0.6 + 0.4 = 1.0
    assert isinstance(summary["excluded_with_target"], list)
