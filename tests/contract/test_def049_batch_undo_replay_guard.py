"""DEF-049 (functional test manual I-08, 2026-09-24): the import-batch undo runs the SAME replay
guard as every ledger-tab delete, audits every row it removes, and logs what it did.

Measured on demo before the fix: CSV import 2884 buy 100 (batch 15) → a hand-entered sell of 150
(holding 200 → 50) → 復原 on batch 15 answered 200 ``{"deleted": 1}`` with no warning at all. The
dashboard then read 2884 ``shares -50``, ``adjusted_cost_total 0`` (basis discarded), XIRR
``null``. Deleting the SAME buy on the 交易 ledger tab answered 422 ``oversell`` and asked first.
``provenance.delete_batch`` ran a bare ``DELETE FROM {table} WHERE import_batch_id=?``: no
replay, no ``ledger_audit`` row.

Every case below drives the real HTTP door (``DELETE /api/import/batches/{id}``) and asserts the
RESPONSE and the resulting DATABASE — none of them reads source text:

* a later sell the batch's buy covers → 422 ``oversell`` naming date / symbol / sold / held and
  the consequence; nothing deleted, nothing audited; ``ack_oversell=true`` → 200, one
  ``ledger_audit`` 'delete' row per removed row (full before-image, 「批次復原 #id」), and ONE
  action-log row that says which batch, how many rows, and that a 賣超 was acknowledged;
* a dividend only the batch's buy supports → 422 ``orphan_correction``, and no ack passes it;
* a pre-existing, UNRELATED 賣超 → not blocked;
* the batch's corporate actions and DRIP shares count in the would-be ledger;
* a batch holding a buy AND the sell it covers, and a dividend batch that strands nothing → 200;
* a deposit batch that funded a later withdrawal → 422 ``negative_cash`` until ``ack_negative``
  (the cash doors' own check); an already-short pool the batch did not cause → not blocked.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.provenance import delete_batch
from portfolio_dash.data_ingestion.store import (
    StoredCorporateAction,
    insert_cash_movement,
    insert_dividend,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

_TXN_HEADER = "account,symbol,side,date,shares,price\n"


def _commit(client: TestClient, kind: str, csv_text: str) -> int:
    r = client.post("/api/import/commit", json={
        "kind": kind, "csv_text": csv_text, "ack_warnings": True, "source_name": "t.csv"})
    assert r.status_code == 200, r.text
    batch = r.json().get("import_batch_id")
    assert isinstance(batch, int), r.json()
    return batch


def _sell(conn: sqlite3.Connection, account: str, symbol: str, day: date, qty: str,
          price: str = "550") -> int:
    """A sell entered by hand (no batch id) — the row the undo must not strand silently."""
    return insert_transaction(conn, account_id=account, symbol=symbol, side=Side.SELL,
                              quantity=Decimal(qty), price=Decimal(price), fees=Decimal("0"),
                              tax=Decimal("0"), trade_date=day)


def _count(conn: sqlite3.Connection, table: str, where: str = "1=1",
           params: tuple[Any, ...] = ()) -> int:
    return int(conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0])  # noqa: S608


def _audit(conn: sqlite3.Connection, batch: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT table_name, row_id, action, before_json, source FROM ledger_audit "
        "WHERE source=? ORDER BY id", (f"批次復原 #{batch}",)).fetchall()


def _log(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    return [(str(r[0]), int(r[1])) for r in conn.execute(
        "SELECT action, status FROM action_log WHERE path LIKE '/api/import/batches/%' "
        "ORDER BY id")]


def _listed(client: TestClient) -> set[int]:
    return {b["id"] for b in client.get("/api/import/batches").json()["batches"]}


# ------------------------------------------------------------------ the verifier's repro


def test_an_undo_that_strands_a_later_sell_is_refused_and_names_it(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """Golden: tw_broker holds 2330 × 1000 since 2026-01-05. The batch adds 100 on 02-02 and a
    hand sell of 1050 on 02-10 is covered only because of it."""
    batch = _commit(api_client, "transactions",
                    _TXN_HEADER + "tw_broker,2330,BUY,2026-02-02,100,500\n")
    _sell(golden_db, "tw_broker", "2330", date(2026, 2, 10), "1050")
    before = _count(golden_db, "transactions")

    r = api_client.delete(f"/api/import/batches/{batch}")
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "oversell"
    msg = err["message"]
    for part in ("2026-02-10", "2330", "賣出 1050 股", "超過當日持股 1000 股",
                 "{account:tw_broker}", "成本基礎會被捨棄（待釐清）"):
        assert part in msg, (part, msg)
    assert err["issues"] == [{
        "kind": "oversell", "account_id": "tw_broker", "symbol": "2330",
        "date": "2026-02-10", "sold": "1050", "held": "1000",
        "message": "{account:tw_broker} 2026-02-10 的 2330 賣出 1050 股，超過當日持股 1000 股",
    }]
    # Refused means refused: nothing deleted, nothing audited, the batch still offered.
    assert _count(golden_db, "transactions") == before
    assert _count(golden_db, "transactions", "import_batch_id=?", (batch,)) == 1
    assert _audit(golden_db, batch) == []
    assert batch in _listed(api_client)
    # The attempt is in the action log, with why nothing happened.
    assert _log(golden_db)[-1] == (f"匯入批次刪除（批次 #{batch}・未刪除：賣超待確認）", 422)


def test_acknowledged_undo_deletes_audits_every_row_and_logs_the_ack(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    batch = _commit(api_client, "transactions",
                    _TXN_HEADER + "tw_broker,2330,BUY,2026-02-02,100,500\n"
                                  "tw_broker,2330,BUY,2026-02-03,50,505\n")
    ids = sorted(int(r[0]) for r in golden_db.execute(
        "SELECT id FROM transactions WHERE import_batch_id=?", (batch,)))
    sell_id = _sell(golden_db, "tw_broker", "2330", date(2026, 2, 10), "1100")
    assert api_client.delete(f"/api/import/batches/{batch}").status_code == 422

    r = api_client.delete(f"/api/import/batches/{batch}?ack_oversell=true")
    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": 2, "import_batch_id": batch, "oversell_acknowledged": True}
    assert _count(golden_db, "transactions", "import_batch_id=?", (batch,)) == 0
    assert _count(golden_db, "transactions", "id=?", (sell_id,)) == 1   # the hand row stays
    assert batch not in _listed(api_client)

    audit = _audit(golden_db, batch)
    assert [(a["table_name"], a["row_id"], a["action"]) for a in audit] == [
        ("transactions", str(i), "delete") for i in ids]
    for a, i in zip(audit, ids, strict=True):
        before = json.loads(a["before_json"])
        assert before["id"] == i and before["import_batch_id"] == batch   # the FULL row
        assert before["symbol"] == "2330" and before["side"] == "BUY"
    assert _log(golden_db)[-1] == (
        f"匯入批次刪除（批次 #{batch}・刪除 2 筆・已確認賣超）", 200)

    # The dashboard now shows exactly what the owner acknowledged: the flagged 賣超.
    dash = api_client.get("/api/dashboard").json()
    held = next(h for h in dash["holdings"] if h["symbol"] == "2330")
    assert held.get("oversold") is True, held


def test_an_undo_that_strands_a_dividend_is_refused_and_no_ack_passes_it(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    upsert_instrument(golden_db, Instrument(symbol="2884", market=Market.TW,
                                            quote_ccy=Currency.TWD, sector="Financials",
                                            name="玉山金", board="TWSE"))
    batch = _commit(api_client, "transactions",
                    _TXN_HEADER + "tw_broker,2884,BUY,2026-02-02,100,30\n")
    insert_dividend(golden_db, account_id="tw_broker", symbol="2884", div_date=date(2026, 4, 1),
                    div_type="CASH", gross=Decimal("100"), withholding=Decimal("0"),
                    net=Decimal("100"))
    for suffix in ("", "?ack_oversell=true", "?ack_oversell=true&ack_negative=true"):
        r = api_client.delete(f"/api/import/batches/{batch}{suffix}")
        assert r.status_code == 422, r.text
        err = r.json()["error"]
        assert err["code"] == "orphan_correction", err
        assert "{account:tw_broker} 2884" in err["message"] and "股利" in err["message"]
    assert _count(golden_db, "transactions", "import_batch_id=?", (batch,)) == 1
    assert _audit(golden_db, batch) == []


def test_an_unrelated_pre_existing_oversell_never_blocks(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """schwab AAPL (10 held) is already 賣超 before the batch exists; undoing a 2330 buy that no
    sell needs must not be held hostage to it."""
    _sell(golden_db, "schwab", "AAPL", date(2026, 2, 1), "50", price="120")
    batch = _commit(api_client, "transactions",
                    _TXN_HEADER + "tw_broker,2330,BUY,2026-02-02,100,500\n")
    r = api_client.delete(f"/api/import/batches/{batch}")
    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": 1, "import_batch_id": batch}
    assert len(_audit(golden_db, batch)) == 1


def test_a_batch_holding_the_buy_and_the_sell_it_covers_undoes_cleanly(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    upsert_instrument(golden_db, Instrument(symbol="2884", market=Market.TW,
                                            quote_ccy=Currency.TWD, sector="Financials",
                                            name="玉山金", board="TWSE"))
    batch = _commit(api_client, "transactions",
                    _TXN_HEADER + "tw_broker,2884,BUY,2026-02-02,100,30\n"
                                  "tw_broker,2884,SELL,2026-02-03,100,31\n")
    r = api_client.delete(f"/api/import/batches/{batch}")
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == 2 and len(_audit(golden_db, batch)) == 2


def test_a_dividend_batch_that_strands_nothing_undoes(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    batch = _commit(api_client, "dividends",
                    "account,symbol,date,type,gross,withholding,net\n"
                    "tw_broker,2330,2026-04-15,CASH,1000,0,1000\n")
    # An ack sent with nothing to acknowledge is not recorded as one (payload nor log).
    r = api_client.delete(f"/api/import/batches/{batch}?ack_oversell=true&ack_negative=true")
    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": 1, "import_batch_id": batch}
    assert _log(golden_db)[-1] == (f"匯入批次刪除（批次 #{batch}・刪除 1 筆）", 200)
    (row,) = _audit(golden_db, batch)
    assert row["table_name"] == "dividends"
    assert json.loads(row["before_json"])["import_batch_id"] == batch


def test_drip_shares_a_later_sell_needs_count_in_the_would_be_ledger(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """A DRIP dividend adds shares: undoing it can strand a sell exactly like undoing a buy."""
    batch = _commit(api_client, "dividends",
                    "account,symbol,date,type,gross,withholding,net,reinvest_shares,"
                    "reinvest_price\n"
                    "schwab,AAPL,2026-03-02,DRIP,100,30,70,2,35\n")
    _sell(golden_db, "schwab", "AAPL", date(2026, 4, 1), "12", price="120")
    r = api_client.delete(f"/api/import/batches/{batch}")
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "oversell"
    assert "賣出 12 股，超過當日持股 10 股" in r.json()["error"]["message"]
    assert api_client.delete(
        f"/api/import/batches/{batch}?ack_oversell=true").status_code == 200


def test_the_batch_corporate_actions_leave_the_would_be_ledger_too(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """A SPLIT batch: the post-split sell of 5,000 is covered only by the split. The undo's
    would-be ledger must drop the batch's actions, not only its plain rows."""
    batch = _commit(api_client, "corporate_actions",
                    "account,date,kind,from_symbol,to_symbol,ratio_to,ratio_from\n"
                    "tw_broker,2026-05-01,SPLIT,2330,2330,10,1\n")
    _sell(golden_db, "tw_broker", "2330", date(2026, 5, 2), "5000", price="60")
    r = api_client.delete(f"/api/import/batches/{batch}")
    assert r.status_code == 422, r.text
    assert "賣出 5000 股，超過當日持股 1000 股" in r.json()["error"]["message"]
    ok = api_client.delete(f"/api/import/batches/{batch}?ack_oversell=true")
    assert ok.status_code == 200, ok.text
    (row,) = _audit(golden_db, batch)
    assert (row["table_name"], row["action"]) == ("corporate_actions", "delete")


# ------------------------------------------------------------------ cash / FX rows


def test_a_deposit_batch_that_funded_a_later_withdrawal_asks_first(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """Golden schwab USD: +1,000 (FX 01-08) − 1,000 (AAPL 01-10) = 0. The batch deposits 500 on
    02-01 and a hand withdrawal of 400 on 02-15 spends it — the undo would leave −400."""
    batch = _commit(api_client, "cash", "account,date,kind,ccy,amount\n"
                                        "schwab,2026-02-01,DEPOSIT,USD,500\n")
    insert_cash_movement(golden_db, account_id="schwab", move_date=date(2026, 2, 15),
                         kind="WITHDRAW", ccy=Currency.USD, amount=Decimal("400"))
    r = api_client.delete(f"/api/import/batches/{batch}")
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "negative_cash"
    assert "{account:schwab}" in err["message"] and "2026-02-15" in err["message"]
    # An oversell ack does not answer a cash question.
    assert api_client.delete(
        f"/api/import/batches/{batch}?ack_oversell=true").status_code == 422
    ok = api_client.delete(f"/api/import/batches/{batch}?ack_negative=true")
    assert ok.status_code == 200, ok.text
    assert ok.json() == {"deleted": 1, "import_batch_id": batch,
                         "negative_cash_acknowledged": True}
    (row,) = _audit(golden_db, batch)
    assert row["table_name"] == "cash_movements"
    assert _log(golden_db)[-1] == (
        f"匯入批次刪除（批次 #{batch}・刪除 1 筆・已確認現金為負）", 200)


def test_an_fx_batch_whose_dollars_were_spent_asks_first(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """The FX delete door's check (QA-10), over the batch: the conversion's TO-pool loses the
    credit that paid for a later withdrawal."""
    insert_cash_movement(golden_db, account_id="schwab", move_date=date(2026, 1, 2),
                         kind="DEPOSIT", ccy=Currency.TWD, amount=Decimal("100000"))
    batch = _commit(api_client, "fx", "account,date,from_ccy,from_amount,to_ccy,to_amount\n"
                                      "schwab,2026-02-01,TWD,16000,USD,500\n")
    insert_cash_movement(golden_db, account_id="schwab", move_date=date(2026, 2, 15),
                         kind="WITHDRAW", ccy=Currency.USD, amount=Decimal("400"))
    r = api_client.delete(f"/api/import/batches/{batch}")
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "negative_cash"
    assert _count(golden_db, "fx_conversions", "import_batch_id=?", (batch,)) == 1
    ok = api_client.delete(f"/api/import/batches/{batch}?ack_negative=true")
    assert ok.status_code == 200, ok.text
    (row,) = _audit(golden_db, batch)
    assert row["table_name"] == "fx_conversions"


def test_an_already_short_pool_the_batch_did_not_cause_does_not_ask(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """Golden tw_broker TWD sits at −500,000 from its first buy (no deposit is seeded). A 1,000
    deposit that funds nothing, undone, leaves that low exactly where it was."""
    batch = _commit(api_client, "cash", "account,date,kind,ccy,amount\n"
                                        "tw_broker,2026-02-01,DEPOSIT,TWD,1000\n")
    r = api_client.delete(f"/api/import/batches/{batch}")
    assert r.status_code == 200, r.text


# ------------------------------------------------------------------ the seam itself


def test_the_guard_is_a_required_seam(golden_db: sqlite3.Connection) -> None:
    """A second caller of ``delete_batch`` cannot undo without a guard (D39's lesson: a missed
    registration must be a TypeError, not a silently unguarded undo)."""
    def _no_actions(rows: list[StoredCorporateAction]) -> None:
        raise AssertionError(rows)

    with pytest.raises(TypeError):
        delete_batch(golden_db, 1, delete_actions=_no_actions)  # type: ignore[call-arg]
