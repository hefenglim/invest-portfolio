"""DEF-009 (owner ruling 2026-09-24): an auto-booked 折讓款 row is fully editable, and audited.

Before: the cash page's 編輯 dialog on 「2026-02-01 台灣券商 折讓款 191 TWD」 (demo cash #34)
disabled 日期／類型／備註, and ``PUT /api/cash/movements/{id}`` refused a kind or date change
with 400 「折讓款的類型與日期已鎖定…」. The lock existed because the rebate inbox recognised a
credited month FROM those fields (the month before the date, the 「YYYY-MM 折讓款」 note tag,
kind REBATE) — so an edit re-opened the month and invited a second credit. And no cash edit
ever reached ``ledger_audit`` (demo: 12 ``transactions`` update rows, 0 ``cash_movements``).

After: the credit carries an explicit ``cash_movements.rebate_period`` link (written by the
confirm, backfilled once for existing credits) that the inbox reads FIRST and no edit touches;
the PUT no longer locks anything; every cash edit and delete writes its before-image.

Why nothing caught it: the lock WAS the pinned behaviour — two recorded pins in
``test_cash_movement_guard_contract.py`` and ``test_rebate_movement_kind_and_date_are_locked``
asserted the 400. They were re-recorded / replaced; the ruling is what changed.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from portfolio_dash.api import rebates as svc
from portfolio_dash.data_ingestion.schema import create_tables
from portfolio_dash.data_ingestion.store import insert_transaction
from portfolio_dash.shared.models.enums import Side

_TW = "tw_broker"


def _seed_may(conn: sqlite3.Connection) -> None:
    insert_transaction(
        conn, account_id=_TW, symbol="2330", side=Side.BUY, quantity=Decimal("1000"),
        price=Decimal("500"), fees=Decimal("142"), tax=Decimal("0"),
        trade_date=date(2026, 5, 5))


def _pending(api_client: TestClient) -> list[str]:
    return [r["month"] for r in api_client.get("/api/rebates").json()["rows"]
            if r["account_id"] == _TW]


def _audit(conn: sqlite3.Connection, row_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT action, before_json FROM ledger_audit WHERE table_name='cash_movements' "
        "AND row_id=? ORDER BY id", (str(row_id),)).fetchall()


def _movement(api_client: TestClient, move_id: int) -> dict[str, Any]:
    rows = api_client.get("/api/cash", params={"limit": 500}).json()["movements"]["rows"]
    row: dict[str, Any] = next(r for r in rows if r["id"] == move_id)
    return row


def _confirm_may(api_client: TestClient, golden_db: sqlite3.Connection) -> int:
    _seed_may(golden_db)
    assert "2026-05" in _pending(api_client)
    r = api_client.post("/api/rebates/confirm",
                        json={"account_id": _TW, "month": "2026-05", "amount": "109"})
    assert r.status_code == 200, r.json()
    return int(r.json()["id"])


def test_every_field_of_a_booked_rebate_is_editable_and_the_month_stays_booked(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    move_id = _confirm_may(api_client, golden_db)
    assert _movement(api_client, move_id)["rebate_period"] == "2026-05"
    edit = {"account_id": _TW, "ccy": "TWD", "ack_negative": True,
            "date": "2026-05-10", "kind": "deposit", "amount": "120", "note": "改成入金"}
    r = api_client.put(f"/api/cash/movements/{move_id}", json=edit)
    assert r.status_code == 200, r.json()
    row = _movement(api_client, move_id)
    assert (row["date"], row["kind"], row["amount"], row["note"]) == (
        "2026-05-10", "deposit", "120", "改成入金")
    # The link survives every edit — the month the credit booked stays booked…
    assert row["rebate_period"] == "2026-05"
    assert "2026-05" not in _pending(api_client)
    # …so it can never be confirmed (and credited) a second time.
    again = api_client.post("/api/rebates/confirm",
                            json={"account_id": _TW, "month": "2026-05", "amount": "109"})
    assert again.status_code == 400 and again.json()["error"]["field"] == "month"
    # …and a date moved into another month does not hide THAT month's refund: under the
    # old date key a credit dated 2026-05-10 would have read as April's booking.
    insert_transaction(
        golden_db, account_id=_TW, symbol="2330", side=Side.BUY, quantity=Decimal("10"),
        price=Decimal("500"), fees=Decimal("20"), tax=Decimal("0"), trade_date=date(2026, 4, 20))
    assert "2026-04" in _pending(api_client)


def test_every_cash_edit_and_delete_writes_its_before_image(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    move_id = _confirm_may(api_client, golden_db)
    before = _movement(api_client, move_id)
    r = api_client.put(f"/api/cash/movements/{move_id}", json={
        "account_id": _TW, "ccy": "TWD", "ack_negative": True,
        "date": before["date"], "kind": "rebate", "amount": "100", "note": "實收 100"})
    assert r.status_code == 200, r.json()
    audit = _audit(golden_db, move_id)
    assert [a["action"] for a in audit] == ["update"]
    image = json.loads(audit[0]["before_json"])
    assert image["amount"] == "109" and image["note"] == "2026-05 折讓款"
    assert image["kind"] == "REBATE" and image["rebate_period"] == "2026-05"
    d = api_client.delete(f"/api/cash/movements/{move_id}", params={"ack_negative": True})
    assert d.status_code == 200, d.json()
    audit = _audit(golden_db, move_id)
    assert [a["action"] for a in audit] == ["update", "delete"]
    assert json.loads(audit[1]["before_json"])["amount"] == "100"
    # Deleting the credit is the one way to re-open its month (documented in the dialog).
    assert "2026-05" in _pending(api_client)


def test_a_hand_entered_rebate_keeps_the_legacy_keys(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """A REBATE typed on the cash page names no month: it has no link and the inbox reads it
    exactly as before (the month before its date, or its note tag)."""
    _seed_may(golden_db)
    r = api_client.post("/api/cash/movements", json={
        "account_id": _TW, "date": "2026-06-01", "kind": "rebate", "ccy": "TWD",
        "amount": "109"})
    assert r.status_code == 201, r.json()
    assert _movement(api_client, int(r.json()["id"]))["rebate_period"] is None
    assert "2026-05" not in _pending(api_client)


def test_the_migration_links_existing_credits_to_the_month_they_already_booked() -> None:
    """A legacy database (no ``rebate_period`` column) gains the column on boot and every
    REBATE credit is linked to the month the inbox ALREADY read as booked — the note tag
    when it parses, else the month before the credit's date. No month flips to open."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE cash_movements (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "account_id TEXT NOT NULL, date TEXT NOT NULL, kind TEXT NOT NULL, "
        "ccy TEXT NOT NULL, amount TEXT NOT NULL, note TEXT, acq_home_amount TEXT)")
    rows = [
        ("tw_broker", "2026-02-01", "REBATE", "TWD", "191", "2026-01 折讓款"),   # demo #34
        ("tw_broker", "2026-04-01", "REBATE", "TWD", "50", "note edited away"),
        ("tw_broker", "2026-01-01", "REBATE", "TWD", "10", None),               # Jan → Dec
        ("tw_broker", "2026-03-01", "DEPOSIT", "TWD", "999", "2026-02 折讓款"),  # not a credit
    ]
    conn.executemany(
        "INSERT INTO cash_movements (account_id, date, kind, ccy, amount, note) "
        "VALUES (?,?,?,?,?,?)", rows)
    create_tables(conn)
    got = [tuple(r) for r in conn.execute(
        "SELECT id, rebate_period FROM cash_movements ORDER BY id")]
    assert got == [(1, "2026-01"), (2, "2026-03"), (3, "2025-12"), (4, None)]
    # A second boot is a no-op (the backfill runs only when the column is first added).
    conn.execute("UPDATE cash_movements SET rebate_period=NULL WHERE id=3")
    create_tables(conn)
    assert conn.execute("SELECT rebate_period FROM cash_movements WHERE id=3").fetchone()[0] \
        is None
    conn.close()


def test_the_inbox_reads_the_link_before_the_row_fields(golden_db: sqlite3.Connection) -> None:
    """Service level: a linked credit suppresses exactly its own month, whatever its kind
    and date now say."""
    golden_db.execute(
        "INSERT INTO cash_movements (account_id, date, kind, ccy, amount, note, rebate_period) "
        "VALUES ('tw_broker', '2026-09-09', 'DEPOSIT', 'TWD', '5', 'x', '2026-03')")
    confirmed = svc._confirmed_months(golden_db)
    assert ("tw_broker", "2026-03") in confirmed
    assert ("tw_broker", "2026-08") not in confirmed   # the date no longer speaks for it
