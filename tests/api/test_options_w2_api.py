"""Options W2 — the dashboard wire carries ``options`` (spec §7.1) and the TWR answer says the
trend excludes an open contract (R3), each only on an option ledger.

A stock ledger keeps the exact payload it had: no ``options`` key (the golden test holds the
bytes) and no ``options_note`` on the TWR response.
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
from portfolio_dash.shared.options import TREND_EXCLUDES_OPTIONS_NOTE
from tests.conftest import DashboardClientFactory

D = Decimal
_NOW = datetime(2026, 6, 10, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))
PUT = "XYZ261218P00050000"


def _stocks(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US,
                                       quote_ccy=Currency.USD, sector="Tech", name="Apple"))
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=D("10"), price=D("100"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 3, 2))
    upsert_prices(conn, [PriceRow(instrument="AAPL", market=Market.US, as_of=date(2026, 6, 9),
                                  close=D("120"), source="test")], fetched_at=_NOW)
    upsert_fx(conn, [FxRow(base=Currency.USD, quote=Currency.TWD, as_of=date(2026, 3, 1),
                           rate=D("32"), source="test")], fetched_at=_NOW)


def _with_put(conn: sqlite3.Connection) -> None:
    _stocks(conn)
    register_option_contract(conn, PUT)
    insert_transaction(conn, account_id="schwab", symbol=PUT, side=Side.SELL,
                       quantity=D("200"), price=D("2.00"), fees=D("1.30"), tax=D("0"),
                       trade_date=date(2026, 3, 2), short_sale=True)


def test_the_dashboard_wire_carries_the_block_only_with_an_option(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    plain = dashboard_client_factory(_stocks, now=_NOW).get("/api/dashboard").json()
    assert "options" not in plain
    payload = dashboard_client_factory(_with_put, now=_NOW).get("/api/dashboard").json()
    block = payload["options"]
    [row] = block["open"]
    assert (row["contract"], row["direction"], row["contracts"], row["obligation"]) == (
        "XYZ 2026-12-18 50 Put", "short", "2", "10000")
    assert row["market_value"] == "-400.00" and row["last_price"] == "2.00"
    assert "underlying_price" not in row             # XYZ is not registered: omitted (§7.1)
    assert block["market_value"] == {"by_currency": {"USD": "-400.00"}, "reporting": "-12800.00"}
    assert [h["symbol"] for h in payload["holdings"]] == ["AAPL"]
    assert payload["kpis"]["total_market_value"] == "25600.00"     # 38400 + (−12800)
    assert payload["kpis"]["principal_fx_effect"] is None
    assert block["principal_fx_note"] is not None
    assert block["note"] == TREND_EXCLUDES_OPTIONS_NOTE


def test_the_twr_answer_names_the_trend_exclusion_only_when_a_contract_was_skipped(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    plain = dashboard_client_factory(_stocks, now=_NOW).get(
        "/api/performance/twr?benchmark=sp500&window=all").json()
    assert "options_note" not in plain
    body = dashboard_client_factory(_with_put, now=_NOW).get(
        "/api/performance/twr?benchmark=sp500&window=all").json()
    assert body["options_note"] == TREND_EXCLUDES_OPTIONS_NOTE
