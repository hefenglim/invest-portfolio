"""A synthetic option ledger for the W6 export tests (options spec §9) — no real data.

Valuation day 2026-06-10 (``NOW``). schwab holds 10 AAPL (priced 120 USD, USD/TWD 32) and:

* ``PUT``  — XYZ 2026-12-18 50 Put, sold to open 2 contracts @ 2.00 (fee 1.30): OPEN, short;
* ``CALL`` — XYZ 2026-05-15 60 Call, bought 1 @ 2.50 (fee 0.65), sold to close @ 3.00 (fee
  0.66): CLOSED, realized ``sale`` 48.69;
* ``EXP``  — ABC 2026-04-17 30 Put, sold 1 @ 1.00 (fee 0.65), expired (the §4.1 price-0 BUY):
  CLOSED, realized ``short_cover`` 99.35.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.register import register_option_contract
from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.pricing.store import upsert_fx, upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import init_golden_base

D = Decimal
NOW = datetime(2026, 6, 10, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))
PUT = "XYZ261218P00050000"
CALL = "XYZ260515C00060000"
EXP = "ABC260417P00030000"


def seed_option_ledger(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US,
                                       quote_ccy=Currency.USD, sector="Tech", name="Apple"))
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=D("10"), price=D("100"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 3, 2))
    upsert_prices(conn, [PriceRow(instrument="AAPL", market=Market.US, as_of=date(2026, 6, 9),
                                  close=D("120"), source="test")], fetched_at=NOW)
    upsert_fx(conn, [FxRow(base=Currency.USD, quote=Currency.TWD, as_of=date(2026, 3, 1),
                           rate=D("32"), source="test")], fetched_at=NOW)
    for sym in (PUT, CALL, EXP):
        register_option_contract(conn, sym)
    insert_transaction(conn, account_id="schwab", symbol=PUT, side=Side.SELL,
                       quantity=D("200"), price=D("2.00"), fees=D("1.30"), tax=D("0"),
                       trade_date=date(2026, 3, 2), short_sale=True)
    insert_transaction(conn, account_id="schwab", symbol=CALL, side=Side.BUY,
                       quantity=D("100"), price=D("2.50"), fees=D("0.65"), tax=D("0"),
                       trade_date=date(2026, 3, 3))
    insert_transaction(conn, account_id="schwab", symbol=CALL, side=Side.SELL,
                       quantity=D("100"), price=D("3.00"), fees=D("0.66"), tax=D("0"),
                       trade_date=date(2026, 4, 1))
    insert_transaction(conn, account_id="schwab", symbol=EXP, side=Side.SELL,
                       quantity=D("100"), price=D("1.00"), fees=D("0.65"), tax=D("0"),
                       trade_date=date(2026, 3, 5), short_sale=True)
    insert_transaction(conn, account_id="schwab", symbol=EXP, side=Side.BUY,
                       quantity=D("100"), price=D("0"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 4, 17), close_reason="expired")
    conn.commit()


def option_ledger() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_golden_base(conn)
    seed_option_ledger(conn)
    return conn
