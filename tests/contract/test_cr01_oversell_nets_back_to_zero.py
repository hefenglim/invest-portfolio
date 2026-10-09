"""CR-01 / CR-11 (pre-merge review, 2026-10-04): a 賣超 that later nets back to EXACTLY zero
shares must stay visible — to every correction door, to the dashboard and to the drawer.

``build_book`` drops every position whose signed net is 0, and the sticky ``ever_oversold``
flag lived only on that dropped ``Holding``. So an oversell followed by a buy of the same size
was invisible to ``replay_guard._oversold_shares`` (it read ``book.holdings`` only): measured on
the reviewer's probe — buy 1,000 (01-05), sell 1,000 (02-01), buy 1,000 (03-01); deleting the
first buy answered 200 with no question, the +100,000 realized row vanished, and no 賣超 / 待釐清
appeared anywhere. The root fix records every 賣超 position on the BOOK
(``Book.oversold_positions``), the channel ``unapplied_actions`` / ``refused_dividends`` already
use for "the flag dies with its carrier".

Every case drives the real HTTP door and asserts the response and the database.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_transaction,
    load_ledger_bundle,
    upsert_instrument,
)
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW, DashboardClientFactory, init_golden_base

_TXN_HEADER = "account,symbol,side,date,shares,price\n"


def _instruments(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    for sym, name in (("2330", "TSMC"), ("2454", "MediaTek")):
        upsert_instrument(conn, Instrument(symbol=sym, market=Market.TW,
                                           quote_ccy=Currency.TWD, sector="Semiconductors",
                                           name=name, board="TWSE"))
    upsert_prices(conn, [
        PriceRow(instrument=s, market=Market.TW, as_of=date(2026, 6, 9),
                 close=Decimal("600"), source="test") for s in ("2330", "2454")
    ], fetched_at=GOLDEN_NOW)


def _tx(conn: sqlite3.Connection, side: Side, qty: str, price: str, day: date,
        symbol: str = "2330") -> int:
    return insert_transaction(conn, account_id="tw_broker", symbol=symbol, side=side,
                              quantity=Decimal(qty), price=Decimal(price), fees=Decimal("0"),
                              tax=Decimal("0"), trade_date=day)


def _probe(ids: dict[str, int]) -> Any:
    """The reviewer's ledger: the sell is covered only by the first buy, and the later buy
    of the same size nets the position back to exactly zero once that buy is gone."""
    def seed(conn: sqlite3.Connection) -> None:
        _instruments(conn)
        ids["b1"] = _tx(conn, Side.BUY, "1000", "500", date(2026, 1, 5))
        ids["sell"] = _tx(conn, Side.SELL, "1000", "600", date(2026, 2, 1))
        ids["b2"] = _tx(conn, Side.BUY, "1000", "550", date(2026, 3, 1))
    return seed


# ------------------------------------------------------------------ the calculation core


def test_the_book_records_an_oversold_position_that_netted_back_to_zero() -> None:
    """CR-11's root: the replay keeps the 賣超 on the BOOK even when the holding is dropped."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_golden_base(conn)
    _instruments(conn)
    _tx(conn, Side.SELL, "1000", "600", date(2026, 2, 1))
    _tx(conn, Side.BUY, "1000", "550", date(2026, 3, 1))
    book = build_book(load_ledger_bundle(conn), allow_oversell=True)
    assert book.holdings == []                       # 0 shares: nothing held, nothing valued
    (flat,) = book.oversold_positions
    assert (flat.account_id, flat.symbol, flat.shares) == ("tw_broker", "2330", Decimal("0"))
    assert flat.oversold_on == date(2026, 2, 1)
    assert (flat.oversold_sold, flat.oversold_held) == (Decimal("1000"), Decimal("0"))
    conn.close()


# ------------------------------------------------------------------ the correction doors


def test_deleting_the_buy_asks_first_even_when_the_position_nets_to_zero(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    ids: dict[str, int] = {}
    client = dashboard_client_factory(_probe(ids))
    r = client.delete(f"/api/ledgers/transactions/{ids['b1']}")
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "oversell"
    assert "2026-02-01 的 2330 賣出 1000 股，超過當日持股 0 股" in err["message"], err

    ok = client.delete(f"/api/ledgers/transactions/{ids['b1']}?ack_oversell=true")
    assert ok.status_code == 200, ok.text


def test_rekeying_the_buy_asks_first_even_when_the_position_nets_to_zero(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The edit door: moving the covering buy to another symbol strands the same sell."""
    ids: dict[str, int] = {}
    client = dashboard_client_factory(_probe(ids))
    r = client.put(f"/api/ledgers/transactions/{ids['b1']}", json={
        "account_id": "tw_broker", "symbol": "2454", "side": "buy", "date": "2026-01-05",
        "shares": "1000", "price": "500", "fee": "0", "tax": "0",
        "fee_overridden": True, "tax_overridden": True})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "oversell"
    assert "2026-02-01 的 2330 賣出 1000 股" in r.json()["error"]["message"]


def test_a_batch_undo_asks_first_even_when_the_position_nets_to_zero(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """DEF-049's import-batch undo runs the same guard: the batch held the covering buy."""
    def seed(conn: sqlite3.Connection) -> None:
        _instruments(conn)
    client = dashboard_client_factory(seed)
    r = client.post("/api/import/commit", json={
        "kind": "transactions",
        "csv_text": _TXN_HEADER + "tw_broker,2330,BUY,2026-01-05,1000,500\n",
        "ack_warnings": True, "source_name": "t.csv"})
    assert r.status_code == 200, r.text
    batch = r.json()["import_batch_id"]
    for body in ("tw_broker,2330,SELL,2026-02-01,1000,600\n",
                 "tw_broker,2330,BUY,2026-03-01,1000,550\n"):
        assert client.post("/api/import/commit", json={
            "kind": "transactions", "csv_text": _TXN_HEADER + body,
            "ack_warnings": True}).status_code == 200

    r = client.delete(f"/api/import/batches/{batch}")
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "oversell"
    assert err["issues"][0]["date"] == "2026-02-01" and err["issues"][0]["held"] == "0", err


def test_deleting_a_split_asks_first_even_when_the_position_nets_to_zero(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The corporate-action door: buy 100, split 10:1 → 1,000, sell 1,000, buy 900. Without the
    split the sell oversells by 900 and the later buy of 900 nets the position to exactly 0."""
    def seed(conn: sqlite3.Connection) -> None:
        _instruments(conn)
        _tx(conn, Side.BUY, "100", "5000", date(2026, 1, 5))
    client = dashboard_client_factory(seed)
    r = client.post("/api/ledgers/corporate-actions", json={
        "account_id": "tw_broker", "date": "2026-01-10", "kind": "SPLIT",
        "from_symbol": "2330", "to_symbol": "2330", "ratio_to": "10", "ratio_from": "1",
        "ack_warnings": True})
    assert r.status_code == 201, r.text
    rows = client.get("/api/ledgers/corporate-actions", params={"limit": 500}).json()["rows"]
    action_id = max(int(a["id"]) for a in rows)
    for body in ("tw_broker,2330,SELL,2026-02-01,1000,600\n",
                 "tw_broker,2330,BUY,2026-03-01,900,550\n"):
        assert client.post("/api/import/commit", json={
            "kind": "transactions", "csv_text": _TXN_HEADER + body,
            "ack_warnings": True}).status_code == 200

    r = client.delete(f"/api/ledgers/corporate-actions/{action_id}")
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "oversell"
    assert err["issues"][0]["sold"] == "1000" and err["issues"][0]["held"] == "100", err


def test_editing_a_split_asks_first_even_when_the_position_nets_to_zero(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The corporate-action EDIT door: buy 100, split 10:1 → 1,000, sell 1,000, buy 500. Edited
    to 5:1 the split leaves 500, the sell oversells by 500, and the buy of 500 nets to 0."""
    def seed(conn: sqlite3.Connection) -> None:
        _instruments(conn)
        _tx(conn, Side.BUY, "100", "5000", date(2026, 1, 5))
    client = dashboard_client_factory(seed)
    split = {"account_id": "tw_broker", "date": "2026-01-10", "kind": "SPLIT",
             "from_symbol": "2330", "to_symbol": "2330", "ratio_to": "10", "ratio_from": "1",
             "ack_warnings": True}
    assert client.post("/api/ledgers/corporate-actions", json=split).status_code == 201
    rows = client.get("/api/ledgers/corporate-actions", params={"limit": 500}).json()["rows"]
    action_id = max(int(a["id"]) for a in rows)
    for body in ("tw_broker,2330,SELL,2026-02-01,1000,600\n",
                 "tw_broker,2330,BUY,2026-03-01,500,550\n"):
        assert client.post("/api/import/commit", json={
            "kind": "transactions", "csv_text": _TXN_HEADER + body,
            "ack_warnings": True}).status_code == 200

    r = client.put(f"/api/ledgers/corporate-actions/{action_id}",
                   json={**split, "ratio_to": "5"})
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "oversell"
    assert err["issues"][0]["sold"] == "1000" and err["issues"][0]["held"] == "500", err


# ------------------------------------------------------------------ what the owner then sees


def test_after_the_ack_the_dashboard_and_the_drawer_name_the_flat_oversell(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The acknowledged 賣超 does not vanish with its zero-share holding: XIRR and the
    FX-complete figure are withheld with a reason naming the position, the trend marks the
    days from the oversell on as incomplete, and the drawer carries the cause line."""
    ids: dict[str, int] = {}
    client = dashboard_client_factory(_probe(ids))
    assert client.delete(
        f"/api/ledgers/transactions/{ids['b1']}?ack_oversell=true").status_code == 200

    dash = client.get("/api/dashboard")
    assert dash.status_code == 200, dash.text
    body = dash.json()
    assert not any(h["symbol"] == "2330" for h in body["holdings"])   # 0 shares held
    assert body["kpis"]["xirr"] is None
    reason = body["freshness"]["xirr_unavailable_reason"]
    for part in ("賣超", "2330", "2026-02-01", "{account:tw_broker}"):
        assert part in reason, (part, reason)
    assert "賣超" in (body["kpis"]["fx_complete_reason"] or ""), body["kpis"]
    points = body["trend"]["points"]
    assert points and points[-1]["incomplete"] is True
    assert all(p["incomplete"] for p in points if p["date"] >= "2026-02-01")

    detail = client.get("/api/symbol/2330/detail")
    assert detail.status_code == 200, detail.text
    (line,) = detail.json()["ledger_issues"]["oversold_flat"]
    assert line["account_id"] == "tw_broker" and line["date"] == "2026-02-01"
    assert "賣出 1000 股" in line["reason"] and "超過當日持股 0 股" in line["reason"], line
