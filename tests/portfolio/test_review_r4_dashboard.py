"""R4 integration: the counterfactual on a real ledger, with the index actually seeded.

The pure-function pins live in ``test_review_r4_counterfactual``. This file covers the wiring
that those cannot see — that ``build_dashboard`` routes each market's flows to the right
index, converts the index into the reporting currency at its own carry-forward FX, and
compares the answer against **B** rather than A.

⚠ The spec-17 golden fixture seeds NO benchmark prices, so its payload records the honest
degradation (``available=false``, all three markets uncovered). That is the right snapshot for
a ledger-only database, but it means the golden file alone never exercises the happy path —
hence this file.
"""

import sqlite3
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.pricing.store import upsert_fx, upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

D = Decimal
NOW = datetime(2026, 6, 10, 12, 0)
BUY_DAY = date(2026, 1, 5)
TWD, USD = Currency.TWD, Currency.USD


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    create_pricing_tables(c)
    seed_accounts(c)
    yield c
    c.close()


def _daily(instrument: str, market: Market, start: date, days: int, first: str,
           step: str) -> list[PriceRow]:
    return [
        PriceRow(instrument=instrument, market=market, as_of=start + timedelta(days=i),
                 close=D(first) + D(step) * i, source="test")
        for i in range(days)
    ]


def _seed_tw_only(conn: sqlite3.Connection) -> None:
    """One TW buy, and a 0050 series that doubles over the holding period."""
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=TWD,
                                       sector="Semiconductors", name="TSMC", board="TWSE"))
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=D("1000"), price=D("500"), fees=D("0"), tax=D("0"),
                       trade_date=BUY_DAY)
    upsert_prices(conn, _daily("2330", Market.TW, BUY_DAY, 160, "500", "1"), fetched_at=NOW)
    # 0050 at 100 on the buy date, rising by 1/day -> 259 on 2026-06-13 (day 159).
    upsert_prices(conn, _daily("0050", Market.TW, BUY_DAY, 160, "100", "1"), fetched_at=NOW)


def test_the_counterfactual_buys_the_index_with_the_same_money_on_the_same_day(
    conn: sqlite3.Connection,
) -> None:
    _seed_tw_only(conn)
    data = build_dashboard(conn, now=NOW, reporting=TWD)
    b = data.benchmark
    assert b is not None and b.available, b.reason if b else "no benchmark block"

    # 500,000 in at index 100 = 5,000 units. Valued at the last close on-or-before as_of.
    assert b.net_invested == D("500000")
    assert b.uncovered_markets == [] and b.uncovered_ratio == D("0")
    last_level = D("100") + D("1") * ((NOW.date() - BUY_DAY).days)
    assert b.terminal_value == D("5000") * last_level
    assert b.benchmark_return == b.terminal_value - b.net_invested
    assert [leg.market for leg in b.by_market] == [Market.TW]
    assert b.by_market[0].label  # server-owned zh name; the web layer never names an index


def test_excess_is_measured_against_B_not_against_A(conn: sqlite3.Connection) -> None:
    """AI-D41's whole point. A applies FX to the gain only; the counterfactual spends
    reporting-currency money at trade-date rates, exactly as ``net_invested`` does. Only B is
    on the same ruler, and on a single-currency ledger the two happen to coincide — so this
    asserts the IDENTITY that defines the field, which holds in every case."""
    _seed_tw_only(conn)
    data = build_dashboard(conn, now=NOW, reporting=TWD)
    b, k = data.benchmark, data.kpis
    assert b is not None and b.excess is not None
    assert k.total_return_fx_complete is not None
    assert b.benchmark_return is not None
    assert b.excess == k.total_return_fx_complete - b.benchmark_return


def test_a_market_with_no_benchmark_degrades_the_headline_instead_of_hiding(
    conn: sqlite3.Connection,
) -> None:
    """A MY position has no index (AI-D22). Its money must still be counted as uncovered, or
    a three-market portfolio is silently compared against a two-market counterfactual."""
    _seed_tw_only(conn)
    upsert_instrument(conn, Instrument(symbol="1155", market=Market.MY,
                                       quote_ccy=Currency.MYR, sector="Banks", name="Maybank"))
    insert_transaction(conn, account_id="moomoo_my", symbol="1155", side=Side.BUY,
                       quantity=D("1000"), price=D("10"), fees=D("0"), tax=D("0"),
                       trade_date=BUY_DAY)
    upsert_prices(conn, _daily("1155", Market.MY, BUY_DAY, 160, "10", "0"), fetched_at=NOW)
    upsert_fx(conn, [FxRow(base=Currency.MYR, quote=TWD, as_of=BUY_DAY + timedelta(days=i),
                           rate=D("7"), source="test") for i in range(160)], fetched_at=NOW)

    data = build_dashboard(conn, now=NOW, reporting=TWD)
    b = data.benchmark
    assert b is not None and b.available
    assert b.uncovered_markets == ["MY"]
    # 500,000 TWD of TW flow vs 10,000 MYR x 7 = 70,000 TWD of MY flow.
    assert b.uncovered_ratio == D("70000") / D("570000")
    assert [leg.market for leg in b.by_market] == [Market.TW]


def test_a_ledger_with_no_benchmark_prices_refuses_rather_than_returning_zero(
    conn: sqlite3.Connection,
) -> None:
    """A zero would read as 「the index went nowhere」 — the opposite of 「we cannot tell」."""
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=TWD,
                                       sector="Semiconductors", name="TSMC", board="TWSE"))
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=D("1000"), price=D("500"), fees=D("0"), tax=D("0"),
                       trade_date=BUY_DAY)
    upsert_prices(conn, _daily("2330", Market.TW, BUY_DAY, 160, "500", "1"), fetched_at=NOW)

    data = build_dashboard(conn, now=NOW, reporting=TWD)
    b = data.benchmark
    assert b is not None and not b.available
    assert b.benchmark_return is None and b.excess is None
    assert b.reason and b.uncovered_ratio == D("1")


def test_an_empty_ledger_reports_no_flows_not_a_zero_excess(
    conn: sqlite3.Connection,
) -> None:
    data = build_dashboard(conn, now=NOW, reporting=TWD)
    b = data.benchmark
    assert b is not None and not b.available and b.excess is None
