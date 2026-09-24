"""DEF-042 (functional test manual B-17 / OBS-3, owner ruling 2026-09-24): the ledger 「編輯」
door runs the SAME ``validate_transaction`` as a new entry, over "the ledger without this row +
the edited row".

Measured on the demo site (R2): 交易帳本 › 交易 › 編輯 → the date moved before the position's
opening build date → 儲存 (``PUT /api/ledgers/transactions/{id}``) succeeded with none of the
findings DEF-014 added to the entry door; the modal's own preview asked the ENTRY question
(「如果再新增這一筆」), so it counted the edited row twice — a sell covered only by itself
previewed as a 賣超, and every unchanged row was 「a duplicate of itself」.

Pinned through the real doors (TestClient):

* the edit preview (``/api/input/manual/preview`` + ``replaces_txn_id``) and the PUT both
  raise the DEF-014 date findings, and the PUT returns them in its 200 body;
* the edited row neither covers, strands nor duplicates ITSELF, and a same-day row entered
  AFTER it does not cover it (an edit keeps its id, DEF-012's write order);
* HARD findings are refused (400) only when the edit introduces them — a legacy row's own
  condition stays correctable, the LOW-3 rule — and the row's own 賣超 is the entry door's
  422 ``oversell`` until acknowledged.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_transaction,
    upsert_instrument,
    upsert_opening,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import DashboardClientFactory

D = Decimal


def _tx(conn: sqlite3.Connection, side: Side, qty: str, price: str, d: date,
        *, short: bool = False) -> int:
    return insert_transaction(conn, account_id="tw_broker", symbol="2330", side=side,
                              quantity=D(qty), price=D(price), fees=D("0"), tax=D("0"),
                              trade_date=d, short_sale=short)


def _base(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Semiconductors", name="TSMC", board="TWSE"))
    upsert_opening(conn, account_id="tw_broker", symbol="2330", shares=D("1000"),
                   original_cost_total=D("500000"), build_date=date(2026, 2, 1))
    _tx(conn, Side.BUY, "100", "600", date(2026, 3, 2))       # id 1
    _tx(conn, Side.SELL, "50", "610", date(2026, 4, 1))       # id 2
    _tx(conn, Side.BUY, "50", "605", date(2026, 4, 1))        # id 3 — entered AFTER id 2


def _rows(client: TestClient) -> list[dict[str, Any]]:
    return list(client.get("/api/ledgers/transactions", params={"limit": 500}).json()["rows"])


def _row(client: TestClient, txn_id: int) -> dict[str, Any]:
    return next(r for r in _rows(client) if r["id"] == txn_id)


def _edit_body(row: dict[str, Any], **over: Any) -> dict[str, Any]:
    body = {"account_id": row["account_id"], "symbol": row["symbol"], "side": row["side"],
            "date": row["date"], "shares": row["shares"], "price": row["price"],
            "fee": row["fee"], "tax": row["tax"], "note": row["note"]}
    body.update(over)
    return body


def _preview(client: TestClient, txn_id: int, **over: Any) -> dict[str, Any]:
    row = _row(client, txn_id)
    body = {"account_id": row["account_id"], "symbol": row["symbol"], "side": row["side"],
            "date": row["date"], "shares": row["shares"], "price": row["price"],
            "replaces_txn_id": txn_id}
    body.update(over)
    r = client.post("/api/input/manual/preview", json=body)
    assert r.status_code == 200, r.text
    return dict(r.json())


def _codes(payload: dict[str, Any]) -> list[str]:
    return [i["code"] for i in payload.get("issues") or []]


def _id_of(client: TestClient, side: str, day: str) -> int:
    return int(next(r["id"] for r in _rows(client) if r["side"] == side and r["date"] == day))


# --- the reported repro: a date moved before the opening build date ------------------------

def test_moving_a_trade_before_the_opening_is_warned_on_the_preview_and_the_save(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_base)
    buy = _id_of(client, "buy", "2026-03-02")
    pv = _preview(client, buy, date="2026-01-15")
    assert "trade_before_opening" in _codes(pv), pv["issues"]
    warn = next(i for i in pv["issues"] if i["code"] == "trade_before_opening")
    assert warn["sev"] == "warn" and "期初庫存建檔日 2026-02-01" in warn["text"]
    r = client.put(f"/api/ledgers/transactions/{buy}",
                   json=_edit_body(_row(client, buy), date="2026-01-15"))
    assert r.status_code == 200, r.text
    assert "trade_before_opening" in _codes(r.json()), (
        "the correction door must report the DEF-014 finding it saved over")


def test_a_future_date_is_the_same_warning_on_the_edit_door(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_base)
    buy = _id_of(client, "buy", "2026-03-02")
    assert "future_trade_date" in _codes(_preview(client, buy, date="2099-12-31"))
    r = client.put(f"/api/ledgers/transactions/{buy}",
                   json=_edit_body(_row(client, buy), date="2099-12-31"))
    assert r.status_code == 200 and "future_trade_date" in _codes(r.json())


# --- the row is excluded from its own computation -----------------------------------------

def test_an_unchanged_row_is_not_its_own_duplicate_nor_its_own_oversell(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The old modal asked the ENTRY question, so every row was a duplicate of itself and a
    sell covered by exactly the shares it sells was 「賣超」 by its own quantity."""
    client = dashboard_client_factory(_base)
    sell = _id_of(client, "sell", "2026-04-01")
    pv = _preview(client, sell, price="611")
    assert "duplicate_trade" not in _codes(pv), pv["issues"]
    assert "sell_exceeds_holdings" not in _codes(pv), pv["issues"]
    r = client.put(f"/api/ledgers/transactions/{sell}",
                   json=_edit_body(_row(client, sell), price="611"))
    assert r.status_code == 200, r.text


def _full_exit(conn: sqlite3.Connection) -> None:
    _base(conn)
    _tx(conn, Side.SELL, "1150", "630", date(2026, 5, 6))   # sells EVERYTHING held


def test_a_full_exit_opened_unchanged_is_neither_its_own_cover_nor_its_own_duplicate(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The sharpest shape of the old entry question: a sell of the whole position, opened in
    the modal with nothing changed. Counting the stored row too, it sold its own 1,150 shares
    (held 0 → 「賣超」) and matched itself field for field (「相同交易已存在」)."""
    client = dashboard_client_factory(_full_exit)
    sell = _id_of(client, "sell", "2026-05-06")
    pv = _preview(client, sell)
    assert "sell_exceeds_holdings" not in _codes(pv), pv["issues"]
    assert "duplicate_trade" not in _codes(pv), pv["issues"]
    r = client.put(f"/api/ledgers/transactions/{sell}",
                   json=_edit_body(_row(client, sell), note="全數出清"))
    assert r.status_code == 200, r.text
    assert not {"sell_exceeds_holdings", "duplicate_trade"} & set(_codes(r.json()))
    # A real correction of the quantity (1,150 → 1,100): the row's own 1,150 no longer
    # stands in the count, so 1,100 of 1,150 held is simply covered.
    pv = _preview(client, sell, shares="1100")
    assert "sell_exceeds_holdings" not in _codes(pv), pv["issues"]
    fixed = client.put(f"/api/ledgers/transactions/{sell}",
                       json=_edit_body(_row(client, sell), shares="1100"))
    assert fixed.status_code == 200, fixed.text


def test_a_real_duplicate_is_still_found(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_base)
    buy = _id_of(client, "buy", "2026-03-02")
    # Moving the 04-01 buy onto the 03-02 buy's exact fields IS a duplicate of another row.
    later = next(r["id"] for r in _rows(client)
                 if r["side"] == "buy" and r["date"] == "2026-04-01")
    pv = _preview(client, later, date="2026-03-02", shares="100", price="600")
    assert "duplicate_trade" in _codes(pv), pv["issues"]
    assert buy != later


def test_a_same_day_row_entered_after_the_edited_sell_does_not_cover_it(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """On 04-01 the sell (id 2) books BEFORE the buy (id 3): 1,100 held then. Raising the sell
    to 1,120 is covered only by the later buy — the replay books a 賣超, so must the check."""
    client = dashboard_client_factory(_base)
    sell = _id_of(client, "sell", "2026-04-01")
    pv = _preview(client, sell, shares="1120")
    over = next((i for i in pv["issues"] if i["code"] == "sell_exceeds_holdings"), None)
    assert over is not None, pv["issues"]
    assert "1100" in over["text"] and "同日較晚登錄" in over["text"], over["text"]
    r = client.put(f"/api/ledgers/transactions/{sell}",
                   json=_edit_body(_row(client, sell), shares="1120"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "oversell"


# --- hard findings: introduced vs carried -------------------------------------------------

def _legacy_fraction(conn: sqlite3.Connection) -> None:
    _base(conn)
    _tx(conn, Side.BUY, "1.5", "600", date(2026, 5, 4))     # before the L12 whole-share rule


def test_a_legacy_hard_condition_stays_correctable_but_cannot_be_reintroduced(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_legacy_fraction)
    legacy = _id_of(client, "buy", "2026-05-04")
    ok = client.put(f"/api/ledgers/transactions/{legacy}",
                    json=_edit_body(_row(client, legacy), note="補備註"))
    assert ok.status_code == 200, ok.text
    bad = client.put(f"/api/ledgers/transactions/{legacy}",
                     json=_edit_body(_row(client, legacy), shares="2.5"))
    assert bad.status_code == 400, bad.text
    assert "shares_not_integer" in [i["code"] for i in bad.json()["error"]["issues"]]
    buy = _id_of(client, "buy", "2026-03-02")
    new = client.put(f"/api/ledgers/transactions/{buy}",
                     json=_edit_body(_row(client, buy), shares="100.5"))
    assert new.status_code == 400 and "整數" in new.json()["error"]["message"]


def _acked_oversell(conn: sqlite3.Connection) -> None:
    _base(conn)
    _tx(conn, Side.SELL, "5000", "620", date(2026, 5, 5))   # an acked 賣超 already in the ledger


def test_an_acked_oversell_edited_in_place_is_not_a_new_oversell_but_a_worse_one_is(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_acked_oversell)
    sell = _id_of(client, "sell", "2026-05-05")
    note = client.put(f"/api/ledgers/transactions/{sell}",
                      json=_edit_body(_row(client, sell), note="待釐清"))
    assert note.status_code == 200, note.text
    worse = client.put(f"/api/ledgers/transactions/{sell}",
                       json=_edit_body(_row(client, sell), shares="6000"))
    assert worse.status_code == 422 and worse.json()["error"]["code"] == "oversell"
    acked = client.put(f"/api/ledgers/transactions/{sell}",
                       json=_edit_body(_row(client, sell), shares="6000", ack_oversell=True))
    assert acked.status_code == 200, acked.text


def test_the_preview_for_an_unknown_row_is_a_404_not_an_entry_preview(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_base)
    r = client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy", "date": "2026-03-02",
        "shares": "100", "price": "600", "replaces_txn_id": 99999})
    assert r.status_code == 404
