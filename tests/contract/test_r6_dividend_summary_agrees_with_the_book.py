"""R6 (pre-merge review round 2, 2026-10-04): 股利總覽 counts exactly the dividends the BOOK
booked.

``build_dashboard``'s dividend summary (``by_year`` / ``total_by_currency`` / ``ttm_net``)
walked the dividend ledger directly, so a payout the replay REFUSES was still summed there —
one payout, three answers: the summary counted it, 總報酬 and XIRR excluded it, and the drawer
said 無法入帳. The replay refuses a dividend in three cases, and all three are left out:

* no position in that account by its date (CR-02 — ``Book.orphan_dividends``);
* the position was an OPEN declared short (a short seller pays the dividend, it is not
  received);
* the position had been EXCHANGED away (the old ticker no longer holds anything).

Each is named elsewhere on the page — the XIRR reason / the drawer for the first, the
``unbookable_dividend`` 待釐清 flag for the other two — so leaving it out of the total is the
book's answer, not a silent drop.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_corporate_action,
    insert_dividend,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import DashboardClientFactory


def _cash(conn: sqlite3.Connection, symbol: str, day: date, net: str) -> None:
    insert_dividend(conn, account_id="tw_broker", symbol=symbol, div_date=day,
                    div_type="CASH", gross=Decimal(net), withholding=Decimal("0"),
                    net=Decimal(net))


def _seed(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    for sym in ("2330", "2317", "2454", "2603", "2609"):
        upsert_instrument(conn, Instrument(symbol=sym, market=Market.TW,
                                           quote_ccy=Currency.TWD, sector="Electronics",
                                           name=sym, board="TWSE"))
    # booked: 2330 held, a 500 cash dividend reduces its cost.
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("1000"), price=Decimal("500"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 1, 5))
    _cash(conn, "2330", date(2026, 3, 1), "500")
    # refused (1): 2317 never held in this account.
    _cash(conn, "2317", date(2026, 3, 2), "300")
    # refused (2): 2454 is an open DECLARED short when its dividend lands.
    insert_transaction(conn, account_id="tw_broker", symbol="2454", side=Side.SELL,
                       quantity=Decimal("100"), price=Decimal("800"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 2, 1), short_sale=True)
    _cash(conn, "2454", date(2026, 3, 3), "200")
    # refused (3): 2603 was exchanged into 2609 before its dividend.
    insert_transaction(conn, account_id="tw_broker", symbol="2603", side=Side.BUY,
                       quantity=Decimal("100"), price=Decimal("50"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 1, 10))
    insert_corporate_action(conn, account_id="tw_broker", action_date=date(2026, 2, 1),
                            kind=CorporateActionKind.EXCHANGE, from_symbol="2603",
                            to_symbol="2609", ratio_to=Decimal("1"), ratio_from=Decimal("1"))
    _cash(conn, "2603", date(2026, 3, 4), "100")


def test_the_dividend_summary_counts_only_what_the_book_booked(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed)
    r = client.get("/api/dashboard")
    assert r.status_code == 200, r.text
    summary = r.json()["dividends"]
    assert summary["total_by_currency"] == {"TWD": "500"}, summary
    assert summary["ttm_net"] == {"TWD": "500"}, summary
    assert summary["by_year"] == [{"year": 2026, "by_currency": {"TWD": "500"}}], summary
