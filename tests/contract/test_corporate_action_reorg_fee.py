"""DEF-020 (functional test manual D-10, 2026-09-23): the reorganisation fee is ONE write
with the action, linked to it, and leaves with it.

The form used to post the fee as a SECOND request (``POST /api/cash/movements``) after
``POST /api/ledgers/corporate-actions`` had committed. Measured on the demo site: a 502 on
the second request left an action with no fee; deleting the action left the fee standing,
so the TWD pool read 6,067,545 against 6,067,595 before the action, with nothing on the
cash page to say why. Two writers, no link, no transaction.

Now: ``reorg_fee`` rides in the action body; the route validates it through the SAME cash
guard the cash door runs, writes the WITHDRAW under the action's own commit with
``cash_movements.corporate_action_id`` set, ``DELETE`` (single and set) removes exactly the
linked rows in the same transaction, and ``PUT`` syncs the fee (amount, date, account)
under one commit. The 「byte-identical」 assertion below is the one that matters: after
save + delete the ``cash_movements`` table must be exactly what it was, row for row.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import (
    insert_cash_movement,
    insert_transaction,
    list_cash_movements,
    list_corporate_actions,
)
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.enums import Side

D = Decimal
_BASE = "/api/ledgers/corporate-actions"
SPLIT_DAY = date(2026, 6, 10)


def _body(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "account_id": "tw_broker", "date": SPLIT_DAY.isoformat(), "kind": "SPLIT",
        "from_symbol": "2330", "to_symbol": "2330",
        "ratio_to": "10", "ratio_from": "1",
    }
    base.update(over)
    return base


def _fund(conn: sqlite3.Connection, account_id: str = "tw_broker",
          ccy: Currency = Currency.TWD, amount: str = "1000000") -> None:
    """The golden pools carry no deposits (the 2330 buy alone leaves TWD at −495,000), so
    the withdraw guard would refuse ANY fee. Fund the pool the way a real ledger is."""
    insert_cash_movement(conn, account_id=account_id, move_date=date(2026, 1, 1),
                         kind="DEPOSIT", ccy=ccy, amount=D(amount))
    conn.commit()


def _rows(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    """Every cash_movements row, every column, as stored — the byte-identical oracle."""
    return [tuple(r) for r in conn.execute("SELECT * FROM cash_movements ORDER BY id")]


def _linked(conn: sqlite3.Connection) -> list[Any]:
    return [m for m in list_cash_movements(conn) if m.corporate_action_id is not None]


# ------------------------------------------------------------------ one request, one write


def test_the_fee_lands_with_the_action_and_is_linked_to_it(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _fund(golden_db)
    r = api_client.post(_BASE, json=_body(reorg_fee="50"))
    assert r.status_code == 201, r.text
    resp = r.json()
    fee = resp["reorg_fee"]
    assert fee["amount"] == "50" and fee["ccy"] == "TWD" and fee["kind"] == "WITHDRAW"
    assert fee["date"] == SPLIT_DAY.isoformat() and fee["account_id"] == "tw_broker"
    (m,) = _linked(golden_db)
    assert m.id == fee["movement_id"]
    assert m.corporate_action_id == resp["ids"][0]
    assert m.amount == D("50") and m.ccy is Currency.TWD and m.kind == "WITHDRAW"
    assert m.note == f"重組費用 2330 {SPLIT_DAY.isoformat()}"


def test_no_fee_means_no_movement(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _fund(golden_db)
    before = _rows(golden_db)
    for body in (_body(), _body(reorg_fee=""), _body(reorg_fee="0")):
        golden_db.execute("DELETE FROM corporate_actions")
        golden_db.commit()
        r = api_client.post(_BASE, json=body)
        assert r.status_code == 201, r.text
        assert r.json()["reorg_fee"] is None
        assert _rows(golden_db) == before


def test_the_list_row_carries_the_linked_fee(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """What the delete confirm reads: the amount and currency that will leave with the row."""
    _fund(golden_db)
    assert api_client.post(_BASE, json=_body(reorg_fee="50")).status_code == 201
    (row,) = api_client.get(_BASE).json()["rows"]
    assert row["reorg_fee"]["amount"] == "50" and row["reorg_fee"]["ccy"] == "TWD"
    assert row["reorg_fee"]["kind_label"] == "出金"


# ---------------------------------------------------------------------- refused as a whole


def test_a_fee_the_cash_guard_refuses_refuses_the_whole_action(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """No deposit: the pool cannot cover 50, so the same guard the cash door runs refuses
    the fee — and the action is NOT written either. Before: the action committed, the
    second request failed, and the toast said 「重組費用登錄失敗」 over a saved action."""
    r = api_client.post(_BASE, json=_body(reorg_fee="50"))
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "withdraw_insufficient_balance" and err["field"] == "reorg_fee"
    assert "公司行動也未寫入" in err["message"]
    assert list_corporate_actions(golden_db) == []
    assert _linked(golden_db) == []


@pytest.mark.parametrize("bad, field", [
    ("-5", "reorg_fee"), ("abc", "reorg_fee"), ("50", "reorg_fee_ccy"),
], ids=["negative", "not a number", "unknown currency"])
def test_a_malformed_fee_is_a_zh_400_that_writes_nothing(
    api_client: TestClient, golden_db: sqlite3.Connection, bad: str, field: str
) -> None:
    _fund(golden_db)
    over: dict[str, object] = {"reorg_fee": bad}
    if field == "reorg_fee_ccy":
        over["reorg_fee_ccy"] = "XYZ"
    r = api_client.post(_BASE, json=_body(**over))
    assert r.status_code == 400, r.text
    assert r.json()["error"]["field"] == field
    assert list_corporate_actions(golden_db) == [] and _linked(golden_db) == []


def test_the_action_and_the_fee_are_atomic(
    api_client: TestClient, golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ The second step fails AFTER the action rows are inserted: nothing may survive."""
    import portfolio_dash.api.routers.ledgers as mod

    _fund(golden_db)
    before = _rows(golden_db)

    def boom(*_a: object, **_k: object) -> int:
        raise RuntimeError("simulated failure between the two writes")

    monkeypatch.setattr(mod, "insert_cash_movement", boom)
    try:
        r = api_client.post(_BASE, json=_body(reorg_fee="50"))
    except RuntimeError:
        pass          # TestClient re-raises server exceptions by default
    else:
        assert r.status_code == 500, r.text
    assert list_corporate_actions(golden_db) == [], "the action rows must roll back too"
    assert _rows(golden_db) == before


# --------------------------------------------------------------------- delete cascades


def test_delete_removes_the_linked_fee_and_the_cash_ledger_is_byte_identical(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _fund(golden_db)
    before = _rows(golden_db)
    r = api_client.post(_BASE, json=_body(reorg_fee="50"))
    assert r.status_code == 201, r.text
    assert _rows(golden_db) != before
    (action_id,) = r.json()["ids"]

    d = api_client.delete(f"{_BASE}/{action_id}")
    assert d.status_code == 200, d.text
    (gone,) = d.json()["fee_deleted"]
    assert gone["amount"] == "50" and gone["ccy"] == "TWD"
    assert _rows(golden_db) == before, "the pool must be exactly where it was"
    assert list_corporate_actions(golden_db) == []


def test_delete_of_a_whole_set_removes_the_fee_too(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """AAPL in two accounts (E13 writes two rows); the fee sits on the submitting one and
    the set delete — the only way to leave a multi-account set — takes it along."""
    insert_transaction(golden_db, account_id="moomoo_my", symbol="AAPL", side=Side.BUY,
                       quantity=D("20"), price=D("90"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 1, 5))
    _fund(golden_db, account_id="schwab", ccy=Currency.USD, amount="10000")
    before = _rows(golden_db)
    r = api_client.post(_BASE, json=_body(
        account_id="schwab", from_symbol="AAPL", to_symbol="AAPL",
        ratio_to="4", ratio_from="1", reorg_fee="5"))
    assert r.status_code == 201, r.text
    assert r.json()["written"] == 2
    (m,) = _linked(golden_db)
    assert m.account_id == "schwab" and m.ccy is Currency.USD and m.amount == D("5")

    d = api_client.delete(f"{_BASE}/set", params={
        "from_symbol": "AAPL", "date": SPLIT_DAY.isoformat(), "kind": "SPLIT"})
    assert d.status_code == 200, d.text
    assert d.json()["deleted"] == 2 and len(d.json()["fee_deleted"]) == 1
    assert _rows(golden_db) == before
    assert list_corporate_actions(golden_db) == []


def test_a_movement_entered_on_its_own_is_never_taken_by_a_delete(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """Only the LINKED row leaves. A withdraw the owner booked themselves on the cash page —
    same account, same day, same amount — is theirs and stays."""
    _fund(golden_db)
    insert_cash_movement(golden_db, account_id="tw_broker", move_date=SPLIT_DAY,
                         kind="WITHDRAW", ccy=Currency.TWD, amount=D("50"), note="mine")
    golden_db.commit()
    before = _rows(golden_db)
    r = api_client.post(_BASE, json=_body(reorg_fee="50"))
    assert r.status_code == 201, r.text
    (action_id,) = r.json()["ids"]
    assert api_client.delete(f"{_BASE}/{action_id}").status_code == 200
    assert _rows(golden_db) == before
    assert [m.note for m in list_cash_movements(golden_db) if m.kind == "WITHDRAW"] == ["mine"]


# ---------------------------------------------------------------------------- edit syncs


def _edit_body(**over: object) -> dict[str, object]:
    body = _body(**over)
    body.setdefault("ack_warnings", False)
    return body


def test_edit_syncs_the_fee_amount_date_and_account(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _fund(golden_db)
    r = api_client.post(_BASE, json=_body(reorg_fee="50"))
    assert r.status_code == 201, r.text
    (action_id,) = r.json()["ids"]
    (m0,) = _linked(golden_db)

    moved = date(2026, 6, 11)
    e = api_client.put(f"{_BASE}/{action_id}", json=_edit_body(
        date=moved.isoformat(), reorg_fee="80"))
    assert e.status_code == 200, e.text
    assert e.json()["reorg_fee"]["amount"] == "80"
    (m1,) = _linked(golden_db)
    assert m1.id == m0.id, "the SAME movement is updated, not a second one written"
    assert m1.amount == D("80") and m1.date == moved
    assert m1.corporate_action_id == action_id


def test_edit_with_a_blank_fee_removes_the_linked_movement(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _fund(golden_db)
    before = _rows(golden_db)
    r = api_client.post(_BASE, json=_body(reorg_fee="50"))
    (action_id,) = r.json()["ids"]
    e = api_client.put(f"{_BASE}/{action_id}", json=_edit_body(reorg_fee=""))
    assert e.status_code == 200, e.text
    assert e.json()["reorg_fee"] is None
    assert _rows(golden_db) == before


def test_edit_without_the_key_leaves_the_fee_alone(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """`reorg_fee` absent means 「not my business」 — a caller that edits only the note must
    not silently strip a fee it never saw."""
    _fund(golden_db)
    r = api_client.post(_BASE, json=_body(reorg_fee="50"))
    (action_id,) = r.json()["ids"]
    after_save = _rows(golden_db)
    e = api_client.put(f"{_BASE}/{action_id}", json=_edit_body(note="edited"))
    assert e.status_code == 200, e.text
    assert e.json()["reorg_fee"]["amount"] == "50"
    assert _rows(golden_db) == after_save


def test_edit_adds_a_fee_to_an_action_that_had_none(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _fund(golden_db)
    r = api_client.post(_BASE, json=_body())
    (action_id,) = r.json()["ids"]
    assert _linked(golden_db) == []
    e = api_client.put(f"{_BASE}/{action_id}", json=_edit_body(reorg_fee="30"))
    assert e.status_code == 200, e.text
    (m,) = _linked(golden_db)
    assert m.corporate_action_id == action_id and m.amount == D("30")


def test_edit_that_the_cash_guard_refuses_changes_nothing(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """The guard runs BEFORE the row is touched, with the existing fee excluded from its own
    overdraft check — so a legal 50 → 80 passes and an impossible 50 → 9,999,999 leaves both
    the action and the fee as they were."""
    _fund(golden_db)
    r = api_client.post(_BASE, json=_body(reorg_fee="50", note="original"))
    (action_id,) = r.json()["ids"]
    after_save = _rows(golden_db)
    e = api_client.put(f"{_BASE}/{action_id}", json=_edit_body(
        reorg_fee="9999999", note="changed"))
    assert e.status_code == 422, e.text
    assert e.json()["error"]["code"] == "withdraw_insufficient_balance"
    assert _rows(golden_db) == after_save
    (a,) = list_corporate_actions(golden_db)
    assert a.note == "original"
