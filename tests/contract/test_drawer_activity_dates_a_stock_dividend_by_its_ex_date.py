"""Round 2 (b), CR-05's class on a display surface: the drawer's 交易明細 places a 配股 where the
replay books it — its ex-date.

``api/routers/symbol.py``'s ``activity`` list is "the ONE list 交易明細 renders", sorted by the
replay's own ``(date, EventPriority)`` so it cannot drift from the replay it displays. Its
dividend rows were keyed and labelled by the PAYMENT date while ``build_book`` books a STOCK
dividend with a known ex-date ON the ex-date (``Dividend.effective_date``, R6). A sale between
the two dates — legal, because the 配股 shares already count — was listed BEFORE the shares it
sold, so the list read as an oversell over a correct book.

Pinned: the 配股 row sits at, and reads, its effective date; a DRIP (paid and reinvested on
the payment date) and a 配股 without an ex-date keep the payment date.
"""

import sqlite3
from datetime import date
from decimal import Decimal

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_dividend,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW, DashboardClientFactory

D = Decimal


def _seed(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    for sym in ("2330", "2317"):
        upsert_instrument(conn, Instrument(symbol=sym, market=Market.TW, quote_ccy=Currency.TWD,
                                           sector="Semiconductors", name=sym, board="TWSE"))
        insert_transaction(conn, account_id="tw_broker", symbol=sym, side=Side.BUY,
                           quantity=D("1000"), price=D("500"), fees=D("0"), tax=D("0"),
                           trade_date=date(2026, 1, 5))
        upsert_prices(conn, [PriceRow(instrument=sym, market=Market.TW,
                                      as_of=date(2026, 6, 9), close=D("600"),
                                      source="test")], fetched_at=GOLDEN_NOW)
    # 2330: a 配股 ex 03-10 / paid 04-05, and a sale of 1,100 on 03-20 that needs it.
    insert_dividend(conn, account_id="tw_broker", symbol="2330", div_date=date(2026, 4, 5),
                    div_type="STOCK", gross=D("0"), withholding=D("0"), net=D("0"),
                    reinvest_shares=D("100"), ex_date=date(2026, 3, 10))
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.SELL,
                       quantity=D("1100"), price=D("600"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 3, 20))
    # 2317: a 配股 with NO ex-date counts from its payment date, exactly as before.
    insert_dividend(conn, account_id="tw_broker", symbol="2317", div_date=date(2026, 4, 5),
                    div_type="STOCK", gross=D("0"), withholding=D("0"), net=D("0"),
                    reinvest_shares=D("100"))
    insert_transaction(conn, account_id="tw_broker", symbol="2317", side=Side.SELL,
                       quantity=D("100"), price=D("600"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 3, 20))
    conn.commit()


def test_the_stock_dividend_is_listed_at_its_ex_date_before_the_sale_it_covers(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed)
    activity = client.get("/api/symbol/2330/detail").json()["activity"]
    # pre-fix: buy 01-05 → sell 03-20 → stock 04-05 (the sale listed before its shares)
    assert [(a["side"], a["date"]) for a in activity] == [
        ("buy", "2026-01-05"), ("stock", "2026-03-10"), ("sell", "2026-03-20")]


def test_a_stock_dividend_without_an_ex_date_keeps_its_payment_date(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed)
    activity = client.get("/api/symbol/2317/detail").json()["activity"]
    assert [(a["side"], a["date"]) for a in activity] == [
        ("buy", "2026-01-05"), ("sell", "2026-03-20"), ("stock", "2026-04-05")]
