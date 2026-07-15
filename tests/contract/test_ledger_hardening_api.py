"""Contract regression tests: P3-batch3 Wave 2A ledger hardening (audit findings).

Golden subset: tw_broker holds 2330 (TW, buy 1000 @500) with a dependent 2330 CASH
dividend; schwab holds AAPL (US, buy 10 @100). Every finding's confirmed probe is a
named test here.
"""

import sqlite3
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import (
    insert_transaction,
    list_transactions,
    upsert_instrument,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side


def _tx_id(api_client: TestClient, symbol: str, account_id: str = "tw_broker") -> int:
    rows = api_client.get("/api/ledgers/transactions", params={"limit": 500}).json()["rows"]
    hit = next(r for r in rows if r["symbol"] == symbol and r["account_id"] == account_id)
    return int(hit["id"])


def _tx_id_side(api_client: TestClient, symbol: str, side: str) -> int:
    rows = api_client.get("/api/ledgers/transactions", params={"limit": 500}).json()["rows"]
    return int(next(r for r in rows if r["symbol"] == symbol and r["side"] == side)["id"])


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


# --- H2 (edit path): negative fee is a Pydantic 422, row unchanged ----------


def test_edit_negative_fee_rejected_4xx(api_client: TestClient) -> None:
    """LOW-4b: PUT with fee=-1 trips the `fee: ge=0` model constraint. The app's error
    handler normalizes the Pydantic request-validation 422 to a 400 validation_error, so
    assert a client error (4xx) and that the row is unchanged."""
    txn_id = _tx_id(api_client, "2330")
    before = _tx_row(api_client, txn_id)["fee"]
    r = api_client.put(f"/api/ledgers/transactions/{txn_id}", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-01-05", "shares": "1000", "price": "500", "fee": "-1", "tax": "0"})
    assert 400 <= r.status_code < 500
    assert _tx_row(api_client, txn_id)["fee"] == before  # untouched


# --- MED-1: daytrade flag persists + governs the recompute tax rate ---------


def test_edit_recompute_preserves_daytrade_tax_rate(
    golden_db: sqlite3.Connection, api_client: TestClient
) -> None:
    """A stored TW day-trade SELL, edited (shares) WITHOUT a fee/tax override, recomputes
    tax at the day-trade rate (0.15%) — not the 現股 0.3% — and the flag survives."""
    insert_transaction(
        golden_db, account_id="tw_broker", symbol="2330", side=Side.SELL,
        quantity=Decimal("100"), price=Decimal("600"), fees=Decimal("85"),
        tax=Decimal("90"), trade_date=date(2026, 6, 10), daytrade=True,
        fee_rule_snapshot={"brokerage": "0.001425", "min_fee": "20", "tax_rate": "0.0015"})
    txn_id = _tx_id_side(api_client, "2330", "sell")
    r = api_client.put(f"/api/ledgers/transactions/{txn_id}", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "sell",
        "date": "2026-06-10", "shares": "200", "price": "600", "fee": "0", "tax": "0"})
    assert r.status_code == 200, r.text
    assert r.json()["tax"] == "180"  # 0.0015 * (200*600); NOT 0.003 * 120000 = 360
    assert (_tx_row(api_client, txn_id)["fee_snapshot"] or {}).get("tax_rate") == "0.0015"
    stored = {t.id: t for t in list_transactions(golden_db)}[txn_id]
    assert stored.daytrade is True  # flag survived the recompute (not on the wire)


def test_edit_recompute_normal_sell_uses_normal_tax(
    golden_db: sqlite3.Connection, api_client: TestClient
) -> None:
    """A normal (non-daytrade) SELL edit recomputes at the 現股 0.3% rate."""
    insert_transaction(
        golden_db, account_id="tw_broker", symbol="2330", side=Side.SELL,
        quantity=Decimal("100"), price=Decimal("600"), fees=Decimal("85"),
        tax=Decimal("180"), trade_date=date(2026, 6, 11), daytrade=False)
    txn_id = _tx_id_side(api_client, "2330", "sell")
    r = api_client.put(f"/api/ledgers/transactions/{txn_id}", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "sell",
        "date": "2026-06-11", "shares": "200", "price": "600", "fee": "0", "tax": "0"})
    assert r.status_code == 200, r.text
    assert r.json()["tax"] == "360"  # 0.003 * (200*600)
    assert (_tx_row(api_client, txn_id)["fee_snapshot"] or {}).get("tax_rate") == "0.003"


# --- LOW-3: H1 coherence guard scoped to re-keys (legacy row editable in place) ---


def test_legacy_incoherent_row_editable_in_place(
    golden_db: sqlite3.Connection, api_client: TestClient
) -> None:
    """A legacy incoherent row (US AAPL booked in the TWD tw_broker account, written
    directly via the store) stays editable IN PLACE — a shares/amount fix must not be
    blocked by H1 — while changing its symbol to another incoherent combo still 400s."""
    insert_transaction(
        golden_db, account_id="tw_broker", symbol="AAPL", side=Side.BUY,
        quantity=Decimal("10"), price=Decimal("100"), fees=Decimal("0"),
        tax=Decimal("0"), trade_date=date(2026, 1, 15))
    upsert_instrument(golden_db, Instrument(
        symbol="MSFT", market=Market.US, quote_ccy=Currency.USD,
        sector="Tech", name="Microsoft"))
    txn_id = _tx_id(api_client, "AAPL", account_id="tw_broker")
    # in-place edit (same account+symbol): coherence skipped -> succeeds
    ok = api_client.put(f"/api/ledgers/transactions/{txn_id}", json={
        "account_id": "tw_broker", "symbol": "AAPL", "side": "buy",
        "date": "2026-01-15", "shares": "20", "price": "100", "fee": "0", "tax": "0"})
    assert ok.status_code == 200, ok.text
    assert _tx_row(api_client, txn_id)["shares"] == "20"
    # re-keying the symbol to another US instrument in the TW account still 400s (H1)
    rekey = api_client.put(f"/api/ledgers/transactions/{txn_id}", json={
        "account_id": "tw_broker", "symbol": "MSFT", "side": "buy",
        "date": "2026-01-15", "shares": "20", "price": "100", "fee": "0", "tax": "0"})
    assert rekey.status_code == 400 and "US" in rekey.json()["error"]["message"]
