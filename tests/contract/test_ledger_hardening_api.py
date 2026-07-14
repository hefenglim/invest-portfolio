"""Contract regression tests: P3-batch3 Wave 2A ledger hardening (audit findings).

Golden subset: tw_broker holds 2330 (TW, buy 1000 @500) with a dependent 2330 CASH
dividend; schwab holds AAPL (US, buy 10 @100). Every finding's confirmed probe is a
named test here.
"""

from fastapi.testclient import TestClient


def _tx_id(api_client: TestClient, symbol: str, account_id: str = "tw_broker") -> int:
    rows = api_client.get("/api/ledgers/transactions", params={"limit": 500}).json()["rows"]
    hit = next(r for r in rows if r["symbol"] == symbol and r["account_id"] == account_id)
    return int(hit["id"])


def _tx_row(api_client: TestClient, txn_id: int) -> dict:
    rows = api_client.get("/api/ledgers/transactions", params={"limit": 500}).json()["rows"]
    return next(r for r in rows if r["id"] == txn_id)


# --- H1: account↔instrument market coherence -------------------------------


def test_market_mismatch_rejected_on_manual_commit(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/commit", json={
        "account_id": "tw_broker", "symbol": "AAPL", "side": "buy",
        "date": "2026-06-10", "shares": "1", "price": "100"})
    assert r.status_code == 400
    msg = r.json()["error"]["message"]
    assert "US" in msg and "台股" in msg


def test_edit_transaction_market_mismatch_rejected(api_client: TestClient) -> None:
    txn_id = _tx_id(api_client, "2330")
    r = api_client.put(f"/api/ledgers/transactions/{txn_id}", json={
        "account_id": "tw_broker", "symbol": "AAPL", "side": "buy",
        "date": "2026-06-10", "shares": "1", "price": "100", "fee": "0", "tax": "0"})
    assert r.status_code == 400 and "US" in r.json()["error"]["message"]


# --- H2: negative fee / tax ------------------------------------------------


def test_negative_fee_rejected_on_manual_commit(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/commit", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-10", "shares": "1", "price": "100", "fee_override": "-1"})
    assert r.status_code == 400


# --- H3: orphan-dividend correction (was a 500) ----------------------------


def test_orphan_dividend_delete_returns_422(api_client: TestClient) -> None:
    """Deleting the 2330 buy strands its dependent CASH dividend -> 422, not 500."""
    txn_id = _tx_id(api_client, "2330")
    r = api_client.delete(f"/api/ledgers/transactions/{txn_id}")
    assert r.status_code == 422 and r.json()["error"]["code"] == "orphan_correction"
    assert "2330" in r.json()["error"]["message"]
    assert _tx_row(api_client, txn_id) is not None  # refused -> row intact


def test_orphan_dividend_edit_returns_422(api_client: TestClient) -> None:
    """Moving the 2330 buy to (schwab, AAPL) is market-coherent but strands the 2330
    dividend -> 422 orphan (previously a 500 from build_book's ValueError)."""
    txn_id = _tx_id(api_client, "2330")
    r = api_client.put(f"/api/ledgers/transactions/{txn_id}", json={
        "account_id": "schwab", "symbol": "AAPL", "side": "buy",
        "date": "2026-01-05", "shares": "1000", "price": "500", "fee": "0", "tax": "0"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "orphan_correction"


# --- M4: overflow-sized input (was a 500) ----------------------------------


def test_overflow_price_400_not_500_on_manual_preview(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-10", "shares": "1", "price": "1e999"})
    assert r.status_code == 400  # bounded model, never reaches the fee quantize


def test_overflow_price_csv_row_errors_not_500(api_client: TestClient) -> None:
    csv_text = ("account,symbol,side,date,shares,price\n"
                "tw_broker,2330,buy,2026-06-10,1,1e999\n")
    r = api_client.post("/api/import/preview", json={"kind": "transactions", "csv_text": csv_text})
    assert r.status_code == 200  # degrades to a row error, not a crash
    assert r.json()["rows"][0]["status"] == "error"


# --- M6: edit recompute vs explicit override -------------------------------


def test_edit_recomputes_fee_when_core_changes(api_client: TestClient) -> None:
    """Changing shares without touching fee recomputes fee from the account's rule set."""
    txn_id = _tx_id(api_client, "2330")
    r = api_client.put(f"/api/ledgers/transactions/{txn_id}", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-01-05", "shares": "2000", "price": "500", "fee": "0", "tax": "0"})
    assert r.status_code == 200
    # tw fee = 0.001425 * (2000*500) = 1425 (min NT$20 does not bind)
    assert r.json()["fee"] == "1425"
    assert _tx_row(api_client, txn_id)["fee"] == "1425"


def test_edit_honors_explicit_fee_override(api_client: TestClient) -> None:
    txn_id = _tx_id(api_client, "2330")
    r = api_client.put(f"/api/ledgers/transactions/{txn_id}", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-01-05", "shares": "2000", "price": "500", "fee": "999", "tax": "0",
        "fee_overridden": True})
    assert r.status_code == 200 and r.json()["fee"] == "999"
    row = _tx_row(api_client, txn_id)
    assert row["fee"] == "999"
    assert (row["fee_snapshot"] or {}).get("override") == "true"


# --- M8: oversell replay scoping -------------------------------------------


def test_oversell_scoping_unrelated_delete_allowed(api_client: TestClient) -> None:
    """A pre-existing acked 2330 oversell must NOT block an unrelated AAPL delete."""
    # introduce the 2330 oversell (sell 2000 vs 1000 held), acknowledged
    sell = api_client.post("/api/input/manual/commit", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "sell",
        "date": "2026-06-10", "shares": "2000", "price": "600", "ack_oversell": True})
    assert sell.status_code == 201, sell.text
    # deleting the unrelated schwab AAPL buy no longer poisons on the 2330 oversell
    aapl_id = _tx_id(api_client, "AAPL", account_id="schwab")
    r = api_client.delete(f"/api/ledgers/transactions/{aapl_id}")
    assert r.status_code == 200, r.text
    assert api_client.get("/api/dashboard").status_code == 200  # never-500 invariant
