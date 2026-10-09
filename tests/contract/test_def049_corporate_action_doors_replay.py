"""DEF-049, the class fix: the corporate-action ledger's DELETE and EDIT doors replay the
would-be ledger like every other correction door.

Same class as the import-batch undo: ``DELETE /api/ledgers/corporate-actions/{id}`` and
``…/set`` went straight to ``_delete_actions`` and ``PUT …/{id}`` straight to
``update_corporate_action`` — no replay. Measured 2026-09-25 (golden ledger, tw_broker 2330 ×
1,000): a 10:1 SPLIT on 05-01, then a sell of 5,000 on 05-02; deleting the split answered 200
and the dashboard read 2330 at −4,000, 賣超, basis discarded. The ledger tab's delete of a buy
the same sell depended on answers 422 ``oversell`` and asks first.

Every case drives the real HTTP door and asserts the response and the database.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import (
    insert_dividend,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import DashboardClientFactory, _seed_dual_account

_SPLIT = {"account_id": "tw_broker", "date": "2026-05-01", "kind": "SPLIT",
          "from_symbol": "2330", "to_symbol": "2330", "ratio_to": "10", "ratio_from": "1",
          "ack_warnings": True}


def _add(client: TestClient, body: dict[str, Any]) -> int:
    r = client.post("/api/ledgers/corporate-actions", json=body)
    assert r.status_code == 201, r.text
    rows = client.get("/api/ledgers/corporate-actions", params={"limit": 500}).json()["rows"]
    return max(int(a["id"]) for a in rows)


def _sell(conn: sqlite3.Connection, account: str, symbol: str, day: date, qty: str) -> int:
    return insert_transaction(conn, account_id=account, symbol=symbol, side=Side.SELL,
                              quantity=Decimal(qty), price=Decimal("60"), fees=Decimal("0"),
                              tax=Decimal("0"), trade_date=day)


def _actions(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM corporate_actions").fetchone()[0])


def _audited(conn: sqlite3.Connection, action_id: int, action: str) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM ledger_audit WHERE table_name='corporate_actions' "
        "AND row_id=? AND action=?", (str(action_id), action)).fetchone()[0])


# ------------------------------------------------------------------ DELETE one row


def test_deleting_a_split_a_later_sell_needs_is_refused_then_acked(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    action_id = _add(api_client, _SPLIT)
    _sell(golden_db, "tw_broker", "2330", date(2026, 5, 2), "5000")

    r = api_client.delete(f"/api/ledgers/corporate-actions/{action_id}")
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "oversell"
    for part in ("此刪除將造成賣超", "{account:tw_broker} 2026-05-02 的 2330 賣出 5000 股",
                 "超過當日持股 1000 股", "成本基礎會被捨棄（待釐清）"):
        assert part in err["message"], (part, err["message"])
    assert err["issues"][0]["sold"] == "5000" and err["issues"][0]["held"] == "1000"
    assert _actions(golden_db) == 1 and _audited(golden_db, action_id, "delete") == 0

    ok = api_client.delete(f"/api/ledgers/corporate-actions/{action_id}?ack_oversell=true")
    assert ok.status_code == 200, ok.text
    assert _actions(golden_db) == 0 and _audited(golden_db, action_id, "delete") == 1


def test_a_split_nothing_depends_on_deletes_without_asking(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """…and a pre-existing, UNRELATED 賣超 (schwab AAPL) does not hold it hostage."""
    _sell(golden_db, "schwab", "AAPL", date(2026, 2, 1), "50")
    action_id = _add(api_client, _SPLIT)
    r = api_client.delete(f"/api/ledgers/corporate-actions/{action_id}")
    assert r.status_code == 200, r.text
    assert _actions(golden_db) == 0


def test_deleting_an_exchange_a_dividend_stands_on_is_refused_hard(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """The dividend on the exchange's destination has no position once the exchange is gone
    — the orphan arm, which no acknowledgement passes."""
    upsert_instrument(golden_db, Instrument(symbol="2884", market=Market.TW,
                                            quote_ccy=Currency.TWD, sector="Financials",
                                            name="玉山金", board="TWSE"))
    action_id = _add(api_client, {
        "account_id": "tw_broker", "date": "2026-04-01", "kind": "EXCHANGE",
        "from_symbol": "2330", "to_symbol": "2884", "ratio_to": "1", "ratio_from": "1",
        "ack_warnings": True})
    insert_dividend(golden_db, account_id="tw_broker", symbol="2884", div_date=date(2026, 5, 1),
                    div_type="CASH", gross=Decimal("100"), withholding=Decimal("0"),
                    net=Decimal("100"))
    for suffix in ("", "?ack_oversell=true&ack_negative=true"):
        r = api_client.delete(f"/api/ledgers/corporate-actions/{action_id}{suffix}")
        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "orphan_correction", r.json()
    assert _actions(golden_db) == 1


# ------------------------------------------------------------------ DELETE the whole set


def test_deleting_a_whole_set_replays_every_row_of_it(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """AAPL is held in schwab (30) and moomoo_my (10): a 4:1 split is one two-row set. A
    schwab sell of 100 after it is covered only by the split."""
    client = dashboard_client_factory(_seed_dual_account)
    client.post("/api/ledgers/corporate-actions", json={
        "account_id": "schwab", "date": "2026-03-01", "kind": "SPLIT", "from_symbol": "AAPL",
        "to_symbol": "AAPL", "ratio_to": "4", "ratio_from": "1", "ack_warnings": True})
    rows = client.get("/api/ledgers/corporate-actions",
                      params={"limit": 500}).json()["rows"]
    assert sorted(a["account_id"] for a in rows) == ["moomoo_my", "schwab"]
    sell = client.post("/api/input/manual/commit", json={
        "account_id": "schwab", "symbol": "AAPL", "side": "sell", "date": "2026-03-02",
        "shares": "100", "price": "30"})
    assert sell.status_code == 201, sell.text

    path = "/api/ledgers/corporate-actions/set?from_symbol=AAPL&date=2026-03-01&kind=SPLIT"
    r = client.delete(path)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "oversell"
    assert "{account:schwab} 2026-03-02 的 AAPL 賣出 100 股，超過當日持股 30 股" in err["message"]
    assert len(client.get("/api/ledgers/corporate-actions").json()["rows"]) == 2

    ok = client.delete(path + "&ack_oversell=true")
    assert ok.status_code == 200, ok.text
    assert ok.json()["deleted"] == 2
    assert client.get("/api/ledgers/corporate-actions").json()["rows"] == []


# ------------------------------------------------------------------ EDIT


def _edit_body(**change: str) -> dict[str, Any]:
    body: dict[str, Any] = {k: v for k, v in _SPLIT.items()}
    body.update(change)
    return body


def test_editing_the_ratio_under_a_later_sell_is_refused_then_acked(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    action_id = _add(api_client, _SPLIT)
    _sell(golden_db, "tw_broker", "2330", date(2026, 5, 2), "5000")
    r = api_client.put(f"/api/ledgers/corporate-actions/{action_id}",
                       json=_edit_body(ratio_to="2"))
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "oversell"
    assert "此更正將造成賣超" in err["message"]
    assert "賣出 5000 股，超過當日持股 2000 股" in err["message"]
    stored = golden_db.execute(
        "SELECT ratio_to FROM corporate_actions WHERE id=?", (action_id,)).fetchone()[0]
    assert str(stored) == "10"                       # refused means not written
    assert _audited(golden_db, action_id, "update") == 0

    ok = api_client.put(f"/api/ledgers/corporate-actions/{action_id}",
                        json=_edit_body(ratio_to="2", ack_oversell=True))  # type: ignore[arg-type]
    assert ok.status_code == 200, ok.text
    assert str(golden_db.execute(
        "SELECT ratio_to FROM corporate_actions WHERE id=?", (action_id,)).fetchone()[0]) == "2"


def test_moving_the_split_past_the_sell_stays_a_hard_refusal(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """Counter-evidence, measured while fixing: the DATE move was already refused before this
    fix — the §5 validator replays the ledger AT the action's new date, finds 2330 賣超
    there (5,000 sold against 1,000) and hard-rejects (E18 ``oversold_source``). The replay
    guard runs after it, so this answer — a 400, not an ack-able 422 — must not change."""
    action_id = _add(api_client, _SPLIT)
    _sell(golden_db, "tw_broker", "2330", date(2026, 5, 2), "5000")
    r = api_client.put(f"/api/ledgers/corporate-actions/{action_id}",
                       json=_edit_body(date="2026-05-03", ack_oversell=True))  # type: ignore[arg-type]
    assert r.status_code == 400, r.text
    assert r.json()["error"]["issues"][0]["code"] == "oversold_source"
    assert str(golden_db.execute(
        "SELECT date FROM corporate_actions WHERE id=?", (action_id,)).fetchone()[0]
    ) == "2026-05-01"


def test_an_edit_that_strands_nothing_saves_without_asking(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    action_id = _add(api_client, _SPLIT)
    _sell(golden_db, "tw_broker", "2330", date(2026, 5, 2), "500")
    r = api_client.put(f"/api/ledgers/corporate-actions/{action_id}",
                       json=_edit_body(ratio_to="2"))
    assert r.status_code == 200, r.text
