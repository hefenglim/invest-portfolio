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
from portfolio_dash.strategy import target_weights as tw
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


def _list(value: object) -> list[dict[str, object]]:
    """Narrow a wire ``object`` field (rows' ``legs`` / ``accounts``) to a list of dicts."""
    assert isinstance(value, list)
    return value


def _summary(result: dict[str, object]) -> dict[str, object]:
    assert isinstance(result["summary"], dict)
    return result["summary"]


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


def test_zero_price_symbol_excluded_not_crash() -> None:
    """A held+targeted symbol with a degenerate 0 price is EXCLUDED, never a divide-by-zero.

    Regression: sizing raw shares as ``delta / price`` raised decimal.DivisionByZero (a 500
    on the preview / report route) when a quote came back as 0. A non-positive price is now
    treated as no usable price -> the symbol is excluded, honoring the degradation invariant.
    """
    conn = _golden()
    # AAPL is held; overwrite its current quote with 0 (halted / degenerate feed).
    upsert_prices(conn, [PriceRow(instrument="AAPL", market=Market.US,
                                  as_of=date(2026, 6, 9), close=Decimal("0"),
                                  source="test")], fetched_at=_NOW)
    conn.commit()
    result = compute_rebalance(conn, now=_NOW, reporting=Currency.TWD,
                               targets={"AAPL": Decimal("0.5"), "2330": Decimal("0.5")})
    by_sym = _rows_by_symbol(result)
    assert "AAPL" not in by_sym  # zero-priced -> no row
    excluded = _summary(result)["excluded"]
    assert isinstance(excluded, list)
    assert "AAPL" in excluded
    assert "2330" in by_sym  # the other (priced) symbol still computes
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
    assert set(summary) >= {"turnover_reporting", "total_fees_reporting", "cash_after",
                            "excluded", "over_allocated", "excluded_with_target"}
    conn.close()


def test_my_market_leg_snaps_to_100_lot() -> None:
    """The single MY leg's shares snap to a 100-unit board lot (per-leg rule)."""
    conn = _golden()
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
    result = compute_rebalance(conn, now=_NOW, reporting=Currency.TWD,
                               targets={"1155": Decimal("0.01")})
    row = _rows_by_symbol(result)["1155"]
    legs = row["legs"]
    assert isinstance(legs, list) and len(legs) == 1
    leg = legs[0]
    assert leg["account_id"] == "moomoo_my_my"
    assert Decimal(str(leg["shares"])) % Decimal("100") == 0  # board-lot rounding per leg
    assert leg["odd_lot"] is False  # odd_lot only flags TW 張 (not MY)
    conn.close()


# --- combined cross-account engine (owner ruling 2026-07-13) -----------------------------

# _dual: AAPL genuinely held in TWO accounts (schwab 30 @100 + moomoo_my_us 10 @110), plus
# 2330 (tw_broker 1000 @500) and a watch-only 2454 (registered, unheld). Current: AAPL 120
# USD, 2330 600 TWD, USD/TWD 33.  total = 600000 (2330) + 40*120*33 (AAPL) = 758400 TWD.
_TOTAL_DUAL = Decimal("758400")
_AAPL_MV = Decimal("158400")  # 40 sh * 120 USD * 33


def _dual() -> sqlite3.Connection:
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
    upsert_instrument(conn, Instrument(symbol="2454", market=Market.TW,
                                       quote_ccy=Currency.TWD, sector="Semiconductors",
                                       name="MediaTek", board="TWSE"))  # watch-only, unheld
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("1000"), price=Decimal("500"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 5))
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("30"), price=Decimal("100"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 10))
    insert_transaction(conn, account_id="moomoo_my_us", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("10"), price=Decimal("110"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 12))
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


def test_dual_combined_current_weight_on_the_row() -> None:
    """A dual-account symbol's row carries the COMBINED current weight (both accounts)."""
    conn = _dual()
    result = compute_rebalance(conn, now=_NOW, reporting=Currency.TWD,
                               targets={"AAPL": Decimal("0.40")})  # buy (up from ~0.209)
    row = _rows_by_symbol(result)["AAPL"]
    assert row["side"] == "buy"
    assert row["current_weight"] == _AAPL_MV / _TOTAL_DUAL  # combined, not one account's slice
    # constituents surfaced (both accounts, most-shares first)
    accounts = row["accounts"]
    assert isinstance(accounts, list)
    assert [a["account_id"] for a in accounts] == ["schwab", "moomoo_my_us"]
    assert [Decimal(str(a["shares"])) for a in accounts] == [Decimal("30"), Decimal("10")]
    conn.close()


def test_dual_target_equals_combined_weight_is_no_trade() -> None:
    """Target == the true COMBINED weight -> NO trade (the pre-fix bug still bought here)."""
    conn = _dual()
    result = compute_rebalance(conn, now=_NOW, reporting=Currency.TWD,
                               targets={"AAPL": _AAPL_MV / _TOTAL_DUAL})
    assert "AAPL" not in _rows_by_symbol(result)  # on target across the combined position
    conn.close()


def test_dual_buy_routes_to_most_shares_account() -> None:
    """A BUY is one leg routed to the most-shares account (schwab 30 > moomoo 10)."""
    conn = _dual()
    result = compute_rebalance(conn, now=_NOW, reporting=Currency.TWD,
                               targets={"AAPL": Decimal("0.40")})
    row = _rows_by_symbol(result)["AAPL"]
    legs = row["legs"]
    assert isinstance(legs, list) and len(legs) == 1
    assert legs[0]["account_id"] == "schwab"  # most shares
    assert legs[0]["side"] == "buy"
    conn.close()


def test_dual_target_zero_liquidates_all_accounts_greedy() -> None:
    """Target 0 -> greedy sell empties EVERY account; each leg uses its OWN fee rule set."""
    conn = _dual()
    result = compute_rebalance(conn, now=_NOW, reporting=Currency.TWD,
                               targets={"AAPL": Decimal("0")})
    row = _rows_by_symbol(result)["AAPL"]
    assert row["side"] == "sell"
    legs = row["legs"]
    assert isinstance(legs, list)
    # greedy most-shares-first: schwab (30) then moomoo_my_us (10) — both fully liquidated
    assert [lg["account_id"] for lg in legs] == ["schwab", "moomoo_my_us"]
    assert [Decimal(str(lg["shares"])) for lg in legs] == [Decimal("30"), Decimal("10")]
    assert Decimal(str(row["shares"])) == Decimal("40")  # aggregate = all shares
    assert row["new_weight"] == Decimal("0")  # combined position fully closed
    # per-account fee rule sets (fee-engine v2): schwab sell 30@120 = sec 0.07 + taf 0.01 =
    # 0.08; moomoo_us sell 10@120 = comm 0.36 + platform 0.99 + settle 0.03 + cat 0.00 +
    # sec 0.02 + taf 0.01 = 1.41 — DIFFERENT structures, proving fees bind to account not market.
    assert Decimal(str(legs[0]["fee"])) == Decimal("0.08")
    assert Decimal(str(legs[1]["fee"])) == Decimal("1.41")
    conn.close()


def test_dual_partial_sell_spans_accounts_bounded() -> None:
    """A sell larger than the biggest account spills into the next, bounded by its shares."""
    conn = _dual()
    # Keep 5 of 40 shares -> sell 35: schwab 30 (all) + moomoo 5. target = 5*120*33/758400.
    target = (Decimal("5") * Decimal("120") * Decimal("33")) / _TOTAL_DUAL
    result = compute_rebalance(conn, now=_NOW, reporting=Currency.TWD, targets={"AAPL": target})
    row = _rows_by_symbol(result)["AAPL"]
    legs = _list(row["legs"])
    assert [lg["account_id"] for lg in legs] == ["schwab", "moomoo_my_us"]
    assert Decimal(str(legs[0]["shares"])) == Decimal("30")  # schwab fully drained first
    assert Decimal(str(legs[1]["shares"])) == Decimal("5")   # remainder from moomoo, bounded
    conn.close()


def test_tw_odd_lot_flag_on_partial_lot_leg() -> None:
    """A TW leg whose shares are not a whole 1,000-share 張 sets odd_lot (display hint)."""
    conn = _dual()
    result = compute_rebalance(conn, now=_NOW, reporting=Currency.TWD,
                               targets={"2330": Decimal("0.75")})  # sell ~52 sh
    row = _rows_by_symbol(result)["2330"]
    assert row["side"] == "sell"
    legs = _list(row["legs"])
    assert len(legs) == 1
    assert Decimal(str(legs[0]["shares"])) % Decimal("1000") != 0
    assert legs[0]["odd_lot"] is True
    conn.close()


def test_over_allocated_flag() -> None:
    """Σ(submitted targets) > 1 sets summary.over_allocated (flag only, no hard block)."""
    conn = _dual()
    over = compute_rebalance(conn, now=_NOW, reporting=Currency.TWD,
                             targets={"2330": Decimal("0.8"), "AAPL": Decimal("0.5")})
    assert _summary(over)["over_allocated"] is True  # sum 1.3
    ok = compute_rebalance(conn, now=_NOW, reporting=Currency.TWD,
                           targets={"2330": Decimal("0.6"), "AAPL": Decimal("0.2")})
    assert _summary(ok)["over_allocated"] is False  # sum 0.8
    conn.close()


def test_excluded_with_target_surfaces_watch_only_stored_target() -> None:
    """A symbol with a stored 目標配置 weight but no held/priced position is surfaced."""
    conn = _dual()
    tw.save_target_weights(conn, {"2454": Decimal("0.1")}, now=_NOW)  # watch-only, unheld
    result = compute_rebalance(conn, now=_NOW, reporting=Currency.TWD,
                               targets={"2330": Decimal("0.6"), "AAPL": Decimal("0.2")})
    assert _summary(result)["excluded_with_target"] == ["2454"]  # 2330/AAPL held -> NOT here
    conn.close()
