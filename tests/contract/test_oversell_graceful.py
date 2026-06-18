"""An acked oversell writes a sell exceeding holdings; the dashboard must DEGRADE
gracefully (never 500), surfacing the position as 賣超/oversold with 待釐清 (null) value
rather than computing a bogus short P&L (decided 2026-06-18, human sign-off: lightweight
— never crash + label, NOT full short-position accounting). The valid holdings still
compute; the oversold position is excluded from the portfolio aggregates.
"""

import sqlite3
from datetime import date
from decimal import Decimal

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.pricing.store import upsert_fx, upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW, DashboardClientFactory

_D = Decimal


def _seed_oversold(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Semiconductors", name="TSMC", board="TWSE"))
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                                       sector="Tech", name="Apple"))
    # 2330: buy 1000, then SELL 1500 (> held) -> an oversold ledger (acked-oversell result).
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=_D("1000"), price=_D("500"), fees=_D("0"), tax=_D("0"),
                       trade_date=date(2026, 1, 5))
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.SELL,
                       quantity=_D("1500"), price=_D("600"), fees=_D("0"), tax=_D("0"),
                       trade_date=date(2026, 4, 1))
    # AAPL: a normal, valid holding that must still compute.
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=_D("10"), price=_D("100"), fees=_D("0"), tax=_D("0"),
                       trade_date=date(2026, 1, 10))
    upsert_prices(conn, [
        PriceRow(instrument="2330", market=Market.TW, as_of=date(2026, 6, 9),
                 close=_D("600"), source="test"),
        PriceRow(instrument="AAPL", market=Market.US, as_of=date(2026, 6, 9),
                 close=_D("120"), source="test"),
    ], fetched_at=GOLDEN_NOW)
    upsert_fx(conn, [
        FxRow(base=Currency.USD, quote=Currency.TWD, as_of=date(2026, 6, 9),
              rate=_D("32"), source="test"),
    ], fetched_at=GOLDEN_NOW)


def test_oversold_dashboard_degrades_not_500(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed_oversold)
    r = client.get("/api/dashboard")
    assert r.status_code == 200  # must NOT 500 on an oversold ledger
    body = r.json()
    by_sym = {h["symbol"]: h for h in body["holdings"]}

    # 2330 is surfaced as an oversold (negative) position with 待釐清 (null) value.
    over = by_sym["2330"]
    assert over["oversold"] is True
    assert Decimal(over["shares"]) == Decimal("-500")
    assert over["market_value"] is None
    assert over["unrealized_pnl"] is None
    assert over["capital_gain"] is None
    assert over["weight"] is None

    # The valid holding still computes normally.
    aapl = by_sym["AAPL"]
    assert aapl["oversold"] is False
    assert Decimal(aapl["market_value"]) == Decimal("120") * Decimal("10")  # 1,200 USD

    # Portfolio aggregates EXCLUDE the oversold position (total = AAPL only, in TWD).
    assert Decimal(body["kpis"]["total_market_value"]) == Decimal("1200") * Decimal("32")
    # XIRR all-or-nothing: an oversold ledger -> None with an honest reason.
    assert body["kpis"]["xirr"] is None
    assert body["freshness"]["xirr_unavailable_reason"] is not None
