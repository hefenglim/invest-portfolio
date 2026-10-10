"""Options W5 — the symbol drawer's wire for a contract and for its underlying (spec §6.2).

A contract has no ``holdings`` row (OPT-D5: holdings are stock-only), so before W5 its drawer
read a book count of 0 and printed ⚠ 對帳不一致 under every open contract, listed its trades as
bare 買／賣 in share-equivalents, and showed no realized row at all. The drawer now reads the
dashboard's ONE ``options`` block: the footer's book count is the signed share-equivalents of
``options.open``, every trade row carries its §6.1 side and 口數, and the realized rows say how
each contract closed.

Invariant 1: on a ledger with no option the drawer payload carries none of this — no
``options`` key, no option keys on an activity row.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from portfolio_dash.data_ingestion.register import register_option_contract
from portfolio_dash.data_ingestion.store import insert_transaction
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.options import REFUSED_CLOSE_REMEDY
from tests.conftest import DashboardClientFactory, _seed_golden

D = Decimal
_NOW = datetime(2026, 10, 9, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))
PUT = "AAPL261218P00110000"        # open, sold 2
CALL = "AAPL260918C00140000"       # bought 1, expired worthless


def _options(conn: sqlite3.Connection) -> None:
    _seed_golden(conn)
    for sym in (PUT, CALL):
        register_option_contract(conn, sym)
    insert_transaction(conn, account_id="schwab", symbol=PUT, side=Side.SELL,
                       quantity=D("200"), price=D("3.20"), fees=D("1.30"), tax=D("0"),
                       trade_date=date(2026, 9, 1), short_sale=True)
    insert_transaction(conn, account_id="schwab", symbol=CALL, side=Side.BUY,
                       quantity=D("100"), price=D("1.00"), fees=D("0.65"), tax=D("0"),
                       trade_date=date(2026, 8, 3))
    insert_transaction(conn, account_id="schwab", symbol=CALL, side=Side.SELL,
                       quantity=D("100"), price=D("0"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 9, 18), close_reason="expired")
    conn.commit()


def test_a_stock_ledger_drawer_carries_no_option_key(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    body = dashboard_client_factory(_seed_golden, now=_NOW).get(
        "/api/symbol/AAPL/detail").json()
    assert "options" not in body
    assert body["activity"], "the golden AAPL has a buy — the guard would be blind without it"
    for row in body["activity"]:
        assert not {"contracts", "option_side", "close_reason", "refused_close"} & set(row)


def test_an_open_contract_reconciles_against_the_option_block(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    body = dashboard_client_factory(_options, now=_NOW).get(f"/api/symbol/{PUT}/detail").json()
    opt = body["options"]
    assert opt["is_contract"] is True
    assert opt["contract"] == {"name": "AAPL 2026-12-18 110 Put", "underlying": "AAPL",
                               "expiry": "2026-12-18", "strike": "110", "right": "P",
                               "right_label": "賣權", "multiplier": 100}
    assert opt["whatif_note"] == "選擇權不提供試算"
    assert opt["unit"] == "股當量"
    assert opt["remedy"] == REFUSED_CLOSE_REMEDY and opt["refused_closes"] == []
    [row] = opt["open"]
    assert (row["direction_label"], row["contracts"], row["share_equivalents"]) == (
        "賣方", "2", "-200")
    [trade] = body["activity"]
    assert (trade["contracts"], trade["option_side"], trade["refused_close"]) == (
        "2", "賣出開倉", False)
    total = body["activity_reconcile"]["total"]
    assert (total["net_shares"], total["book_shares"], total["balances"]) == ("-200", "-200", True)
    assert body["activity_reconcile"]["by_account"]["schwab"]["balances"] is True
    assert body["price_history"]["note"].startswith("選擇權合約不抓取報價")


def test_an_expired_contract_lists_its_reason(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    body = dashboard_client_factory(_options, now=_NOW).get(f"/api/symbol/{CALL}/detail").json()
    sides = [(r["side"], r["option_side"], r["close_reason_label"]) for r in body["activity"]]
    assert sides == [("buy", "買進開倉", None), ("sell", "到期作廢", "到期作廢")]
    [rz] = body["realized_rows"]
    assert (rz["close_reason"], rz["close_reason_label"], rz["closed_how"], rz["realized"]) == (
        "expired", "到期作廢", "到期作廢", "-100.65")
    assert body["activity_reconcile"]["total"]["balances"] is True
    assert body["options"]["open"] == []


def test_the_underlying_names_its_open_contracts(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    body = dashboard_client_factory(_options, now=_NOW).get("/api/symbol/AAPL/detail").json()
    opt = body["options"]
    assert opt["is_contract"] is False
    assert [r["text"] for r in opt["open"]] == ["2 口 2026-12-18 110 Put（賣方）"]
    # The stock's own rows are untouched by the option walk.
    for row in body["activity"]:
        assert "option_side" not in row
