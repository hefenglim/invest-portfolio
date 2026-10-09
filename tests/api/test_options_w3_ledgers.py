"""Options W3 — the ledger doors and the cash statement wire (spec §4.2, §4.3, §6.2, §10).

``GET /api/ledgers/transactions`` rows carry ``close_reason`` / ``contracts`` / ``option_side``
/ ``refused_close`` (null / false on a stock row); the edit door works on a close row and
protects a stored expiry; the cash statement lines carry the same ``option_side`` the ledger
tab shows; the corporate-action doors accept ``OPTION_ADJUST`` with ``strike_products`` /
``conserves`` and register the new contract only on save.

No case here replays an ``OPTION_ADJUST`` or relies on a §4.4-refused close (W2's; crossed at
the wave-1 merge) — ``refused_close`` is only checked to be false.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.register import register_option_contract
from portfolio_dash.data_ingestion.store import (
    get_instrument,
    insert_transaction,
    list_corporate_actions,
    list_transactions,
)
from portfolio_dash.shared.models.enums import Side

PUT = "XYZ261218P00050000"
ADJ = "XYZ261218P00025000"     # 50 × 1 = 25 × 2
STRANDED = "此合約已登錄到期作廢，請先修改或刪除該列"


def _seed(conn: sqlite3.Connection) -> tuple[int, int]:
    register_option_contract(conn, PUT)
    sto = insert_transaction(conn, account_id="schwab", symbol=PUT, side=Side.SELL,
                             quantity=Decimal("200"), price=Decimal("1.5"),
                             fees=Decimal("1.31"), tax=Decimal("0"),
                             trade_date=date(2026, 5, 4), short_sale=True)
    exp = insert_transaction(conn, account_id="schwab", symbol=PUT, side=Side.BUY,
                             quantity=Decimal("200"), price=Decimal("0"), fees=Decimal("0"),
                             tax=Decimal("0"), trade_date=date(2026, 12, 18),
                             fee_rule_snapshot={"source": "close_reason"},
                             close_reason="expired")
    return sto, exp


def _rows(client: TestClient, **params: Any) -> dict[int, dict[str, Any]]:
    r = client.get("/api/ledgers/transactions", params=params)
    assert r.status_code == 200, r.text
    return {row["id"]: row for row in r.json()["rows"]}


# --- the transactions wire ---------------------------------------------------------------------


def test_a_stock_ledger_row_carries_the_new_keys_as_null(api_client: TestClient) -> None:
    for row in _rows(api_client).values():
        assert (row["close_reason"], row["contracts"], row["option_side"],
                row["refused_close"]) == (None, None, None, False)


def test_option_rows_carry_their_side_and_contracts(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    sto, exp = _seed(golden_db)
    rows = _rows(api_client)
    assert (rows[sto]["option_side"], rows[sto]["contracts"], rows[sto]["close_reason"]) == (
        "賣出開倉", "2", None)
    assert (rows[exp]["option_side"], rows[exp]["contracts"], rows[exp]["close_reason"]) == (
        "到期作廢", "2", "expired")
    assert rows[exp]["refused_close"] is False
    # A filtered page labels exactly as the whole ledger does (one walk over every row).
    only_close = _rows(api_client, frm="2026-12-01")
    assert only_close[exp]["option_side"] == "到期作廢"


# --- the cash statement wire -------------------------------------------------------------------


def test_cash_statement_lines_carry_the_same_side(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    sto, exp = _seed(golden_db)
    ledger = _rows(api_client)
    r = api_client.get("/api/cash/statement", params={"account": "schwab", "ccy": "USD"})
    assert r.status_code == 200, r.text
    lines = r.json()["rows"]
    options = [ln for ln in lines if ln["symbol"] == PUT]
    assert sorted((ln["option_side"], ln["contracts"]) for ln in options) == sorted(
        (ledger[i]["option_side"], ledger[i]["contracts"]) for i in (sto, exp))
    stock = [ln for ln in lines if ln["symbol"] == "AAPL"]
    assert stock and all(ln["option_side"] is None and ln["contracts"] is None for ln in stock)


# --- the edit door on a close row -------------------------------------------------------------


def _edit_body(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"account_id": "schwab", "symbol": PUT, "side": "buy",
                            "date": "2026-12-18", "shares": "200", "price": "0", "fee": "0",
                            "tax": "0", "note": "edited"}
    body.update(over)
    return body


def test_a_close_row_is_editable_at_price_zero(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    _sto, exp = _seed(golden_db)
    r = api_client.put(f"/api/ledgers/transactions/{exp}", json=_edit_body())
    assert r.status_code == 200, r.text
    (row,) = [t for t in list_transactions(golden_db, symbol=PUT) if t.id == exp]
    assert (row.close_reason, row.price, row.note) == ("expired", Decimal("0"), "edited")


def test_a_close_row_edit_refuses_money_and_a_new_reason(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    _sto, exp = _seed(golden_db)
    priced = api_client.put(f"/api/ledgers/transactions/{exp}",
                            json=_edit_body(price="0.05"))
    assert priced.status_code == 400
    feed = api_client.put(f"/api/ledgers/transactions/{exp}",
                          json=_edit_body(fee="0.65", fee_overridden=True))
    assert feed.status_code == 400
    assert feed.json()["error"]["message"] == "到期作廢／被指派／履約列不收手續費與稅"
    moved = api_client.put(f"/api/ledgers/transactions/{exp}",
                           json=_edit_body(close_reason="assigned"))
    assert moved.status_code == 400
    assert "不能在更正時變更" in moved.json()["error"]["message"]


def test_an_option_row_is_edited_in_contracts(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    register_option_contract(golden_db, PUT)
    sto = insert_transaction(golden_db, account_id="schwab", symbol=PUT, side=Side.SELL,
                             quantity=Decimal("200"), price=Decimal("1.5"),
                             fees=Decimal("1.31"), tax=Decimal("0"),
                             trade_date=date(2026, 5, 4), short_sale=True)
    body = {"account_id": "schwab", "symbol": PUT, "side": "sell", "date": "2026-05-04",
            "contracts": "3", "price": "1.5", "fee": "1.31", "tax": "0"}
    r = api_client.put(f"/api/ledgers/transactions/{sto}", json=body)
    assert r.status_code == 200, r.text
    (row,) = list_transactions(golden_db, symbol=PUT)
    # 3 contracts = 300 share-equivalents; the fee is recomputed by the option branch
    # (3 × 0.65 + SEC on 450 → 1.95 + 0.01).
    assert (row.quantity, row.fees) == (Decimal("300"), Decimal("1.96"))


def test_contracts_on_a_stock_edit_are_refused(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    aapl = next(t for t in list_transactions(golden_db) if t.symbol == "AAPL")
    r = api_client.put(f"/api/ledgers/transactions/{aapl.id}", json={
        "account_id": "schwab", "symbol": "AAPL", "side": "buy", "date": "2026-01-10",
        "contracts": "1", "price": "100", "fee": "0", "tax": "0"})
    assert r.status_code == 400
    assert r.json()["error"]["message"] == "口數只適用於選擇權合約"


def test_an_edit_cannot_strand_a_stored_expiry(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    sto, _exp = _seed(golden_db)
    r = api_client.put(f"/api/ledgers/transactions/{sto}", json={
        "account_id": "schwab", "symbol": PUT, "side": "sell", "date": "2026-05-04",
        "contracts": "3", "price": "1.5", "fee": "1.31", "tax": "0",
        "fee_overridden": True, "tax_overridden": True})
    assert r.status_code == 400, r.text
    assert r.json()["error"]["message"] == STRANDED


# --- OPTION_ADJUST through the corporate-action doors ------------------------------------------


def _adjust(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"account_id": "schwab", "date": "2026-06-01",
                            "kind": "選擇權合約調整", "from_symbol": PUT, "to_symbol": ADJ,
                            "ratio_to": "2", "ratio_from": "1"}
    body.update(over)
    return body


def _short_only(conn: sqlite3.Connection) -> None:
    register_option_contract(conn, PUT)
    insert_transaction(conn, account_id="schwab", symbol=PUT, side=Side.SELL,
                       quantity=Decimal("100"), price=Decimal("1.5"), fees=Decimal("0.66"),
                       tax=Decimal("0"), trade_date=date(2026, 5, 4), short_sale=True)


def test_the_adjust_preview_prints_the_strike_products_and_writes_nothing(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    _short_only(golden_db)
    r = api_client.post("/api/ledgers/corporate-actions/preview", json=_adjust())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "OPTION_ADJUST" and body["kind_label"] == "選擇權合約調整"
    assert body["strike_products"] == {
        "from_strike": "50", "ratio_from": "1", "from_product": "50",
        "to_strike": "25", "ratio_to": "2", "to_product": "50"}
    assert body["conserves"] is True
    assert get_instrument(golden_db, ADJ) is None


def test_an_equity_preview_carries_null_strike_products(api_client: TestClient) -> None:
    r = api_client.post("/api/ledgers/corporate-actions/preview", json={
        "account_id": "schwab", "date": "2026-02-01", "kind": "SPLIT", "from_symbol": "AAPL",
        "to_symbol": "AAPL", "ratio_to": "2", "ratio_from": "1"})
    assert r.status_code == 200, r.text
    assert (r.json()["strike_products"], r.json()["conserves"]) == (None, None)


def test_the_adjust_save_registers_the_new_contract(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    _short_only(golden_db)
    r = api_client.post("/api/ledgers/corporate-actions",
                        json={**_adjust(), "ack_warnings": True})
    assert r.status_code == 201, r.text
    inst = get_instrument(golden_db, ADJ)
    assert inst is not None and inst.is_option
    (row,) = [a for a in list_corporate_actions(golden_db) if a.kind == "OPTION_ADJUST"]
    assert (row.from_symbol, row.to_symbol, row.ratio_to) == (PUT, ADJ, Decimal("2"))
    listed = api_client.get("/api/ledgers/corporate-actions").json()["rows"]
    (wire,) = [r for r in listed if r["kind"] == "OPTION_ADJUST"]
    assert (wire["kind_label"], wire["ratio_label"]) == ("選擇權合約調整", "每 1 口 → 2 口")


def test_a_nonstandard_adjust_is_refused_and_registers_nothing(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    _short_only(golden_db)
    r = api_client.post("/api/ledgers/corporate-actions",
                        json=_adjust(to_symbol="XYZ261218P00016667", ratio_to="3"))
    assert r.status_code == 400, r.text
    assert "選擇權合約調整" in r.json()["error"]["message"]
    assert get_instrument(golden_db, "XYZ261218P00016667") is None
