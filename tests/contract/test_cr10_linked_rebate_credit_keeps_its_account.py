"""CR-10 (pre-merge review, 2026-10-04): a linked 折讓款 credit cannot be moved to another
account, because its account IS half of the link.

DEF-009 made a booked rebate credit editable — date / kind / amount / note — and moved the
inbox's "already credited" key off those fields onto an explicit link,
``(cash_movements.account_id, cash_movements.rebate_period)`` (``api/rebates._confirmed_months``).
The account half of that key stayed an ordinary editable column. Moving the credit to
another account through ``PUT /api/cash/movements/{id}`` therefore re-opened the original
account's month as pending, and confirming it again credited the same refund twice — the
F2d/F12 double credit the link exists to prevent.

The PUT now refuses an account change on a linked credit with a zh reason that names both
correct paths. The cash page's own dialog never offered an account change (it always posts
the row's own account), so nothing the owner can click changes; only the hole closes.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import insert_transaction
from portfolio_dash.shared.models.enums import Side

_TW = "tw_broker"


def _pending(api_client: TestClient) -> list[str]:
    return [r["month"] for r in api_client.get("/api/rebates").json()["rows"]
            if r["account_id"] == _TW]


def _movement(api_client: TestClient, move_id: int) -> dict[str, Any]:
    rows = api_client.get("/api/cash", params={"limit": 500}).json()["movements"]["rows"]
    row: dict[str, Any] = next(r for r in rows if r["id"] == move_id)
    return row


def _confirm_may(api_client: TestClient, golden_db: sqlite3.Connection) -> int:
    insert_transaction(
        golden_db, account_id=_TW, symbol="2330", side=Side.BUY, quantity=Decimal("1000"),
        price=Decimal("500"), fees=Decimal("142"), tax=Decimal("0"),
        trade_date=date(2026, 5, 5))
    assert "2026-05" in _pending(api_client)
    r = api_client.post("/api/rebates/confirm",
                        json={"account_id": _TW, "month": "2026-05", "amount": "109"})
    assert r.status_code == 200, r.json()
    return int(r.json()["id"])


def test_moving_a_linked_credit_to_another_account_is_refused(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    move_id = _confirm_may(api_client, golden_db)
    before = _movement(api_client, move_id)
    r = api_client.put(f"/api/cash/movements/{move_id}", json={
        "account_id": "schwab", "ccy": "TWD", "ack_negative": True,
        "date": before["date"], "kind": "rebate", "amount": "109", "note": before["note"]})
    assert r.status_code == 400, r.json()
    err = r.json()["error"]
    assert err["field"] == "account_id"
    assert "2026-05" in err["message"] and "折讓款" in err["message"]
    assert "刪除" in err["message"]                       # names the way to re-book it
    assert _movement(api_client, move_id)["account_id"] == _TW     # nothing written
    # The month stays booked, so it can never be confirmed (and credited) a second time.
    assert "2026-05" not in _pending(api_client)
    again = api_client.post("/api/rebates/confirm",
                            json={"account_id": _TW, "month": "2026-05", "amount": "109"})
    assert again.status_code == 400 and again.json()["error"]["field"] == "month"


def test_every_field_def009_freed_is_still_editable(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """Scope guard: DEF-009's ruling (date / kind / amount / note) is untouched — only the
    account, which IS the link, is held."""
    move_id = _confirm_may(api_client, golden_db)
    r = api_client.put(f"/api/cash/movements/{move_id}", json={
        "account_id": _TW, "ccy": "TWD", "ack_negative": True,
        "date": "2026-06-03", "kind": "deposit", "amount": "120", "note": "實收"})
    assert r.status_code == 200, r.json()
    row = _movement(api_client, move_id)
    assert (row["date"], row["kind"], row["amount"], row["note"], row["rebate_period"]) == (
        "2026-06-03", "deposit", "120", "實收", "2026-05")


def test_an_unlinked_movement_may_still_change_account(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """A hand-entered row carries no link, so its account is an ordinary field."""
    r = api_client.post("/api/cash/movements", json={
        "account_id": _TW, "date": "2026-06-01", "kind": "deposit", "ccy": "TWD",
        "amount": "500"})
    assert r.status_code == 201, r.json()
    move_id = int(r.json()["id"])
    moved = api_client.put(f"/api/cash/movements/{move_id}", json={
        "account_id": "schwab", "ccy": "TWD", "ack_negative": True,
        "date": "2026-06-01", "kind": "deposit", "amount": "500", "note": None})
    assert moved.status_code == 200, moved.json()
    assert _movement(api_client, move_id)["account_id"] == "schwab"
