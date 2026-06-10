import sqlite3
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_dividend,
    insert_fx_conversion,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.pricing.results import DividendEvent, FxRow, PriceRow
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.pricing.store import upsert_dividend_events, upsert_fx, upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

NOW = datetime(2026, 6, 10, 12, 0)
TWD = Currency.TWD
USD = Currency.USD
MYR = Currency.MYR


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    create_pricing_tables(c)
    seed_accounts(c)
    yield c
    c.close()


def _seed_full(conn: sqlite3.Connection) -> None:
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=TWD,
                                       sector="Semiconductors", name="TSMC", board="TWSE"))
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US, quote_ccy=USD,
                                       sector="Tech", name="Apple"))
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("1000"), price=Decimal("500"),
                       fees=Decimal("0"), tax=Decimal("0"),
                       trade_date=date(2026, 1, 5))
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("10"), price=Decimal("100"),
                       fees=Decimal("0"), tax=Decimal("0"),
                       trade_date=date(2026, 1, 10))
    insert_dividend(conn, account_id="tw_broker", symbol="2330",
                    div_date=date(2026, 3, 1), div_type="CASH",
                    gross=Decimal("5000"), withholding=Decimal("0"),
                    net=Decimal("5000"))
    insert_fx_conversion(conn, account_id="schwab", date=date(2026, 1, 8),
                         from_ccy=TWD, from_amount=Decimal("32000"),
                         to_ccy=USD, to_amount=Decimal("1000"))
    upsert_prices(conn, [
        PriceRow(instrument="2330", market=Market.TW, as_of=date(2026, 1, 5),
                 close=Decimal("500"), source="test"),
        PriceRow(instrument="2330", market=Market.TW, as_of=date(2026, 6, 9),
                 close=Decimal("600"), source="test"),
        PriceRow(instrument="AAPL", market=Market.US, as_of=date(2026, 1, 10),
                 close=Decimal("100"), source="test"),
        PriceRow(instrument="AAPL", market=Market.US, as_of=date(2026, 6, 9),
                 close=Decimal("120"), source="test"),
    ], fetched_at=NOW)
    upsert_fx(conn, [
        FxRow(base=USD, quote=TWD, as_of=date(2026, 1, 8), rate=Decimal("32"),
              source="test"),
        FxRow(base=USD, quote=TWD, as_of=date(2026, 6, 9), rate=Decimal("33"),
              source="test"),
        FxRow(base=MYR, quote=TWD, as_of=date(2026, 6, 9), rate=Decimal("7"),
              source="test"),
        FxRow(base=USD, quote=MYR, as_of=date(2026, 6, 9), rate=Decimal("4.4"),
              source="test"),
    ], fetched_at=NOW)
    upsert_dividend_events(conn, [
        DividendEvent(instrument="2330", market=Market.TW, ex_date=date(2026, 6, 20),
                      cash_amount=Decimal("5"), currency=TWD, source="test"),
        DividendEvent(instrument="2330", market=Market.TW, ex_date=date(2026, 5, 1),
                      cash_amount=Decimal("5"), currency=TWD, source="test"),
    ], fetched_at=NOW)


def test_build_dashboard_happy_path(conn: sqlite3.Connection) -> None:
    _seed_full(conn)
    data = build_dashboard(conn, now=NOW, reporting=TWD)

    # KPIs: 2330 mv 600k TWD; AAPL mv 1200 USD @33 -> 39600 TWD.
    assert data.kpis.total_market_value == Decimal("639600")
    # unrealized: 2330 (600-495)*1000 = 105000 (cash div reduced adjusted avg to 495);
    # AAPL (120-100)*10*33 = 6600 -> total return 111600.
    assert data.kpis.total_return == Decimal("111600")
    assert data.kpis.realized_total == Decimal("0")
    assert data.kpis.unrealized_total == Decimal("111600")
    # rate = 111600 / (500000 + 1000*33)
    assert data.kpis.total_return_rate == Decimal("111600") / Decimal("533000")
    assert data.kpis.xirr is not None
    assert data.kpis.fx_realized == Decimal("0")
    assert data.kpis.fx_unrealized == Decimal("1200")  # 1200 USD stock * (33-32)

    # Holdings enrichment.
    by_symbol = {h.symbol: h for h in data.holdings}
    tsmc = by_symbol["2330"]
    assert tsmc.name == "TSMC" and tsmc.sector == "Semiconductors"
    assert tsmc.board == "TWSE" and tsmc.account_name == "TW Broker"
    assert tsmc.market_value == Decimal("600000")
    assert tsmc.unrealized_pnl == Decimal("105000")
    assert tsmc.price_as_of == date(2026, 6, 9) and tsmc.price_stale is False
    aapl = by_symbol["AAPL"]
    assert aapl.weight == Decimal("39600") / Decimal("639600")
    weights = sum(h.weight for h in data.holdings if h.weight is not None)
    assert abs(weights - Decimal("1")) < Decimal("1e-20")

    # Sections.
    assert data.returns is not None
    assert data.returns.by_currency[TWD].unrealized == Decimal("105000")
    assert data.allocation is not None
    assert data.allocation.by_sector["Semiconductors"] == Decimal("600000")
    assert data.currency_view is not None
    assert data.currency_view.by_currency_value == {TWD: Decimal("600000"),
                                                    USD: Decimal("1200")}
    assert data.fx is not None
    schwab_fx = data.fx.by_account["schwab"]
    assert schwab_fx.avg_rate == Decimal("32") and schwab_fx.current_spot == Decimal("33")
    assert schwab_fx.foreign_cash == Decimal("0")  # 1000 converted - 1000 spent

    # Dividends + calendar.
    assert data.dividends.total_by_currency == {TWD: Decimal("5000")}
    assert data.dividends.by_year[0].year == 2026
    assert [e.ex_date for e in data.ex_dividend_calendar] == [date(2026, 6, 20)]
    assert data.ex_dividend_calendar[0].name == "TSMC"

    # Trend: first point = buy day at cost; last point = today's full value.
    assert data.trend.available is True
    assert data.trend.points[0].date == date(2026, 1, 5)
    assert data.trend.points[0].total_value == Decimal("500000")
    assert data.trend.points[0].incomplete is False
    last = data.trend.points[-1]
    assert last.date == date(2026, 6, 10)
    assert last.total_value == Decimal("639600")
    # net invested: 500000 + 1000 USD @32 - 5000 dividend = 527000
    assert last.net_invested == Decimal("527000")

    # Freshness: everything present and fresh.
    assert data.freshness.missing_prices == []
    assert data.freshness.missing_fx == []
    assert data.freshness.any_stale is False
    assert data.freshness.xirr_unavailable_reason is None
    assert data.freshness.trend_unavailable_reason is None
    assert data.insights == []
