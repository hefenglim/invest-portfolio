"""Unit tests for the rebalance preview core (spec 03 §3.3).

compute_rebalance is compute-only: it reuses the REAL fee/tax engine (compute_fees) and
the SAME current spot rates as the dashboard (RateResolver), and never writes to any
ledger table. DBs are seeded via real write paths (mirroring tests/conftest.py's golden
seed) so the holding cost basis / valuation is the one build_dashboard produces.

Honest degradation: a target symbol with no current price is EXCLUDED (never faked).
Integer shares; MY-market trades are rounded to 100-unit board lots.
"""

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.pricing.store import upsert_fx, upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.strategy.rebalance import compute_rebalance

_NOW = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))


def _golden() -> sqlite3.Connection:
    """In-memory DB seeded via real write paths: the golden two-symbol scenario.

    2330 in tw_broker (1000 sh @500, current 600 TWD, MV 600000, weight ~0.938);
    AAPL in schwab (10 sh @100, current 120 USD, MV 1200 USD = 39600 TWD, weight ~0.062).
    Total reporting MV ~639600 TWD. USD/TWD = 33.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    bootstrap_db(conn)
    create_pricing_tables(conn)
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW,
                                       quote_ccy=Currency.TWD, sector="Semiconductors",
                                       name="TSMC", board="TWSE"))
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US,
                                       quote_ccy=Currency.USD, sector="Tech", name="Apple"))
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("1000"), price=Decimal("500"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 5))
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("10"), price=Decimal("100"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 10))
    upsert_prices(conn, [
        PriceRow(instrument="2330", market=Market.TW, as_of=date(2026, 6, 9),
                 close=Decimal("600"), source="test"),
        PriceRow(instrument="AAPL", market=Market.US, as_of=date(2026, 6, 9),
                 close=Decimal("120"), source="test"),
    ], fetched_at=_NOW)
    upsert_fx(conn, [FxRow(base=Currency.USD, quote=Currency.TWD, as_of=date(2026, 6, 9),
                           rate=Decimal("33"), source="test")], fetched_at=_NOW)
    conn.commit()
    return conn


def _rows_by_symbol(result: dict[str, object]) -> dict[str, dict[str, object]]:
    rows = result["rows"]
    assert isinstance(rows, list)
    return {r["symbol"]: r for r in rows}


def test_two_symbol_target_sides_and_currencies() -> None:
    """{2330:0.30, AAPL:0.70}: 2330 sells (was ~0.938), AAPL buys; integer shares + ccy."""
    conn = _golden()
    result = compute_rebalance(conn, now=_NOW, reporting=Currency.TWD,
                               targets={"2330": Decimal("0.30"), "AAPL": Decimal("0.70")})
    by_sym = _rows_by_symbol(result)
    assert set(by_sym) == {"2330", "AAPL"}

    sell = by_sym["2330"]
    assert sell["side"] == "sell"
    assert sell["ccy"] == "TWD"
    shares_2330 = Decimal(str(sell["shares"]))
    assert shares_2330 > 0
    assert shares_2330 == shares_2330.to_integral_value()  # integer shares

    buy = by_sym["AAPL"]
    assert buy["side"] == "buy"
    assert buy["ccy"] == "USD"
    shares_aapl = Decimal(str(buy["shares"]))
    assert shares_aapl > 0
    assert shares_aapl == shares_aapl.to_integral_value()

    summary = result["summary"]
    assert isinstance(summary, dict)
    for key in ("turnover_reporting", "total_fees_reporting", "cash_after", "excluded"):
        assert key in summary
    assert summary["excluded"] == []
    conn.close()


def test_missing_price_symbol_is_excluded() -> None:
    """A target referencing an unknown / unpriced symbol -> summary.excluded, not in rows."""
    conn = _golden()
    result = compute_rebalance(conn, now=_NOW, reporting=Currency.TWD,
                               targets={"NOPRICE": Decimal("0.5")})
    by_sym = _rows_by_symbol(result)
    assert "NOPRICE" not in by_sym
    summary = result["summary"]
    assert isinstance(summary, dict)
    assert "NOPRICE" in summary["excluded"]
    conn.close()


def test_my_market_rounds_to_100_lot() -> None:
    """An MY-market holding's traded shares snap to a 100-unit board lot."""
    conn = _golden()
    # Add an MY holding: 5000 units @1.00 MYR, current 1.50 MYR; MYR/TWD = 7.
    upsert_instrument(conn, Instrument(symbol="1155", market=Market.MY,
                                       quote_ccy=Currency.MYR, sector="Banks",
                                       name="Maybank", board="Main"))
    insert_transaction(conn, account_id="moomoo_my_my", symbol="1155", side=Side.BUY,
                       quantity=Decimal("5000"), price=Decimal("1.00"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 2, 1))
    upsert_prices(conn, [PriceRow(instrument="1155", market=Market.MY,
                                  as_of=date(2026, 6, 9), close=Decimal("1.500"),
                                  source="test")], fetched_at=_NOW)
    upsert_fx(conn, [FxRow(base=Currency.MYR, quote=Currency.TWD, as_of=date(2026, 6, 9),
                           rate=Decimal("7"), source="test")], fetched_at=_NOW)
    conn.commit()

    # Target 1155 at a small weight so the trade is a non-trivial sell that must round.
    result = compute_rebalance(conn, now=_NOW, reporting=Currency.TWD,
                               targets={"1155": Decimal("0.01")})
    by_sym = _rows_by_symbol(result)
    assert "1155" in by_sym
    shares = Decimal(str(by_sym["1155"]["shares"]))
    assert shares % Decimal("100") == 0  # board-lot rounding
    assert by_sym["1155"]["ccy"] == "MYR"
    conn.close()


def test_summary_keys_present() -> None:
    conn = _golden()
    result = compute_rebalance(conn, now=_NOW, reporting=Currency.TWD,
                               targets={"2330": Decimal("0.50")})
    summary = result["summary"]
    assert isinstance(summary, dict)
    assert set(summary) >= {"turnover_reporting", "total_fees_reporting",
                            "cash_after", "excluded"}
    conn.close()
