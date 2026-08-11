import sqlite3
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_corporate_action,
    insert_dividend,
    insert_fx_conversion,
    insert_transaction,
    load_ledger_bundle,
    upsert_instrument,
)
from portfolio_dash.portfolio import cost_basis, dashboard, price_basis
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.portfolio.dashboard import (
    _unapplied_action_reason,
    build_dashboard,
)
from portfolio_dash.portfolio.results import UnappliedAction
from portfolio_dash.portfolio.returns import xirr_reporting
from portfolio_dash.pricing.results import DividendEvent, FxRow, PriceRead, PriceRow
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.pricing.store import (
    get_price_history,
    upsert_dividend_events,
    upsert_fx,
    upsert_prices,
)
from portfolio_dash.shared.corporate_actions import CorporateActionKind
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
    # R6: 2330 ('Semiconductors') + AAPL ('Tech') both fold into GICS Information Technology
    # at the grouping seam — 600,000 TWD (2330) + 1,200 USD @33 = 39,600 TWD (AAPL).
    assert data.allocation.by_sector == {"Information Technology": Decimal("639600")}
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


def _seed_usd_only(conn: sqlite3.Connection) -> None:
    """One schwab USD holding; FX/price seeding varies per test."""
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US, quote_ccy=USD,
                                       sector="Tech", name="Apple"))
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("10"), price=Decimal("100"),
                       fees=Decimal("0"), tax=Decimal("0"),
                       trade_date=date(2026, 1, 10))


def test_cold_start_missing_fx_degrades_blends(conn: sqlite3.Connection) -> None:
    _seed_usd_only(conn)
    upsert_prices(conn, [PriceRow(instrument="AAPL", market=Market.US,
                                  as_of=date(2026, 6, 9), close=Decimal("120"),
                                  source="test")], fetched_at=NOW)
    # No fx_rates rows at all.
    data = build_dashboard(conn, now=NOW, reporting=TWD)
    assert data.returns is None
    assert data.allocation is None
    assert data.currency_view is None
    assert data.fx is None
    assert data.kpis.total_market_value is None
    assert data.kpis.total_return is None
    assert "USD/TWD" in data.freshness.missing_fx
    # Per-position data still renders (no FX needed in quote ccy).
    assert data.holdings[0].market_value == Decimal("1200")
    assert data.holdings[0].weight is None
    assert data.kpis.xirr is None
    assert data.freshness.xirr_unavailable_reason is not None
    assert data.trend.available is False
    assert data.freshness.trend_unavailable_reason is not None


def test_no_prices_renders_at_cost_with_flags(conn: sqlite3.Connection) -> None:
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=TWD,
                                       sector="Semiconductors", name="TSMC",
                                       board="TWSE"))
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("1000"), price=Decimal("500"),
                       fees=Decimal("0"), tax=Decimal("0"),
                       trade_date=date(2026, 1, 5))
    data = build_dashboard(conn, now=NOW, reporting=TWD)
    h = data.holdings[0]
    assert h.market_value is None and h.unrealized_pnl is None
    assert h.price_stale is True and h.price_as_of is None
    assert data.freshness.missing_prices == ["2330"]
    # TWD-only: blends work via the identity rate; valued total is 0 (nothing valued).
    assert data.kpis.total_market_value == Decimal("0")
    assert data.returns is not None
    assert data.returns.by_currency[TWD].total_return == Decimal("0")
    assert data.kpis.xirr is None  # terminal value cannot be formed
    assert data.freshness.xirr_unavailable_reason is not None


def test_stale_price_used_and_flagged(conn: sqlite3.Connection) -> None:
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=TWD,
                                       sector="Semiconductors", name="TSMC",
                                       board="TWSE"))
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("1000"), price=Decimal("500"),
                       fees=Decimal("0"), tax=Decimal("0"),
                       trade_date=date(2026, 1, 5))
    upsert_prices(conn, [PriceRow(instrument="2330", market=Market.TW,
                                  as_of=date(2026, 4, 11), close=Decimal("600"),
                                  source="test")], fetched_at=NOW)  # 60 days old
    data = build_dashboard(conn, now=NOW, reporting=TWD)
    h = data.holdings[0]
    assert h.market_value == Decimal("600000")  # last-known value IS used
    assert h.price_stale is True                # ...but flagged
    assert h.price_as_of == date(2026, 4, 11)
    assert data.freshness.any_stale is True
    assert data.freshness.missing_prices == []


def test_xirr_flow_predates_fx_history(conn: sqlite3.Connection) -> None:
    _seed_usd_only(conn)
    upsert_prices(conn, [PriceRow(instrument="AAPL", market=Market.US,
                                  as_of=date(2026, 6, 9), close=Decimal("120"),
                                  source="test")], fetched_at=NOW)
    # Current FX exists, but nothing on/before the 2026-01-10 buy.
    upsert_fx(conn, [FxRow(base=USD, quote=TWD, as_of=date(2026, 6, 9),
                           rate=Decimal("33"), source="test"),
                     FxRow(base=MYR, quote=TWD, as_of=date(2026, 6, 9),
                           rate=Decimal("7"), source="test"),
                     FxRow(base=USD, quote=MYR, as_of=date(2026, 6, 9),
                           rate=Decimal("4.4"), source="test")], fetched_at=NOW)
    data = build_dashboard(conn, now=NOW, reporting=TWD)
    assert data.returns is not None          # current rates fine
    assert data.kpis.xirr is None            # historical rate missing
    reason = data.freshness.xirr_unavailable_reason
    assert reason is not None and "USD/TWD" in reason and "2026-01-10" in reason
    assert data.trend.available is False     # same missing flow-date FX


def test_fx_exposure_counts_only_settlement_ccy_holdings(
    conn: sqlite3.Connection,
) -> None:
    """FX exposure per account sums ONLY holdings quoted in that account's settlement
    (foreign) currency. Bug-proofing for the Moomoo dual-market merge: an account with
    settlement USD / funding MYR may hold BOTH USD-quoted (US) and MYR-quoted (MY)
    instruments; the USD exposure figure must count only the USD holding, never fold the
    MYR-quoted value in (that would mis-sum two currencies into one number).

    moomoo_my is settlement USD / funding MYR. Booking a MYR-quoted MY stock into it
    alongside a USD-quoted US stock reproduces the post-merge dual-market shape directly.
    schwab (settlement USD, all-USD holdings) confirms a single-market account is unchanged.
    """
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US, quote_ccy=USD,
                                       sector="Tech", name="Apple"))
    upsert_instrument(conn, Instrument(symbol="MSFT", market=Market.US, quote_ccy=USD,
                                       sector="Tech", name="Microsoft"))
    upsert_instrument(conn, Instrument(symbol="1155", market=Market.MY, quote_ccy=MYR,
                                       sector="Financials", name="Maybank"))
    # moomoo_my (settlement USD / funding MYR): one USD holding + one MYR holding.
    insert_transaction(conn, account_id="moomoo_my", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("10"), price=Decimal("100"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 10))
    insert_transaction(conn, account_id="moomoo_my", symbol="1155", side=Side.BUY,
                       quantity=Decimal("100"), price=Decimal("9"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 12))
    # MYR->USD funding conversion establishes the USD pool acquisition rate (4.4 MYR/USD).
    insert_fx_conversion(conn, account_id="moomoo_my", date=date(2026, 1, 8),
                         from_ccy=MYR, from_amount=Decimal("4400"),
                         to_ccy=USD, to_amount=Decimal("1000"))
    # schwab (settlement USD / funding TWD): single-market, all-USD holding.
    insert_transaction(conn, account_id="schwab", symbol="MSFT", side=Side.BUY,
                       quantity=Decimal("5"), price=Decimal("200"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 10))
    upsert_prices(conn, [
        PriceRow(instrument="AAPL", market=Market.US, as_of=date(2026, 6, 9),
                 close=Decimal("120"), source="test"),   # 10 * 120 = 1,200 USD
        PriceRow(instrument="MSFT", market=Market.US, as_of=date(2026, 6, 9),
                 close=Decimal("250"), source="test"),   # 5 * 250 = 1,250 USD
        PriceRow(instrument="1155", market=Market.MY, as_of=date(2026, 6, 9),
                 close=Decimal("9"), source="test"),     # 100 * 9 = 900 MYR
    ], fetched_at=NOW)
    upsert_fx(conn, [
        FxRow(base=MYR, quote=TWD, as_of=date(2026, 6, 9), rate=Decimal("7"),
              source="test"),   # moomoo_my home(MYR)->reporting(TWD) rollup rate
        FxRow(base=USD, quote=MYR, as_of=date(2026, 6, 9), rate=Decimal("4.5"),
              source="test"),   # USD->MYR current spot (foreign->home)
        FxRow(base=USD, quote=TWD, as_of=date(2026, 6, 9), rate=Decimal("32"),
              source="test"),
    ], fetched_at=NOW)

    data = build_dashboard(conn, now=NOW, reporting=TWD)
    assert data.fx is not None
    # Dual-market account: ONLY the USD holding (AAPL: 1,200 USD) counts as USD exposure.
    # The MYR-quoted 1155 (900 MYR) is excluded; the buggy sum would have been 2,100.
    moomoo = data.fx.by_account["moomoo_my"]
    assert moomoo.foreign_ccy == USD
    assert moomoo.foreign_stock_value == Decimal("1200")
    # Single-market account is unchanged: all-USD holding sums exactly to its own value.
    schwab = data.fx.by_account["schwab"]
    assert schwab.foreign_ccy == USD
    assert schwab.foreign_stock_value == Decimal("1250")


def test_dividend_ttm_net_excludes_events_older_than_365_days(
    conn: sqlite3.Connection,
) -> None:
    """ttm_net is the trailing-365-day window (display-only attribution); by_year and
    total_by_currency keep the full history. A cash dividend just outside the window is
    excluded from ttm_net yet still counts in the yearly + all-time totals; an event that
    lands exactly on the cutoff is included (inclusive lower bound)."""
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=TWD,
                                       sector="Semiconductors", name="TSMC", board="TWSE"))
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US, quote_ccy=USD,
                                       sector="Tech", name="Apple"))
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("1000"), price=Decimal("500"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2024, 1, 5))
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("10"), price=Decimal("100"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2025, 1, 1))
    # NOW = 2026-06-10 -> trailing-12-month cutoff = 2025-06-10 (inclusive).
    insert_dividend(conn, account_id="tw_broker", symbol="2330", div_date=date(2025, 1, 15),
                    div_type="CASH", gross=Decimal("3000"), withholding=Decimal("0"),
                    net=Decimal("3000"))   # older than the window -> excluded from ttm_net
    insert_dividend(conn, account_id="tw_broker", symbol="2330", div_date=date(2025, 6, 10),
                    div_type="CASH", gross=Decimal("1000"), withholding=Decimal("0"),
                    net=Decimal("1000"))   # exactly on cutoff -> included
    insert_dividend(conn, account_id="tw_broker", symbol="2330", div_date=date(2026, 3, 1),
                    div_type="CASH", gross=Decimal("5000"), withholding=Decimal("0"),
                    net=Decimal("5000"))   # within window
    insert_dividend(conn, account_id="schwab", symbol="AAPL", div_date=date(2026, 4, 1),
                    div_type="CASH", gross=Decimal("100"), withholding=Decimal("30"),
                    net=Decimal("70"))     # within window, different currency

    data = build_dashboard(conn, now=NOW, reporting=TWD)
    dv = data.dividends
    # all-time total keeps every cash dividend
    assert dv.total_by_currency == {TWD: Decimal("9000"), USD: Decimal("70")}
    # by_year splits 2025 (3000 + 1000) and 2026 (5000)
    by_year = {r.year: r.by_currency for r in dv.by_year}
    assert by_year[2025][TWD] == Decimal("4000")
    assert by_year[2026][TWD] == Decimal("5000")
    # trailing 12 months drops the 2025-01-15 event, keeps the on-cutoff 2025-06-10 one;
    # never summed across currencies (TWD and USD stay separate keys).
    assert dv.ttm_net == {TWD: Decimal("6000"), USD: Decimal("70")}


# ===================== Unapplied corporate actions (audit F-47 / F-49 / F-50) ===========


def _seed_unapplied_action(conn: sqlite3.Connection) -> None:
    """A ledger whose only problem is one corporate action the replay cannot apply.

    2330 is exchanged into 6505, which carries an open DECLARED short — E18 refuses to merge
    a long into a short position, so 2330 keeps its PRE-action 1,000 shares while every price
    in the DB is post-action. Nothing here is 賣超: ``has_oversold`` is False, so this
    exercises the new gate and not the pre-existing one.
    """
    for sym, name in (("2330", "TSMC"), ("2454", "MTK"), ("6505", "FPCC")):
        upsert_instrument(conn, Instrument(symbol=sym, market=Market.TW, quote_ccy=TWD,
                                           sector="Semiconductors", name=name,
                                           board="TWSE"))
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("1000"), price=Decimal("500"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 1, 5))
    insert_transaction(conn, account_id="tw_broker", symbol="2454", side=Side.BUY,
                       quantity=Decimal("100"), price=Decimal("300"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 1, 6))
    insert_transaction(conn, account_id="tw_broker", symbol="6505", side=Side.SELL,
                       quantity=Decimal("1"), price=Decimal("100"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 2, 2), short_sale=True)
    insert_corporate_action(conn, account_id="tw_broker", action_date=date(2026, 3, 2),
                            kind=CorporateActionKind.EXCHANGE, from_symbol="2330",
                            to_symbol="6505", ratio_to=Decimal("2"),
                            ratio_from=Decimal("1"))
    upsert_prices(conn, [
        PriceRow(instrument="2330", market=Market.TW, as_of=date(2026, 6, 9),
                 close=Decimal("600"), source="test"),
        PriceRow(instrument="2454", market=Market.TW, as_of=date(2026, 6, 9),
                 close=Decimal("356"), source="test"),
        PriceRow(instrument="6505", market=Market.TW, as_of=date(2026, 6, 9),
                 close=Decimal("100"), source="test"),
    ], fetched_at=NOW)
    upsert_fx(conn, [FxRow(base=USD, quote=TWD, as_of=date(2026, 6, 9),
                           rate=Decimal("32"), source="test")], fetched_at=NOW)


def test_an_unapplied_action_is_excluded_from_every_aggregate(
    conn: sqlite3.Connection,
) -> None:
    """AUDIT F-49, the measured leak. Before the fix, on exactly this ledger: the flagged
    2330 kept ``market_value 600,000`` at ``weight 0.944``, the KPI band reported
    ``total_market_value 635,600``, and XIRR came out ``0.5301`` with ``xirr_reason: None``
    — presented as trustworthy off a terminal value 94% composed of one poisoned row.

    Containment (owner requirement 2026-08-10): the OTHER two positions keep valuing
    normally. Only the symbol carrying the corporate action loses its numbers.
    """
    _seed_unapplied_action(conn)
    data = build_dashboard(conn, now=NOW, reporting=TWD)
    rows = {h.symbol: h for h in data.holdings}

    poisoned = rows["2330"]
    assert poisoned.unbookable_action is True
    assert poisoned.oversold is False           # NOT 賣超 — a different problem
    assert poisoned.shares == Decimal("1000")   # pre-action shares, reported honestly
    assert poisoned.market_value is None        # ...never multiplied by a post-action price
    assert poisoned.unrealized_pnl is None
    assert poisoned.weight is None
    assert poisoned.unrealized_pct is None

    # Contained: the untouched symbols are completely unaffected.
    assert rows["2454"].market_value == Decimal("35600")
    assert rows["6505"].market_value == Decimal("-100")

    # 635,500 − 600,000: the poisoned row is out of the KPI and out of the 合計 footer cell,
    # and the two still agree by construction.
    assert data.kpis.total_market_value == Decimal("35500")
    grand = next(s for s in data.holdings_subtotals
                 if s.account_id is None and s.market is None)
    assert grand.total_market_value == data.kpis.total_market_value


def test_an_unapplied_action_blanks_xirr_and_names_the_row(
    conn: sqlite3.Connection,
) -> None:
    """The XIRR gate — the one figure that legitimately blanks portfolio-wide, so its reason
    must point at the single ledger row responsible (owner requirement 2026-08-10).

    ``xirr_reporting`` multiplies ``price * h.shares`` itself rather than reading
    ``market_value``, so nulling the valuation is NOT enough: without this gate the terminal
    value silently keeps the pre-action share count.
    """
    _seed_unapplied_action(conn)
    data = build_dashboard(conn, now=NOW, reporting=TWD)
    reason = data.freshness.xirr_unavailable_reason
    assert data.kpis.xirr is None
    assert reason is not None
    assert "2330" in reason and "tw_broker" in reason and "2026-03-02" in reason
    assert "公司行動" in reason
    assert "賣超" not in reason, "must be distinguishable from the oversold reason"


def test_an_unapplied_action_blanks_xirr_even_with_no_holding_to_flag(
    conn: sqlite3.Connection,
) -> None:
    """The reason the gate reads ``Book.unapplied_actions`` and not ``any(h.unbookable_action)``.

    Here the source never existed (E1), so no holding carries a flag — yet an action in the
    ledger went unapplied and the book is still 待釐清. A holdings-based gate is blind to it
    (audit F-47 / E1).
    """
    _seed_usd_only(conn)
    upsert_instrument(conn, Instrument(symbol="GHOST", market=Market.US, quote_ccy=USD,
                                       sector="Tech", name="Ghost"))
    upsert_instrument(conn, Instrument(symbol="GHOST2", market=Market.US, quote_ccy=USD,
                                       sector="Tech", name="Ghost 2"))
    insert_corporate_action(conn, account_id="schwab", action_date=date(2026, 3, 2),
                            kind=CorporateActionKind.EXCHANGE, from_symbol="GHOST",
                            to_symbol="GHOST2", ratio_to=Decimal("1"),
                            ratio_from=Decimal("1"))
    upsert_prices(conn, [PriceRow(instrument="AAPL", market=Market.US,
                                  as_of=date(2026, 6, 9), close=Decimal("120"),
                                  source="test")], fetched_at=NOW)
    upsert_fx(conn, [FxRow(base=USD, quote=TWD, as_of=date(2026, 1, 10),
                           rate=Decimal("32"), source="test")], fetched_at=NOW)

    data = build_dashboard(conn, now=NOW, reporting=USD)
    assert not any(h.unbookable_action for h in data.holdings), "no carrier — that is the point"
    assert data.kpis.xirr is None
    reason = data.freshness.xirr_unavailable_reason
    assert reason is not None and "GHOST" in reason


def test_the_xirr_reason_caps_the_list_but_never_drops_a_row() -> None:
    """A KPI badge cannot render twenty rows; it must still account for all of them."""
    many = [
        UnappliedAction(account_id="schwab", date=date(2026, 3, i + 1),
                        kind=CorporateActionKind.SPLIT, from_symbol=f"S{i}",
                        to_symbol=f"S{i}", reason="x")
        for i in range(5)
    ]
    reason = _unapplied_action_reason(many)
    assert "5 筆" in reason and "另有 2 筆" in reason
    assert "S0" in reason and "S2" in reason and "S3" not in reason
    assert "另有" not in _unapplied_action_reason(many[:1])


def test_a_spinoff_child_gets_price_history_so_the_trend_does_not_flatten(
    conn: sqlite3.Connection,
) -> None:
    """AUDIT F-50. The child appears in NO other ledger, so a ``ledger_symbols`` built from
    transactions / dividends / openings alone loaded no prices for it — and a CORRECTLY
    booked spinoff then marked every subsequent day ``incomplete``, flattening the trend at
    the parent-only value. Measured before the fix on this exact ledger: from 2026-06-05
    onward, ``total=1000, incomplete=True``.
    """
    for sym in ("PARENT", "CHILD"):
        upsert_instrument(conn, Instrument(symbol=sym, market=Market.US, quote_ccy=USD,
                                           sector="Tech", name=sym))
    insert_transaction(conn, account_id="schwab", symbol="PARENT", side=Side.BUY,
                       quantity=Decimal("100"), price=Decimal("10"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 6, 1))
    insert_corporate_action(conn, account_id="schwab", action_date=date(2026, 6, 5),
                            kind=CorporateActionKind.SPINOFF, from_symbol="PARENT",
                            to_symbol="CHILD", ratio_to=Decimal("1"),
                            ratio_from=Decimal("2"), cost_carry=Decimal("0.2"))
    upsert_prices(conn, [
        PriceRow(instrument=sym, market=Market.US, as_of=day, close=close, source="test")
        for sym, close in (("PARENT", Decimal("10")), ("CHILD", Decimal("4")))
        for day in (date(2026, 6, 1), date(2026, 6, 5), date(2026, 6, 9))
    ], fetched_at=NOW)

    data = build_dashboard(conn, now=NOW, reporting=USD)
    # Booked cleanly: nothing went unapplied. (The XIRR reason present here is the
    # unrelated 9-day short-window withhold, so assert on the corporate-action gate only.)
    assert "公司行動" not in (data.freshness.xirr_unavailable_reason or "")
    after = [p for p in data.trend.points if p.date >= date(2026, 6, 5)]
    assert after and not any(p.incomplete for p in after)
    # 100 x 10 (the parent keeps its shares through a spinoff) + 50 x 4 (child) = 1,200.
    assert after[0].total_value == Decimal("1200")


def test_a_ledger_with_no_corporate_action_takes_exactly_the_pre_change_branches(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HEADLINE ACCEPTANCE (owner requirement 2026-08-10).

    A symbol with no corporate action must behave exactly as it did before this feature
    existed, so that a defect in the new flow is contained to the one stock that has an
    action and is cheap to fix afterwards.

    Proved STRUCTURALLY — "the new code did not execute" — rather than by equality of
    results, because code that does not run cannot drift while code that happens to compute
    an equal answer can. Three land mines are planted on the three new branches:

    * ``cost_basis._reject`` — the only writer of ``Book.unapplied_actions`` and of
      ``unbookable_action``;
    * ``dashboard._unapplied_action_reason`` — the new XIRR branch;
    * ``pnl.value_holdings``'s new disjunct, checked by asserting that no holding carries
      the flag, which makes ``h.oversold or h.unbookable_action`` identical to the
      pre-change ``h.oversold`` by construction.
    * ``price_basis.split_factor`` — W6b's read-path re-expression (§5.1(d)), reached from
      BOTH the ``price_map`` seam and the trend's per-day lookup. ``price_in`` short-circuits
      on ``index.splits_on(symbol)`` being empty, so with no action in the ledger the
      arithmetic never runs and ``price_map`` is byte-for-byte the pre-change dict.

    ``ledger_symbols`` is observed through the symbols ``get_price_history`` is actually
    asked for: with no actions the two added set comprehensions are empty, so the requested
    set must be exactly the transaction / dividend / opening symbols.
    """
    def _boom(*a: object, **k: object) -> None:
        raise AssertionError("new corporate-action code ran on an action-free ledger")

    requested: list[str] = []

    def _spy(conn_: sqlite3.Connection, instrument: str, start: date,
             end: date) -> list[PriceRead]:
        requested.append(instrument)
        return get_price_history(conn_, instrument, start, end)

    monkeypatch.setattr(cost_basis, "_reject", _boom)
    monkeypatch.setattr(dashboard, "_unapplied_action_reason", _boom)
    monkeypatch.setattr(price_basis, "split_factor", _boom)
    monkeypatch.setattr(dashboard, "get_price_history", _spy)

    _seed_full(conn)            # the rich fixture: two markets, a dividend, an FX conversion
    # A registered instrument that appears in NO ledger. Without it the ledger_symbols
    # assertion below cannot tell "the three ledgers' symbols" apart from "every registered
    # instrument", and a widened set passes unnoticed (measured: mutation 11 stayed green).
    upsert_instrument(conn, Instrument(symbol="UNUSED", market=Market.US, quote_ccy=USD,
                                       sector="Tech", name="Never traded"))
    data = build_dashboard(conn, now=NOW, reporting=TWD)

    # 1. Structure: no refusal machinery ran, and the book carries no new state.
    book = build_book(load_ledger_bundle(conn), allow_oversell=True)
    assert book.unapplied_actions == []
    assert not any(h.unbookable_action for h in data.holdings)

    # 2. ledger_symbols is unchanged: exactly the three non-action ledgers' symbols.
    assert sorted(set(requested)) == ["2330", "AAPL"]

    # 3. The values the change touches are the pre-change values (pinned from
    #    test_build_dashboard_happy_path, which predates this feature).
    rows = {h.symbol: h for h in data.holdings}
    assert rows["2330"].market_value == Decimal("600000")
    assert rows["2330"].unrealized_pnl == Decimal("105000")   # (600 - 495) x 1000
    assert rows["2330"].capital_gain == Decimal("100000")     # (600 - 500) x 1000
    assert rows["AAPL"].market_value == Decimal("1200")
    assert data.kpis.total_market_value == Decimal("639600")  # 600,000 + 1,200 x 33
    assert data.kpis.xirr is not None
    assert data.freshness.xirr_unavailable_reason is None


# --- §5.1(d) at the dashboard's price_map seam (trap #3 · W6b) ------------------------


def _seed_split_with_a_stale_price(conn: sqlite3.Connection) -> None:
    """10 AAPL bought at 100, a 20-for-1 on 6/8, and the newest stored price dated 6/5.

    The price is as traded on 6/5 (100 x pre-split terms); the book's share count on
    NOW (6/10) is already post-split. That gap is the whole artifact.
    """
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US, quote_ccy=USD,
                                       sector="Tech", name="Apple"))
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("10"), price=Decimal("100"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 1, 10))
    insert_corporate_action(conn, account_id="schwab", action_date=date(2026, 6, 8),
                            kind=CorporateActionKind.SPLIT, from_symbol="AAPL",
                            to_symbol="AAPL", ratio_to=Decimal("20"),
                            ratio_from=Decimal("1"))
    upsert_prices(conn, [PriceRow(instrument="AAPL", market=Market.US,
                                  as_of=date(2026, 6, 5), close=Decimal("120"),
                                  source="test")], fetched_at=NOW)
    upsert_fx(conn, [FxRow(base=USD, quote=TWD, as_of=date(2026, 1, 10),
                           rate=Decimal("32"), source="test")], fetched_at=NOW)


def test_a_split_re_expresses_the_price_for_valuation_and_xirr_from_one_seam(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TRAP #3 — the correction belongs at the ``price_map`` assignment, not inside
    ``value_holdings``.

    ``price_map`` is the ONE dict fed to both ``value_holdings`` and ``xirr_reporting``, and
    the latter multiplies ``price x h.shares`` itself rather than reading ``market_value``.
    Correcting only the valuation would leave the XIRR terminal value on the raw price, so
    market value and XIRR would disagree by the whole ratio — two numbers on one screen.
    The spy asserts the price XIRR actually received, which is the only way to tell the two
    placements apart from the outside.
    """
    _seed_split_with_a_stale_price(conn)
    seen: dict[str, Decimal] = {}

    def spy(*args: object, **kwargs: object) -> object:
        price_map = args[6]  # the price_map positional (see build_dashboard step 4)
        assert isinstance(price_map, dict)
        seen.update(price_map)
        return xirr_reporting(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(dashboard, "xirr_reporting", spy)
    data = build_dashboard(conn, now=NOW, reporting=USD)

    row = next(h for h in data.holdings if h.symbol == "AAPL")
    assert row.shares == Decimal("200")          # 10 x 20, re-denominated by the replay
    assert row.market_value == Decimal("1200")   # 200 x (120 / 20) — NOT 200 x 120
    assert data.kpis.total_market_value == Decimal("1200")
    # ...and XIRR was handed the SAME corrected price, from the same dict.
    assert seen == {"AAPL": Decimal("6")}


def test_without_the_reexpression_the_dashboard_inflates_by_the_ratio(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DETECTION POWER — the pre-fix behaviour on the REAL code path (F-11).

    ``price_in`` is neutered to the identity, which is exactly the statement this package
    replaced (``price_map = {sym: pr.value ...}``). Both the holding and the KPI band then
    report 20x the position's actual worth, and nothing anywhere is flagged — a wrong
    number that looks entirely right.
    """
    _seed_split_with_a_stale_price(conn)
    monkeypatch.setattr(
        dashboard, "price_in",
        lambda index, symbol, price, *, priced_on, valued_on: price,
    )
    data = build_dashboard(conn, now=NOW, reporting=USD)
    row = next(h for h in data.holdings if h.symbol == "AAPL")
    assert row.market_value == Decimal("24000")   # 200 x 120
    assert row.unbookable_action is False         # ...and unflagged, which is the danger
    assert data.freshness.xirr_unavailable_reason is None


def test_a_post_split_price_needs_no_correction_at_the_dashboard_either(
    conn: sqlite3.Connection,
) -> None:
    """Self-cancelling: once a genuine post-split row exists, ``pd >= a.date`` empties the
    window and the stored close is used exactly as it is. Nobody switches the correction
    off; it stops applying on its own."""
    _seed_split_with_a_stale_price(conn)
    upsert_prices(conn, [PriceRow(instrument="AAPL", market=Market.US,
                                  as_of=date(2026, 6, 9), close=Decimal("6"),
                                  source="test")], fetched_at=NOW)
    data = build_dashboard(conn, now=NOW, reporting=USD)
    row = next(h for h in data.holdings if h.symbol == "AAPL")
    assert row.market_value == Decimal("1200")    # 200 x 6, untouched
