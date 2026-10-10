"""Options W2 — the dashboard with an option ledger (spec §5, §7.1, OPT-D4 / D5 / D17 / D20).

Point-in-time figures include an open contract at its last trade price (KPI 總市值, total
return A, XIRR); the stock lists stay stock-only; the trend excludes the contract's value with
one note; 本金匯率效果 is withheld while a contract is open; a refused close withholds XIRR and
B. A ledger without an option is untouched (``test_spec17_financials`` holds the golden).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.register import register_option_contract
from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.portfolio import dashboard, returns
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.pricing.store import upsert_fx, upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.options import (
    PRINCIPAL_FX_OPTION_NOTE,
    REFUSED_CLOSE_REMEDY,
    TREND_EXCLUDES_OPTIONS_NOTE,
)

D = Decimal
NOW = datetime(2026, 6, 10, 12, 0)
TWD, USD = Currency.TWD, Currency.USD
PUT = "XYZ261218P00050000"           # XYZ 2026-12-18 50 Put
EXPIRED_PUT = "XYZ260515P00050000"   # expired before NOW


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    create_pricing_tables(c)
    seed_accounts(c)
    upsert_instrument(c, Instrument(symbol="AAPL", market=Market.US, quote_ccy=USD,
                                    sector="Tech", name="Apple"))
    upsert_instrument(c, Instrument(symbol="XYZ", market=Market.US, quote_ccy=USD,
                                    sector="Tech", name="XYZ Corp"))
    upsert_prices(c, [
        PriceRow(instrument="AAPL", market=Market.US, as_of=date(2026, 1, 10),
                 close=D("100"), source="test"),
        PriceRow(instrument="AAPL", market=Market.US, as_of=date(2026, 6, 9),
                 close=D("120"), source="test"),
        PriceRow(instrument="XYZ", market=Market.US, as_of=date(2026, 6, 9),
                 close=D("51.20"), source="test"),
    ], fetched_at=NOW)
    upsert_fx(c, [
        FxRow(base=USD, quote=TWD, as_of=date(2026, 1, 8), rate=D("32"), source="test"),
        FxRow(base=USD, quote=TWD, as_of=date(2026, 6, 9), rate=D("33"), source="test"),
    ], fetched_at=NOW)
    insert_transaction(c, account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=D("10"), price=D("100"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 1, 10))
    yield c
    c.close()


def _trade(conn: sqlite3.Connection, symbol: str, side: Side, qty: str, price: str,
           day: date, *, fees: str = "0", short: bool = False,
           close: str | None = None) -> int:
    return insert_transaction(conn, account_id="schwab", symbol=symbol, side=side,
                              quantity=D(qty), price=D(price), fees=D(fees), tax=D("0"),
                              trade_date=day, short_sale=short,
                              close_reason=close)  # type: ignore[arg-type]


def _sold_put(conn: sqlite3.Connection) -> None:
    register_option_contract(conn, PUT)
    _trade(conn, PUT, Side.SELL, "200", "2.00", date(2026, 3, 2), fees="1.30", short=True)


# --- no block without an option -----------------------------------------------------------


def test_no_options_block_on_a_stock_ledger_or_for_an_untraded_contract(
    conn: sqlite3.Connection,
) -> None:
    assert build_dashboard(conn, now=NOW, reporting=TWD).options is None
    register_option_contract(conn, PUT)
    assert build_dashboard(conn, now=NOW, reporting=TWD).options is None


def test_a_contract_traded_after_the_valuation_day_still_makes_the_block(
    conn: sqlite3.Connection,
) -> None:
    register_option_contract(conn, PUT)
    _trade(conn, PUT, Side.SELL, "100", "2", date(2026, 7, 1), short=True)
    data = build_dashboard(conn, now=NOW, reporting=TWD)
    assert data.options is not None and data.options.open == []
    assert data.kpis.total_market_value == D("39600")        # stock + 0 (nothing open yet)


# --- an open sold put ---------------------------------------------------------------------


def test_the_open_row_carries_every_section_7_1_figure(conn: sqlite3.Connection) -> None:
    _sold_put(conn)
    data = build_dashboard(conn, now=NOW, reporting=TWD)
    assert data.options is not None
    [row] = data.options.open
    assert (row.account_id, row.symbol, row.contract) == (
        "schwab", PUT, "XYZ 2026-12-18 50 Put")
    assert (row.underlying, row.expiry, row.strike, row.right, row.direction) == (
        "XYZ", date(2026, 12, 18), D("50"), "P", "short")
    assert row.days_to_expiry == 191
    assert (row.contracts, row.share_equivalents) == (D(2), D(-200))
    assert row.premium_total == D("398.70")
    assert row.average_premium == D("398.70") / D(200)
    assert (row.last_price, row.last_price_date) == (D("2.00"), date(2026, 3, 2))
    assert row.market_value == D("-400.00")
    assert row.unrealized == (D("2.00") - D("398.70") / D(200)) * D(-200)
    assert row.obligation == D("10000")
    assert (row.underlying_price, row.underlying_price_date) == (D("51.20"),
                                                                 date(2026, 6, 9))
    assert not row.past_expiry and not row.unbookable_close
    [ob] = data.options.obligation_subtotals
    assert (ob.account_id, ob.ccy, ob.amount) == ("schwab", USD, D("10000"))
    assert data.options.market_value.by_currency == {USD: D("-400.00")}
    assert data.options.market_value.reporting == D("-13200.00")


def test_stock_lists_stay_stock_only_and_kpi_adds_the_option(
    conn: sqlite3.Connection,
) -> None:
    _sold_put(conn)
    data = build_dashboard(conn, now=NOW, reporting=TWD)
    assert data.options is not None
    assert [h.symbol for h in data.holdings] == ["AAPL"]
    grand = data.holdings_subtotals[0]
    assert (grand.account_id, grand.market) == (None, None)
    assert grand.total_market_value == D("39600")
    # KPI 總市值 = the stock grand subtotal + options.market_value (OPT-D5)
    assert data.kpis.total_market_value == grand.total_market_value + D("-13200.00")
    assert sum(h.weight or D(0) for h in data.holdings) == D(1)
    assert data.allocation is not None
    assert data.allocation.by_sector == {"Information Technology": D("39600")}  # no option
    assert data.currency_view is not None
    assert data.currency_view.reporting_total_value == D("39600")
    # never quoted: not in freshness, not missing, not stale
    assert [p.symbol for p in data.freshness.prices] == ["AAPL"]
    assert data.freshness.missing_prices == []
    assert not any(p.stale for p in data.freshness.prices)
    assert data.ex_dividend_calendar == []


def test_total_return_a_includes_the_option_unrealized(conn: sqlite3.Connection) -> None:
    _sold_put(conn)
    data = build_dashboard(conn, now=NOW, reporting=TWD)
    option_unrealized = (D("2.00") - D("398.70") / D(200)) * D(-200)
    assert data.kpis.unrealized_total == (D("200") + option_unrealized) * D(33)
    assert data.options is not None
    assert data.options.unrealized.by_currency == {USD: option_unrealized}


def test_xirr_terminal_value_carries_the_open_option(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sold_put(conn)
    seen: dict[str, Any] = {}
    real = returns.xirr_reporting

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen["holdings"] = [h.symbol for h in args[3]]
        seen["prices"] = dict(args[6])
        return real(*args, **kwargs)

    monkeypatch.setattr(dashboard, "xirr_reporting", spy)
    build_dashboard(conn, now=NOW, reporting=TWD)
    assert PUT in seen["holdings"]
    assert seen["prices"][PUT] == D("2.00")


def test_principal_fx_is_withheld_while_an_option_is_open(conn: sqlite3.Connection) -> None:
    _sold_put(conn)
    data = build_dashboard(conn, now=NOW, reporting=TWD)
    assert data.options is not None
    assert data.kpis.total_return_fx_complete is not None      # B still computes
    assert data.kpis.principal_fx_effect is None                # OPT-D20
    assert data.options.principal_fx_note == PRINCIPAL_FX_OPTION_NOTE


def test_the_trend_excludes_the_option_without_marking_days_incomplete(
    conn: sqlite3.Connection,
) -> None:
    _sold_put(conn)
    data = build_dashboard(conn, now=NOW, reporting=TWD)
    assert data.options is not None
    assert data.options.trend_excludes_value
    assert data.options.note == TREND_EXCLUDES_OPTIONS_NOTE
    assert data.trend.available and not any(p.incomplete for p in data.trend.points)
    last = data.trend.points[-1]
    assert last.total_value == D("39600")            # AAPL only: the put's value is excluded


# --- a closed contract ----------------------------------------------------------------------


def test_after_the_close_principal_fx_returns_and_realized_is_split(
    conn: sqlite3.Connection,
) -> None:
    _sold_put(conn)
    _trade(conn, PUT, Side.BUY, "200", "0.50", date(2026, 4, 2), fees="1.30")
    data = build_dashboard(conn, now=NOW, reporting=TWD)
    assert data.options is not None and data.options.open == []
    assert data.options.principal_fx_note is None
    assert data.kpis.principal_fx_effect is not None
    assert data.realized.rows == [] and data.realized.by_currency == {}
    [row] = data.options.realized.rows
    assert (row.symbol, row.kind, row.close_reason, row.contract) == (
        PUT, "short_cover", None, "XYZ 2026-12-18 50 Put")
    assert row.realized == D("297.40")
    assert data.kpis.realized_total == D("297.40") * D(33)    # the KPI includes it
    [c] = data.options.realized.by_contract
    assert (c.opened, c.closed, c.close_reason, c.closed_how) == (
        date(2026, 3, 2), date(2026, 4, 2), None, "買進平倉")
    assert (c.premium_in, c.premium_out, c.realized) == (D("398.70"), D("101.30"),
                                                         D("297.40"))
    assert data.options.realized.realized_total.by_currency == {USD: D("297.40")}
    [y] = data.options.realized.by_year
    assert y.year == 2026 and y.reporting == D("297.40") * D(33)
    [u] = data.options.realized.by_underlying
    assert u.underlying == "XYZ"
    # the put was open 3/2 – 4/1, so those trend days skipped it: the note stays (R3)
    assert data.options.trend_excludes_value
    assert data.options.note == TREND_EXCLUDES_OPTIONS_NOTE


def test_an_expiry_row_chips_its_reason(conn: sqlite3.Connection) -> None:
    _sold_put(conn)
    register_option_contract(conn, EXPIRED_PUT)
    _trade(conn, EXPIRED_PUT, Side.SELL, "100", "1.00", date(2026, 3, 3), short=True)
    _trade(conn, EXPIRED_PUT, Side.BUY, "100", "0", date(2026, 5, 15), close="expired")
    data = build_dashboard(conn, now=NOW, reporting=TWD)
    assert data.options is not None
    expired = [r for r in data.options.realized.rows if r.symbol == EXPIRED_PUT]
    assert [(r.close_reason, r.realized) for r in expired] == [("expired", D("100.00"))]
    [c] = [c for c in data.options.realized.by_contract if c.symbol == EXPIRED_PUT]
    assert (c.closed, c.close_reason, c.closed_how) == (date(2026, 5, 15), "expired",
                                                        "到期作廢")


def test_past_expiry_after_a_partial_assignment(conn: sqlite3.Connection) -> None:
    register_option_contract(conn, EXPIRED_PUT)
    _trade(conn, EXPIRED_PUT, Side.SELL, "200", "1.00", date(2026, 3, 3), short=True)
    _trade(conn, EXPIRED_PUT, Side.BUY, "100", "0", date(2026, 5, 15), close="assigned")
    _trade(conn, "XYZ", Side.BUY, "100", "50", date(2026, 5, 15))
    data = build_dashboard(conn, now=NOW, reporting=TWD)
    assert data.options is not None
    [row] = data.options.open
    assert row.past_expiry and row.days_to_expiry == -26
    assert (row.direction, row.contracts) == ("short", D(1))
    assert sorted(h.symbol for h in data.holdings) == ["AAPL", "XYZ"]


# --- a refused close ------------------------------------------------------------------------


def test_a_refused_close_withholds_xirr_and_b_and_names_the_row(
    conn: sqlite3.Connection,
) -> None:
    register_option_contract(conn, PUT)
    _trade(conn, PUT, Side.SELL, "100", "2.00", date(2026, 3, 2), short=True)
    bad = _trade(conn, PUT, Side.BUY, "200", "0", date(2026, 5, 1), close="assigned")
    data = build_dashboard(conn, now=NOW, reporting=TWD)
    assert data.options is not None
    [rc] = data.options.refused_closes
    assert rc.txn_id == bad and rc.remedy == REFUSED_CLOSE_REMEDY
    assert data.kpis.xirr is None
    reason = data.freshness.xirr_unavailable_reason or ""
    assert "XYZ 2026-12-18 50 Put" in reason and "{account:schwab}" in reason
    assert data.kpis.total_return_fx_complete is None
    assert "選擇權" in (data.kpis.fx_complete_reason or "")
    [row] = data.options.open
    assert row.unbookable_close and row.market_value is None and row.unrealized is None
    assert data.trend.points[-1].incomplete


def test_the_fx_pool_exposure_includes_the_open_option(conn: sqlite3.Connection) -> None:
    """OPT-D4: the FX pool's foreign stock value is a point-in-time figure — stocks + the open
    contract at its last trade (a sold put is a negative exposure)."""
    from portfolio_dash.data_ingestion.store import insert_fx_conversion

    insert_fx_conversion(conn, account_id="schwab", date=date(2026, 1, 8), from_ccy=TWD,
                         from_amount=D("32000"), to_ccy=USD, to_amount=D("1000"))
    _sold_put(conn)
    data = build_dashboard(conn, now=NOW, reporting=TWD)
    assert data.fx is not None
    assert data.fx.by_account["schwab"].foreign_stock_value == D("1200") + D("-400.00")


def test_an_oversold_contract_is_flagged_not_read_as_a_sold_put(
    conn: sqlite3.Connection,
) -> None:
    """An undeclared sell beyond the long lot is a 賣超, not a sold contract: no obligation, no
    value, flagged on the open row (a contract has no holdings row to badge), XIRR withheld."""
    register_option_contract(conn, PUT)
    _trade(conn, PUT, Side.BUY, "100", "2.00", date(2026, 3, 2))
    _trade(conn, PUT, Side.SELL, "200", "2.50", date(2026, 4, 2))
    data = build_dashboard(conn, now=NOW, reporting=TWD)
    assert data.options is not None
    [row] = data.options.open
    assert row.oversold and row.obligation is None and row.market_value is None
    assert data.kpis.xirr is None and "賣超" in (data.freshness.xirr_unavailable_reason or "")
