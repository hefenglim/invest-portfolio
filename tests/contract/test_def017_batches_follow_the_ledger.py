"""DEF-017 (functional test manual C-04, 2026-09-23): the import history follows the ledger.

The manual 股利 form commits through the import pipeline, so each entry opens a batch; the
verifier deleted that dividend on the 股利 tab and the batch stayed — ``row_count 1``,
``committed`` — with a 復原 button whose dialog promised 「手動輸入的紀錄與其他批次不受影響」
and then deleted 0 rows. Every ledger DELETE door (six of them) removes rows by id and none of
them knew a batch existed.

The fix does not ask six doors to remember: ``GET /api/import/batches`` reads each batch's
``row_count`` LIVE from the rows that still carry its ``import_batch_id`` (the stored count
is kept as ``written_count``), and a batch with nothing left is not listed — whichever door
removed its rows. ``DELETE`` on such a batch answers truthfully (0 deleted, and why) and
clears the stale record. ``openings`` is the one kind whose rows carry no batch id (its
table upserts on ``(account, symbol)`` — ``provenance.py``), so its batch is listed with
``undoable: false`` and its undo is refused rather than 「復原」-ing nothing.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

_ROWS_BY_KIND = {
    "transactions": ("transactions",
                     "account,symbol,side,date,shares,price\n"
                     "tw_broker,2330,BUY,2026-02-02,100,500\n"
                     "tw_broker,2330,BUY,2026-02-03,100,510\n"),
    "dividends": ("dividends",
                  "account,symbol,date,type,gross,withholding,net\n"
                  "tw_broker,2330,2026-04-15,CASH,1000,0,1000\n"
                  "tw_broker,2330,2026-05-15,CASH,1200,0,1200\n"),
    "fx": ("fx_conversions",
           "account,date,from_ccy,from_amount,to_ccy,to_amount\n"
           "schwab,2026-06-02,USD,10,TWD,320\n"
           "schwab,2026-06-03,USD,10,TWD,321\n"),
    "cash": ("cash_movements",
             "account,date,kind,ccy,amount\n"
             "schwab,2026-06-01,DEPOSIT,USD,1000\n"
             "schwab,2026-06-02,DEPOSIT,USD,500\n"),
}


def _commit(client: TestClient, kind: str, csv_text: str, **extra: object) -> int:
    r = client.post("/api/import/commit", json={
        "kind": kind, "csv_text": csv_text, "ack_warnings": True, **extra})
    assert r.status_code == 200, r.text
    batch = r.json().get("import_batch_id")
    assert isinstance(batch, int), r.json()
    return batch


def _listed(client: TestClient) -> dict[int, dict[str, object]]:
    return {b["id"]: b for b in client.get("/api/import/batches").json()["batches"]}


def _ids(conn: sqlite3.Connection, table: str, batch: int) -> list[int]:
    rows = conn.execute(f"SELECT id FROM {table} WHERE import_batch_id=? ORDER BY id",  # noqa: S608
                        (batch,)).fetchall()
    return [int(r[0]) for r in rows]


_DELETE_PATH = {
    "transactions": "/api/ledgers/transactions/{id}?ack_oversell=true",
    "dividends": "/api/ledgers/dividends/{id}?ack_oversell=true",
    "fx": "/api/ledgers/fx/{id}?ack_negative=true",
    "cash": "/api/cash/movements/{id}?ack_negative=true",
}


@pytest.mark.parametrize("kind", sorted(_ROWS_BY_KIND))
def test_deleting_rows_on_the_ledger_is_reflected_in_the_batch_list(
    kind: str, api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    table, csv_text = _ROWS_BY_KIND[kind]
    if kind == "fx":   # golden schwab USD is empty; a conversion may never overdraft (FU-D34)
        assert api_client.post("/api/cash/movements", json={
            "account_id": "schwab", "date": "2026-06-01", "kind": "deposit",
            "ccy": "USD", "amount": "1000"}).status_code == 201
    batch = _commit(api_client, kind, csv_text)
    first, second = _ids(golden_db, table, batch)
    listed = _listed(api_client)[batch]
    assert listed["row_count"] == 2 and listed["written_count"] == 2
    assert listed["undoable"] is True

    # One of two rows deleted on the ledger door: the batch stays, and says 1.
    r = api_client.delete(_DELETE_PATH[kind].format(id=first))
    assert r.status_code == 200, r.text
    listed = _listed(api_client)[batch]
    assert listed["row_count"] == 1 and listed["written_count"] == 2

    # The last row deleted: the batch has nothing left to undo and is no longer listed.
    r = api_client.delete(_DELETE_PATH[kind].format(id=second))
    assert r.status_code == 200, r.text
    assert batch not in _listed(api_client)


def test_a_corporate_action_deleted_on_its_ledger_takes_its_batch_off_the_list(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    batch = _commit(api_client, "corporate_actions",
                    "account,date,kind,from_symbol,to_symbol,ratio_to,ratio_from\n"
                    "tw_broker,2026-06-10,SPLIT,2330,2330,10,1\n")
    (action_id,) = _ids(golden_db, "corporate_actions", batch)
    assert _listed(api_client)[batch]["row_count"] == 1
    r = api_client.delete(f"/api/ledgers/corporate-actions/{action_id}")
    assert r.status_code == 200, r.text
    assert batch not in _listed(api_client)


def test_the_verifier_case_manual_dividend_then_ledger_delete(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """The 股利 form's one-row commit — labelled by the form (Agent D sends 「手動輸入」) —
    then deleted on the 股利 tab: the history must not keep an empty 「最近匯入」 line."""
    batch = _commit(api_client, "dividends",
                    "account,symbol,date,type,gross,withholding,net\n"
                    "tw_broker,2330,2026-04-15,CASH,500,0,500\n",
                    source_name="手動輸入")
    assert _listed(api_client)[batch]["source_name"] == "手動輸入"
    (div_id,) = _ids(golden_db, "dividends", batch)
    gone = api_client.delete(f"/api/ledgers/dividends/{div_id}?ack_oversell=true")
    assert gone.status_code == 200, gone.text
    assert batch not in _listed(api_client)

    # A stale page still holding the batch's 復原 button is answered truthfully — and the
    # empty record is cleared, not left for the next reader.
    undo = api_client.delete(f"/api/import/batches/{batch}")
    assert undo.status_code == 200, undo.text
    assert undo.json() == {"deleted": 0, "import_batch_id": batch,
                           "message": "此批次的列已在帳本中刪除"}
    assert golden_db.execute(
        "SELECT COUNT(*) FROM import_batches WHERE id=?", (batch,)).fetchone()[0] == 0


def test_an_undo_with_rows_left_keeps_its_exact_payload(api_client: TestClient) -> None:
    """Counter-evidence: the ordinary undo is byte-identical to before (no ``message``)."""
    table, csv_text = _ROWS_BY_KIND["cash"]
    batch = _commit(api_client, "cash", csv_text)
    undo = api_client.delete(f"/api/import/batches/{batch}")
    assert undo.json() == {"deleted": 2, "import_batch_id": batch}


def test_an_openings_batch_is_listed_as_not_undoable_and_its_undo_is_refused(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    batch = _commit(api_client, "openings",
                    "account,symbol,shares,original_cost_total,build_date\n"
                    "tw_broker,2330,10,5000,2025-12-31\n")
    listed = _listed(api_client)[batch]
    assert listed["undoable"] is False
    assert listed["row_count"] == 1 and listed["written_count"] == 1
    undo = api_client.delete(f"/api/import/batches/{batch}")
    assert undo.status_code == 422, undo.text
    err = undo.json()["error"]
    assert err["code"] == "batch_not_undoable"
    assert "期初庫存" in err["message"]
    # Nothing was touched: the opening row and the batch record are both still there.
    assert golden_db.execute(
        "SELECT COUNT(*) FROM opening_inventory WHERE account_id='tw_broker' AND symbol='2330'"
    ).fetchone()[0] == 1
    assert batch in _listed(api_client)
