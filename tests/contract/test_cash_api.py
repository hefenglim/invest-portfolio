"""Contract: 資金管理 (cash pools, R6 item 7) + negative-pool guard (item 2).

Golden subset flows affecting cash: tw_broker BUY 2330 1000×500 (−500,000 TWD),
schwab BUY AAPL 10×100 (−1,000 USD), schwab FX 32,000 TWD → 1,000 USD, tw_broker
CASH dividend net 5,000 TWD. Stored FX rates: USD/TWD 33 (latest), MYR/TWD 7.
"""

from fastapi.testclient import TestClient


def _balance(api_client: TestClient, account_id: str, ccy: str) -> str | None:
    body = api_client.get("/api/cash").json()
    row = next((b for b in body["balances"]
                if b["account_id"] == account_id and b["ccy"] == ccy), None)
    return row["amount"] if row else None


def test_balances_reflect_all_ledgers(api_client: TestClient) -> None:
    body = api_client.get("/api/cash").json()
    # schwab USD: +1000 (fx in) − 1000 (AAPL buy) = 0
    assert _balance(api_client, "schwab", "USD") == "0"
    # schwab TWD: −32000 (fx out)
    assert _balance(api_client, "schwab", "TWD") == "-32000"
    # tw_broker TWD: −500000 (buy) + 5000 (cash dividend net) = −495000
    assert _balance(api_client, "tw_broker", "TWD") == "-495000"
    assert body["reporting_currency"] == "TWD"
    assert body["reporting_total"] is not None  # rates stored for USD/TWD


def test_deposit_moves_the_pool(api_client: TestClient) -> None:
    r = api_client.post("/api/cash/movements", json={
        "account_id": "tw_broker", "date": "2026-01-01", "kind": "deposit",
        "ccy": "TWD", "amount": "600000", "note": "初始入金"})
    assert r.status_code == 201
    assert _balance(api_client, "tw_broker", "TWD") == "105000"  # 600000−500000+5000
    rows = api_client.get("/api/cash").json()["movements"]["rows"]
    assert rows[0]["kind"] == "deposit" and rows[0]["amount"] == "600000"


def test_withdraw_negative_guard_then_ack(api_client: TestClient) -> None:
    api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my_my", "date": "2026-01-01", "kind": "deposit",
        "ccy": "MYR", "amount": "1000"})
    r = api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my_my", "date": "2026-02-01", "kind": "withdraw",
        "ccy": "MYR", "amount": "1500"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "negative_cash"
    assert "-500" in r.json()["error"]["message"]
    r2 = api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my_my", "date": "2026-02-01", "kind": "withdraw",
        "ccy": "MYR", "amount": "1500", "ack_negative": True})
    assert r2.status_code == 201
    assert _balance(api_client, "moomoo_my_my", "MYR") == "-500"


def test_fx_entry_negative_guard(api_client: TestClient) -> None:
    # schwab TWD pool is −32,000 already: converting MORE TWD must warn.
    r = api_client.post("/api/cash/fx", json={
        "account_id": "schwab", "date": "2026-06-01", "from_ccy": "TWD",
        "from_amt": "10000", "to_ccy": "USD", "to_amt": "300"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "negative_cash"
    # deposit first -> conversion passes and lands in the SAME fx ledger
    api_client.post("/api/cash/movements", json={
        "account_id": "schwab", "date": "2026-05-01", "kind": "deposit",
        "ccy": "TWD", "amount": "50000"})
    r2 = api_client.post("/api/cash/fx", json={
        "account_id": "schwab", "date": "2026-06-01", "from_ccy": "TWD",
        "from_amt": "10000", "to_ccy": "USD", "to_amt": "300"})
    assert r2.status_code == 201
    fx_rows = api_client.get("/api/ledgers/fx", params={"limit": 500}).json()["rows"]
    assert any(x["from_amt"] == "10000" and x["to_amt"] == "300" for x in fx_rows)
    assert _balance(api_client, "schwab", "USD") == "300"  # 0 + 300


def test_movement_edit_delta_guard_and_delete(api_client: TestClient) -> None:
    api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my_us", "date": "2026-01-01", "kind": "deposit",
        "ccy": "USD", "amount": "1000"})
    rows = api_client.get("/api/cash").json()["movements"]["rows"]
    dep = next(x for x in rows if x["account_id"] == "moomoo_my_us")
    api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my_us", "date": "2026-02-01", "kind": "withdraw",
        "ccy": "USD", "amount": "800"})
    # shrinking the deposit below the withdraw strands the pool -> 422
    r = api_client.put(f"/api/cash/movements/{dep['id']}", json={
        "account_id": "moomoo_my_us", "date": "2026-01-01", "kind": "deposit",
        "ccy": "USD", "amount": "500"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "negative_cash"
    # deleting it outright is guarded too
    r2 = api_client.delete(f"/api/cash/movements/{dep['id']}")
    assert r2.status_code == 422
    r3 = api_client.delete(f"/api/cash/movements/{dep['id']}?ack_negative=true")
    assert r3.status_code == 200


def test_bad_inputs_400(api_client: TestClient) -> None:
    assert api_client.post("/api/cash/movements", json={
        "account_id": "tw_broker", "date": "2026-01-01", "kind": "bogus",
        "ccy": "TWD", "amount": "10"}).status_code == 400
    assert api_client.post("/api/cash/movements", json={
        "account_id": "ghost", "date": "2026-01-01", "kind": "deposit",
        "ccy": "TWD", "amount": "10"}).status_code == 400
    assert api_client.post("/api/cash/fx", json={
        "account_id": "schwab", "date": "2026-01-01", "from_ccy": "USD",
        "from_amt": "10", "to_ccy": "USD", "to_amt": "10"}).status_code == 400


def test_actions_logged(api_client: TestClient) -> None:
    api_client.post("/api/cash/movements", json={
        "account_id": "tw_broker", "date": "2026-01-01", "kind": "deposit",
        "ccy": "TWD", "amount": "100"})
    log = api_client.get("/api/system-log", params={"limit": 20}).json()["rows"]
    assert any(x["action"] == "入金／出金" for x in log)


def test_movements_pagination(api_client: TestClient) -> None:
    """WPE: /api/cash movements page via limit/offset; total_count is the whole ledger."""
    for i in range(1, 6):
        r = api_client.post("/api/cash/movements", json={
            "account_id": "tw_broker", "date": f"2026-01-0{i}", "kind": "deposit",
            "ccy": "TWD", "amount": str(1000 * i)})
        assert r.status_code == 201
    p1 = api_client.get("/api/cash", params={"limit": 2, "offset": 0}).json()["movements"]
    p2 = api_client.get("/api/cash", params={"limit": 2, "offset": 2}).json()["movements"]
    assert p1["total_count"] == 5 and p2["total_count"] == 5
    assert len(p1["rows"]) == 2 and len(p2["rows"]) == 2
    assert {r["id"] for r in p1["rows"]}.isdisjoint({r["id"] for r in p2["rows"]})
    # balances are NOT affected by the movements page window
    full = api_client.get("/api/cash", params={"limit": 2, "offset": 4}).json()
    assert len(full["movements"]["rows"]) == 1
    assert full["balances"]  # balance cards intact on any page
