"""Options final review R2 — the doors (spec §4.2, §4.3, §8).

Each case reproduces one reviewer finding through the public door it was found on:

* M1 — an OCC symbol typed in lower case is the SAME contract on every door: the transactions
  CSV runs the position-aware option rules on it, and the corporate-action CSV and form store
  the canonical spelling (a lower-case destination used to be stored verbatim and never apply).
* M2 — a statement that starts after an expiry the ledger already holds re-derives that
  expiry (the contract's known position is measured the day before its expiry) and the CSV
  door refuses the overlap in zh, instead of a 422 ``option_expiry_no_opening``.
* M3 — an ``OPTION_ADJUST`` that would strand a stored close row of its source contract is
  refused by the one validator, so the form and the CSV door (the broker path's) both refuse it.
* M4 — the edit dialog's preview of a stored close row derives the side WITHOUT the row it
  replaces.
* m1 — the manual door requires an option intent or a close reason on an option row.
* m2 — an advisory never gates the corporate-action form.
* nits — no option fee label on a close row; a fractional 口數 reads 「口數須為正整數」.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.holdings import shares_through
from portfolio_dash.data_ingestion.register import register_option_contract
from portfolio_dash.data_ingestion.store import (
    get_instrument,
    insert_transaction,
    list_corporate_actions,
    list_transactions,
)
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.options import CloseReason

PUT = "XYZ261218P00050000"
C60 = "XYZ261218C00060000"
C20 = "XYZ261218C00020000"     # 60 × 1 = 20 × 3
STRANDED = "此合約已登錄到期作廢，請先修改或刪除該列"
_TXN_HEADER = "account,symbol,side,date,shares,price,fee,tax\n"
_CA_HEADER = "account,date,kind,from_symbol,to_symbol,ratio_to,ratio_from,cost_carry,note\n"
_BROKER_HEADER = (
    '"Date","Action","Symbol","Description","Quantity","Price","Fees & Comm","Amount"\n')


def _trade(conn: sqlite3.Connection, symbol: str, side: Side, qty: str, price: str,
           on: date, *, fee: str = "0", short: bool = False,
           close_reason: CloseReason | None = None) -> int:
    return insert_transaction(
        conn, account_id="schwab", symbol=symbol, side=side, quantity=Decimal(qty),
        price=Decimal(price), fees=Decimal(fee), tax=Decimal("0"), trade_date=on,
        short_sale=short, close_reason=close_reason,
        fee_rule_snapshot={"source": "close_reason"} if close_reason else None)


def _short_put_with_expiry(conn: sqlite3.Connection) -> int:
    """A sold put and its stored expiry; returns the expiry row's id."""
    register_option_contract(conn, PUT)
    _trade(conn, PUT, Side.SELL, "200", "1.5", date(2026, 5, 4), fee="1.31", short=True)
    exp = _trade(conn, PUT, Side.BUY, "200", "0", date(2026, 12, 18), close_reason="expired")
    conn.commit()
    return exp


def _short_call(conn: sqlite3.Connection) -> None:
    register_option_contract(conn, C60)
    _trade(conn, C60, Side.SELL, "200", "2", date(2026, 5, 4), fee="1.31", short=True)
    conn.commit()


def _adjust_body(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"account_id": "schwab", "date": "2026-05-10", "kind": "OPTION_ADJUST",
                            "from_symbol": C60, "to_symbol": C20, "ratio_to": "3",
                            "ratio_from": "1"}
    body.update(over)
    return body


def _preview_rows(client: TestClient, kind: str, csv_text: str) -> list[dict[str, Any]]:
    r = client.post("/api/import/preview", json={"kind": kind, "csv_text": csv_text})
    assert r.status_code == 200, r.text
    rows: list[dict[str, Any]] = r.json()["rows"]
    return rows


def _codes(issues: list[dict[str, Any]], sev: str | None = None) -> list[str]:
    return [i["code"] for i in issues if sev is None or i["sev"] == sev]


# --- M1: a lower-case contract is the same contract on every door ----------------------------


def test_a_lower_case_contract_in_the_transactions_csv_meets_the_option_rules(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """The stored expiry is protected whatever case the file spells the contract in."""
    _short_put_with_expiry(golden_db)
    for symbol in (PUT, PUT.lower(), f" {PUT.lower()} "):
        (row,) = _preview_rows(api_client, "transactions",
                               f"{_TXN_HEADER}schwab,{symbol},buy,2026-06-01,100,0.40,0.65,0\n")
        assert "option_expired_row_stranded" in row["kinds"], (symbol, row)
        assert row["status"] == "error"
        assert row["data"]["symbol"] == PUT


def test_a_lower_case_destination_in_the_corporate_action_csv_is_stored_canonical(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    _short_call(golden_db)
    csv_text = f"{_CA_HEADER}schwab,2026-05-10,OPTION_ADJUST,{C60.lower()},{C20.lower()},3,1,,\n"
    r = api_client.post("/api/import/commit",
                        json={"kind": "corporate_actions", "csv_text": csv_text})
    assert r.status_code == 200, r.text
    assert r.json()["written"] == 1
    assert [(a.from_symbol, a.to_symbol) for a in list_corporate_actions(golden_db)] == [
        (C60, C20)]
    assert get_instrument(golden_db, C20) is not None
    # The adjustment APPLIES: the sold 2 contracts are now 6 of the adjusted one.
    assert shares_through(golden_db, "schwab", C20, on=date(2026, 5, 11)) == Decimal("-600")


def test_the_corporate_action_form_canonicalises_both_contracts(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    _short_call(golden_db)
    body = _adjust_body(from_symbol=C60.lower(), to_symbol=f" {C20.lower()} ")
    r = api_client.post("/api/ledgers/corporate-actions/preview", json=body)
    assert r.status_code == 200, r.text
    assert not _codes(r.json()["issues"], "error"), r.json()["issues"]
    r = api_client.post("/api/ledgers/corporate-actions", json=body)
    assert r.status_code == 201, r.text
    assert [(a.from_symbol, a.to_symbol) for a in list_corporate_actions(golden_db)] == [
        (C60, C20)]
    assert shares_through(golden_db, "schwab", C20, on=date(2026, 5, 11)) == Decimal("-600")


# --- M2: an already-booked expiry does not block a later statement --------------------------


def test_a_statement_after_a_booked_expiry_re_derives_it_for_the_csv_door_to_refuse(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """The statement starts on the expiry's POSTING day (after the expiry itself). The ledger
    already holds the ``expired`` row, so measuring the contract the day before the statement
    read flat and the whole convert answered 422 ``option_expiry_no_opening``. Measured the day
    before the contract's expiry, the converter re-derives the row and the CSV door refuses the
    overlap in zh."""
    _short_put_with_expiry(golden_db)
    expired = '12/21/2026,Expired,XYZ 12/18/2026 50.00 P,"OPTION EXPIRATION",2,,,$0.00'
    r = api_client.post("/api/broker/convert", json={
        "account": "schwab", "broker": "schwab", "currency": "USD",
        "exports": [{"name": "b.csv", "text": _BROKER_HEADER + expired + "\n"}]})
    assert r.status_code == 200, r.text
    (row,) = r.json()["rows"]["transactions"]
    assert (row["cells"][1], row["cells"][2], row["cells"][3], row["cells"][4],
            row["cells"][11]) == (PUT, "BUY", "2026-12-18", "200", "expired")
    (prow,) = _preview_rows(api_client, "transactions", r.json()["files"]["transactions"])
    assert prow["status"] == "error"
    assert "option_row_after_expiry_close" in prow["kinds"], prow
    assert prow["reason"] == STRANDED   # the cause, not its consequence 「沒有部位」


# --- M3: an OPTION_ADJUST never strands a stored close row of its source ---------------------


def _strand_setup(conn: sqlite3.Connection) -> None:
    _short_call(conn)
    _trade(conn, C60, Side.BUY, "200", "0", date(2026, 12, 18), close_reason="expired")
    conn.commit()


_STRANDS = ("此合約已登錄到期作廢／被指派／履約列（2026-12-18），合約調整會使該列無法入帳；"
            "請先修改或刪除該列")


def test_the_form_refuses_an_adjustment_that_strands_a_stored_close_row(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    _strand_setup(golden_db)
    r = api_client.post("/api/ledgers/corporate-actions/preview", json=_adjust_body())
    assert "option_adjust_strands_close" in _codes(r.json()["issues"], "error"), r.text
    r = api_client.post("/api/ledgers/corporate-actions", json=_adjust_body(ack_warnings=True))
    assert r.status_code == 400, r.text
    assert r.json()["error"]["message"] == _STRANDS
    assert list_corporate_actions(golden_db) == []


def test_the_csv_door_refuses_an_adjustment_that_strands_a_stored_close_row(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """The broker one-click flow commits its ``OPTION_ADJUST`` rows through this door."""
    _strand_setup(golden_db)
    csv_text = f"{_CA_HEADER}schwab,2026-05-10,OPTION_ADJUST,{C60},{C20},3,1,,\n"
    (row,) = _preview_rows(api_client, "corporate_actions", csv_text)
    assert row["status"] == "error"
    assert "option_adjust_strands_close" in row["kinds"]
    r = api_client.post("/api/import/commit", json={
        "kind": "corporate_actions", "csv_text": csv_text, "ack_warnings": True})
    assert r.status_code == 200, r.text
    assert r.json()["written"] == 0
    assert list_corporate_actions(golden_db) == []


def test_a_close_row_before_the_adjustment_does_not_refuse_it(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """Scoped to close rows ON OR AFTER the action date: an earlier partial assignment
    closed what it closed, and the adjustment moves the remainder."""
    register_option_contract(golden_db, C60)
    _trade(golden_db, C60, Side.SELL, "400", "2", date(2026, 5, 4), fee="2.62", short=True)
    _trade(golden_db, C60, Side.BUY, "200", "0", date(2026, 5, 8), close_reason="assigned")
    golden_db.commit()
    r = api_client.post("/api/ledgers/corporate-actions", json=_adjust_body())
    assert r.status_code == 201, r.text
    assert shares_through(golden_db, "schwab", C20, on=date(2026, 5, 11)) == Decimal("-600")


# --- M4: the edit preview of a stored close row ---------------------------------------------


def test_the_edit_preview_of_a_stored_short_expiry_derives_its_side_without_it(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    exp = _short_put_with_expiry(golden_db)
    r = api_client.post("/api/input/manual/preview", json={
        "account_id": "schwab", "symbol": PUT, "side": "BUY", "date": "2026-12-18",
        "contracts": "2", "replaces_txn_id": exp, "close_reason": "expired"})
    assert r.status_code == 200, r.text
    assert not _codes(r.json()["issues"], "error"), r.json()["issues"]


def test_the_edit_preview_of_a_stored_exercise_derives_its_side_without_it(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    register_option_contract(golden_db, C60)
    _trade(golden_db, C60, Side.BUY, "100", "2", date(2026, 5, 4), fee="0.65")
    xid = _trade(golden_db, C60, Side.SELL, "100", "0", date(2026, 12, 18),
                 close_reason="exercised")
    golden_db.commit()
    r = api_client.post("/api/input/manual/preview", json={
        "account_id": "schwab", "symbol": C60, "side": "SELL", "date": "2026-12-18",
        "contracts": "1", "replaces_txn_id": xid, "close_reason": "exercised"})
    assert r.status_code == 200, r.text
    assert not _codes(r.json()["issues"], "error"), r.json()["issues"]


# --- m1: the manual door needs a direction on an option row ----------------------------------

_NEEDS_INTENT = "選擇權請選擇方向（賣出開倉／買進平倉／買進開倉／賣出平倉）或到期作廢／被指派／履約"


def test_an_option_row_without_an_intent_is_refused_on_the_manual_door(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """Without the intent OPT-D14 / OPT-D21 never ran: a plain BUY of 3 contracts against a
    2-contract short was written as one row that closes AND opens."""
    register_option_contract(golden_db, PUT)
    _trade(golden_db, PUT, Side.SELL, "200", "1.5", date(2026, 5, 4), fee="1.31", short=True)
    golden_db.commit()
    body = {"account_id": "schwab", "symbol": PUT, "date": "2026-06-01", "price": "0.40",
            "side": "BUY", "shares": "300"}
    for door in ("preview", "commit"):
        r = api_client.post(f"/api/input/manual/{door}", json=body)
        assert r.status_code == 400, (door, r.text)
        assert r.json()["error"]["message"] == _NEEDS_INTENT
    assert [t.side for t in list_transactions(golden_db, symbol=PUT)] == [Side.SELL]


def test_the_edit_preview_keeps_the_stock_rules_without_an_intent(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """The edit dialog previews through the manual door with ``replaces_txn_id`` and carries
    no intent (spec §4.2: CSV / edit rows keep the stock rules)."""
    register_option_contract(golden_db, PUT)
    sto = _trade(golden_db, PUT, Side.SELL, "200", "1.5", date(2026, 5, 4), fee="1.31",
                 short=True)
    golden_db.commit()
    r = api_client.post("/api/input/manual/preview", json={
        "account_id": "schwab", "symbol": PUT, "side": "SELL", "date": "2026-05-04",
        "contracts": "2", "price": "1.6", "short_sale": True, "replaces_txn_id": sto})
    assert r.status_code == 200, r.text


# --- m2: an advisory never gates the corporate-action form -----------------------------------


def test_the_new_contract_advisory_does_not_gate_the_adjustment_form(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    _short_call(golden_db)
    r = api_client.post("/api/ledgers/corporate-actions/preview", json=_adjust_body())
    issues = r.json()["issues"]
    assert _codes(issues) == ["option_contract_autoregister"], issues
    assert issues[0]["sev"] == "info"
    assert (r.json()["needs_confirm"], r.json()["blocking"]) == (False, False)
    r = api_client.post("/api/ledgers/corporate-actions", json=_adjust_body())
    assert r.status_code == 201, r.text
    assert get_instrument(golden_db, C20) is not None


# --- nits ------------------------------------------------------------------------------------


def test_a_close_row_preview_shows_no_option_fee_label(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    _short_put_with_expiry_without_close(golden_db)
    r = api_client.post("/api/input/manual/preview", json={
        "account_id": "schwab", "symbol": PUT, "date": "2026-12-18", "contracts": "2",
        "close_reason": "expired"})
    assert r.status_code == 200, r.text
    assert (r.json()["fee"], r.json()["fee_rule_label"]) == ("0", None)


def _short_put_with_expiry_without_close(conn: sqlite3.Connection) -> None:
    register_option_contract(conn, PUT)
    _trade(conn, PUT, Side.SELL, "200", "1.5", date(2026, 5, 4), fee="1.31", short=True)
    conn.commit()


def test_a_fractional_contract_count_reads_whole_contracts_on_the_manual_door(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    register_option_contract(golden_db, PUT)
    golden_db.commit()
    for door in ("preview", "commit"):
        r = api_client.post(f"/api/input/manual/{door}", json={
            "account_id": "schwab", "symbol": PUT, "date": "2026-06-01", "price": "1.5",
            "contracts": "1.5", "option_intent": "STO"})
        assert r.status_code == 400, (door, r.text)
        assert r.json()["error"]["message"] == "口數須為正整數"
        assert r.json()["error"]["field"] == "contracts"
    assert list_transactions(golden_db, symbol=PUT) == []


def test_a_fractional_contract_count_reads_whole_contracts_on_the_edit_door(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    register_option_contract(golden_db, PUT)
    sto = _trade(golden_db, PUT, Side.SELL, "200", "1.5", date(2026, 5, 4), fee="1.31",
                 short=True)
    golden_db.commit()
    r = api_client.put(f"/api/ledgers/transactions/{sto}", json={
        "account_id": "schwab", "symbol": PUT, "side": "SELL", "date": "2026-05-04",
        "contracts": "1.5", "price": "1.5", "fee": "1.31", "tax": "0"})
    assert r.status_code == 400, r.text
    assert r.json()["error"]["message"] == "口數須為正整數"
