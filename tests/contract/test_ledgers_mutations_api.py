"""Contract tests: ledger row corrections — PUT/DELETE /api/ledgers/* (2026-07-02).

Corrections are explicit user actions; the backend replays the WOULD-BE ledger
through build_book first, so an edit/delete that strands a later sell answers
422 "oversell" until explicitly acked. The golden subset holds (tw_broker, 2330):
opening 1000 sh; schwab AAPL etc. — tests write their own rows to mutate.
"""

from fastapi.testclient import TestClient


def _commit_tx(api_client: TestClient, **over: object) -> int:
    body: dict[str, object] = {
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-11", "shares": "100", "price": "600",
    }
    body.update(over)
    r = api_client.post("/api/input/manual/commit", json=body)
    assert r.status_code == 201, r.text
    txn_id = r.json()["txn_id"]
    assert isinstance(txn_id, int)
    return txn_id


def _tx_row(api_client: TestClient, txn_id: int) -> dict[str, object] | None:
    rows = api_client.get("/api/ledgers/transactions", params={"limit": 500}).json()["rows"]
    for row in rows:
        if row["id"] == txn_id:
            return dict(row)
    return None


# --- transactions ---------------------------------------------------------


def test_edit_transaction_updates_values(api_client: TestClient) -> None:
    txn_id = _commit_tx(api_client)
    r = api_client.put(f"/api/ledgers/transactions/{txn_id}", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-12", "shares": "200", "price": "605",
        "fee": "172", "tax": "0", "note": "corrected"})
    assert r.status_code == 200 and r.json()["ok"] is True
    row = _tx_row(api_client, txn_id)
    assert row is not None
    assert row["shares"] == "200" and row["price"] == "605"
    assert row["date"] == "2026-06-12" and row["note"] == "corrected"


def test_edit_transaction_rejects_unregistered_symbol(api_client: TestClient) -> None:
    txn_id = _commit_tx(api_client)
    r = api_client.put(f"/api/ledgers/transactions/{txn_id}", json={
        "account_id": "tw_broker", "symbol": "GHOST", "side": "buy",
        "date": "2026-06-12", "shares": "100", "price": "600",
        "fee": "0", "tax": "0"})
    assert r.status_code == 400
    assert "未註冊" in r.json()["error"]["message"]


def test_edit_transaction_oversell_guard_then_ack(api_client: TestClient) -> None:
    """Editing a BUY into a tiny lot strands the later SELL -> 422 until acked."""
    buy_id = _commit_tx(api_client, shares="1000", price="600")
    _commit_tx(api_client, side="sell", shares="1500", price="610")  # covered: 1000+1000 held
    body = {
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-11", "shares": "10", "price": "600",
        "fee": "20", "tax": "0", "ack_oversell": False}
    r = api_client.put(f"/api/ledgers/transactions/{buy_id}", json=body)
    assert r.status_code == 422 and r.json()["error"]["code"] == "oversell"
    # unchanged on refusal
    row = _tx_row(api_client, buy_id)
    assert row is not None and row["shares"] == "1000"
    # explicit ack writes (dashboard degrades to the flagged 賣超 state)
    body["ack_oversell"] = True
    r2 = api_client.put(f"/api/ledgers/transactions/{buy_id}", json=body)
    assert r2.status_code == 200
    dash = api_client.get("/api/dashboard")
    assert dash.status_code == 200  # never-500 invariant


def test_delete_transaction_removes_row(api_client: TestClient) -> None:
    txn_id = _commit_tx(api_client)
    r = api_client.delete(f"/api/ledgers/transactions/{txn_id}")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert _tx_row(api_client, txn_id) is None


def test_delete_transaction_oversell_guard_then_ack(api_client: TestClient) -> None:
    buy_id = _commit_tx(api_client, shares="1000", price="600")
    _commit_tx(api_client, side="sell", shares="1500", price="610")
    r = api_client.delete(f"/api/ledgers/transactions/{buy_id}")
    assert r.status_code == 422 and r.json()["error"]["code"] == "oversell"
    assert _tx_row(api_client, buy_id) is not None  # refused -> still there
    r2 = api_client.delete(f"/api/ledgers/transactions/{buy_id}?ack_oversell=true")
    assert r2.status_code == 200
    assert _tx_row(api_client, buy_id) is None
    assert api_client.get("/api/dashboard").status_code == 200


def test_edit_transaction_404(api_client: TestClient) -> None:
    r = api_client.put("/api/ledgers/transactions/99999", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-12", "shares": "100", "price": "600", "fee": "0", "tax": "0"})
    assert r.status_code == 404


def test_delete_transaction_404(api_client: TestClient) -> None:
    assert api_client.delete("/api/ledgers/transactions/99999").status_code == 404


# --- dividends ------------------------------------------------------------


def _first_dividend_id(api_client: TestClient) -> tuple[int, dict[str, object]]:
    rows = api_client.get("/api/ledgers/dividends", params={"limit": 500}).json()["rows"]
    assert rows, "golden subset must hold at least one dividend"
    return rows[0]["id"], dict(rows[0])


def test_edit_dividend_updates_values(api_client: TestClient) -> None:
    div_id, row = _first_dividend_id(api_client)
    r = api_client.put(f"/api/ledgers/dividends/{div_id}", json={
        "account_id": row["account_id"], "symbol": row["symbol"],
        "date": row["date"], "type": row["type"],
        "gross": "999", "withhold": "0", "net": "999"})
    assert r.status_code == 200
    rows = api_client.get("/api/ledgers/dividends", params={"limit": 500}).json()["rows"]
    edited = next(x for x in rows if x["id"] == div_id)
    assert edited["gross"] == "999" and edited["net"] == "999"


def test_delete_dividend_removes_row(api_client: TestClient) -> None:
    div_id, _ = _first_dividend_id(api_client)
    r = api_client.delete(f"/api/ledgers/dividends/{div_id}")
    assert r.status_code == 200
    rows = api_client.get("/api/ledgers/dividends", params={"limit": 500}).json()["rows"]
    assert all(x["id"] != div_id for x in rows)
    assert api_client.get("/api/dashboard").status_code == 200


def test_edit_dividend_bad_type_400(api_client: TestClient) -> None:
    div_id, row = _first_dividend_id(api_client)
    r = api_client.put(f"/api/ledgers/dividends/{div_id}", json={
        "account_id": row["account_id"], "symbol": row["symbol"],
        "date": row["date"], "type": "bogus",
        "gross": "1", "withhold": "0", "net": "1"})
    assert r.status_code == 400


# --- fx conversions ---------------------------------------------------------


def _first_fx(api_client: TestClient) -> tuple[int, dict[str, object]]:
    rows = api_client.get("/api/ledgers/fx", params={"limit": 500}).json()["rows"]
    assert rows, "golden subset must hold at least one fx conversion"
    return rows[0]["id"], dict(rows[0])


def test_edit_fx_updates_values(api_client: TestClient) -> None:
    fx_id, row = _first_fx(api_client)
    r = api_client.put(f"/api/ledgers/fx/{fx_id}", json={
        "account_id": row["account_id"], "date": row["date"],
        "from_ccy": row["from_ccy"], "from_amt": "123456",
        "to_ccy": row["to_ccy"], "to_amt": "4000"})
    assert r.status_code == 200
    rows = api_client.get("/api/ledgers/fx", params={"limit": 500}).json()["rows"]
    edited = next(x for x in rows if x["id"] == fx_id)
    assert edited["from_amt"] == "123456" and edited["to_amt"] == "4000"


def test_edit_fx_same_ccy_400(api_client: TestClient) -> None:
    fx_id, row = _first_fx(api_client)
    r = api_client.put(f"/api/ledgers/fx/{fx_id}", json={
        "account_id": row["account_id"], "date": row["date"],
        "from_ccy": "USD", "from_amt": "100", "to_ccy": "USD", "to_amt": "100"})
    assert r.status_code == 400


def test_delete_fx_removes_row(api_client: TestClient) -> None:
    fx_id, _ = _first_fx(api_client)
    assert api_client.delete(f"/api/ledgers/fx/{fx_id}").status_code == 200
    rows = api_client.get("/api/ledgers/fx", params={"limit": 500}).json()["rows"]
    assert all(x["id"] != fx_id for x in rows)


# --- openings ---------------------------------------------------------------
# The golden subset has NO opening rows: tests seed one through the real CSV
# import path first (the only opening write seam), then correct it.


def _seed_opening(api_client: TestClient) -> None:
    csv_text = ("account,symbol,shares,original_avg_cost,build_date\n"
                "tw_broker,2330,500,450,2026-01-02\n")
    r = api_client.post("/api/import/commit",
                        json={"kind": "openings", "csv_text": csv_text,
                              "ack_warnings": True})
    assert r.status_code == 200, r.text


def test_edit_opening_updates_values(api_client: TestClient) -> None:
    _seed_opening(api_client)
    r = api_client.put("/api/ledgers/openings/tw_broker/2330",
                       json={"shares": "2000", "avg": "500", "date": "2026-01-05"})
    assert r.status_code == 200
    rows = api_client.get("/api/ledgers/openings", params={"limit": 500}).json()["rows"]
    edited = next(x for x in rows
                  if x["account_id"] == "tw_broker" and x["symbol"] == "2330")
    assert edited["shares"] == "2000" and edited["avg"] == "500"
    assert edited["total"] == "1000000"  # avg * shares, computed by the backend
    assert api_client.get("/api/dashboard").status_code == 200


def test_delete_opening_removes_row(api_client: TestClient) -> None:
    _seed_opening(api_client)
    r = api_client.delete("/api/ledgers/openings/tw_broker/2330")
    assert r.status_code == 200
    rows = api_client.get("/api/ledgers/openings", params={"limit": 500}).json()["rows"]
    assert all(not (x["account_id"] == "tw_broker" and x["symbol"] == "2330") for x in rows)
    assert api_client.get("/api/dashboard").status_code == 200


def test_delete_opening_oversell_guard_then_ack(api_client: TestClient) -> None:
    """A sell covered ONLY by the opening: deleting it strands the sell -> 422."""
    _seed_opening(api_client)
    _commit_tx(api_client, side="sell", shares="1200", price="610")  # 1000 buy + 500 open
    r = api_client.delete("/api/ledgers/openings/tw_broker/2330")
    assert r.status_code == 422 and r.json()["error"]["code"] == "oversell"
    r2 = api_client.delete("/api/ledgers/openings/tw_broker/2330?ack_oversell=true")
    assert r2.status_code == 200
    assert api_client.get("/api/dashboard").status_code == 200


def test_opening_404(api_client: TestClient) -> None:
    assert api_client.delete("/api/ledgers/openings/tw_broker/NOPE").status_code == 404
