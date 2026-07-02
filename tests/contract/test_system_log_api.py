"""Contract: 系統操作記錄 — mutating /api calls are recorded; previews are not.

The app middleware writes through the SAME get_conn override the routes use, so
the hermetic golden DB carries the rows within one client session.
"""

from fastapi.testclient import TestClient


def _log_rows(api_client: TestClient) -> list[dict[str, object]]:
    r = api_client.get("/api/system-log", params={"limit": 100})
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["total_count"], int)
    return list(body["rows"])


def test_mutating_call_is_logged_with_zh_action(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/commit", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-11", "shares": "100", "price": "600"})
    assert r.status_code == 201
    rows = _log_rows(api_client)
    hit = next((x for x in rows if x["path"] == "/api/input/manual/commit"), None)
    assert hit is not None
    assert hit["action"] == "手動交易寫入"
    assert hit["method"] == "POST" and hit["status"] == 201
    assert isinstance(hit["duration_ms"], int)
    assert hit["username"] is None  # guest mode


def test_previews_and_reads_are_not_logged(api_client: TestClient) -> None:
    api_client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-11", "shares": "100", "price": "600"})
    api_client.get("/api/dashboard")
    rows = _log_rows(api_client)
    assert all(x["path"] != "/api/input/manual/preview" for x in rows)
    assert all(x["path"] != "/api/dashboard" for x in rows)


def test_failed_mutation_logged_with_status(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/commit", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-11", "shares": "0", "price": "600"})
    assert r.status_code == 400
    rows = _log_rows(api_client)
    hit = next((x for x in rows
                if x["path"] == "/api/input/manual/commit" and x["status"] == 400), None)
    assert hit is not None  # refusals are part of "what happened" too


def test_ledger_mutations_labelled(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/commit", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-11", "shares": "100", "price": "600"})
    txn_id = r.json()["txn_id"]
    api_client.delete(f"/api/ledgers/transactions/{txn_id}")
    rows = _log_rows(api_client)
    assert any(x["action"] == "帳本刪除" for x in rows)
