from fastapi.testclient import TestClient


def test_manual_preview_buy_computes_fee_and_total(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-11", "shares": "1000", "price": "612.5"})
    assert r.status_code == 200
    b = r.json()
    assert b["fee"] == "873" and b["tax"] == "0"
    # Full source precision stays on the wire (canonical decimal_str, #2c/M1): 1000 * 612.5
    # is Decimal("612500.0") -- the trailing zero is preserved (the old _money_str
    # normalize() dropped it). The frontend quantizes for display.
    assert b["gross"] == "612500.0" and b["total"] == "-613373.0"
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


def test_manual_commit_writes(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/commit", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-11", "shares": "100", "price": "600"})
    assert r.status_code == 201
    b = r.json()
    assert isinstance(b["txn_id"], int) and b["total"].startswith("-")
    lg = api_client.get("/api/ledgers/transactions", params={"account_id": "tw_broker"}).json()
    assert lg["total_count"] == 2  # golden's 1 tw_broker txn + this one


def test_manual_commit_oversell_unacked_422(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/commit", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "sell",
        "date": "2026-06-11", "shares": "5000", "price": "600", "ack_oversell": False})
    assert r.status_code == 422 and r.json()["error"]["code"] == "oversell_unacknowledged"


def test_manual_commit_oversell_acked_writes(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/commit", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "sell",
        "date": "2026-06-11", "shares": "5000", "price": "600", "ack_oversell": True})
    assert r.status_code == 201


def test_manual_commit_hard_error_400(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/commit", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-11", "shares": "0", "price": "600"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "validation_error"


# --- unregistered symbol is a HARD block (2026-07-02) --------------------------
# An unregistered symbol has no Instrument row (no quote ccy, not in the pricing
# worklist) — committing it would poison the ledger. Preview surfaces sev "error"
# (which also disables the frontend commit button); commit is a 400.


def test_manual_preview_unregistered_symbol_is_hard_issue(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "GHOST", "side": "buy",
        "date": "2026-06-11", "shares": "100", "price": "10"})
    assert r.status_code == 200
    codes = {i["code"]: i for i in r.json()["issues"]}
    assert "symbol_unresolved" in codes
    assert codes["symbol_unresolved"]["sev"] == "error"  # hard, not confirmable


def test_manual_commit_unregistered_symbol_400(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/commit", json={
        "account_id": "tw_broker", "symbol": "GHOST", "side": "buy",
        "date": "2026-06-11", "shares": "100", "price": "10"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "validation_error"
    assert "未註冊" in r.json()["error"]["message"]
    # Nothing was written.
    lg = api_client.get("/api/ledgers/transactions", params={"account_id": "tw_broker"}).json()
    assert all(t["symbol"] != "GHOST" for t in lg["rows"])
