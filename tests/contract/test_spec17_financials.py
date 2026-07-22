"""spec-17 §17.2 — multi-stock financial golden verification (the regression anchor).

A rich, fully hand-computable scenario seeded through the REAL write paths
(`upsert_instrument` / `insert_transaction` / `insert_dividend` / `insert_fx_conversion`
/ `upsert_prices` / `upsert_fx`), driven through `GET /api/dashboard`, and checked
against INDEPENDENT first-principles oracles derived from `rules/domain-ledger.md` —
NOT by re-calling the calc core. Coverage:

- weighted-average cost across multiple buys (2330)
- TW cash dividend -> adjusted-cost reduction; original cost never overwritten (2330)
- partial-sell realized P&L = proceeds_net - adjusted_avg x sold (0056)
- 配股 STOCK dividend -> shares up, cost flat, average drops (2603)
- missing-price degradation -> market/unrealized null, stale, weight null; XIRR
  all-or-nothing on a missing held price (00919)
- US DRIP -> 30% withholding, net reinvested as $0-cost shares, average drops,
  dividend folded in exactly once (AAPL)
- age-based stale price flagged but still valued (MSFT)
- MY cash dividend (net received) + 3-dp price fidelity (1155.KL @ 9.875)
- cross-currency reporting blend at current spot; currencies NEVER raw-summed
- FX gain/loss is an ATTRIBUTION of the reporting total, never added on top
  (CLAUDE.md invariant #6 — no double counting)

Money magnitudes are compared as ``Decimal`` (so Decimal scale / trailing-zero noise
from exact arithmetic never causes spurious failures); a few fields keep exact-string
assertions where the wire form itself matters (3-dp price fidelity, money-is-a-string).

`mock-data.js` (the original design mock) was a hand-authored, internally inconsistent
dataset retired under decision (B); per spec-17 §17.6.1 the regression oracle is the
proven calc core captured as a frozen snapshot, backed by the independent assertions
below.
"""

import json
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_dividend,
    insert_fx_conversion,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.pricing.store import upsert_fx, upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW, DashboardClientFactory

_D = Decimal


def _f(value: Decimal) -> str:
    """The canonical wire form a Decimal serializes to (shared/wire.decimal_str)."""
    return format(value, "f")


def eqd(actual: object, expected: Decimal) -> None:
    """Assert a wire money field (a Decimal string) equals *expected* numerically.

    Compares as Decimal so 28800.0 == 28800 (trailing-zero scale noise from exact
    Decimal arithmetic is not a contract difference); also asserts the wire type is a
    string (money is NEVER a JSON number — CLAUDE.md / stack.md)."""
    assert isinstance(actual, str), f"money must be a string, got {actual!r}"
    assert Decimal(actual) == expected, f"{actual} != {expected}"


# --- the scenario ----------------------------------------------------------------
# Frozen now = 2026-06-11 14:30 +08:00 (GOLDEN_NOW); current spot dated 2026-06-09.
# Current spot rates: USD/TWD = 32, MYR/TWD = 7, USD/MYR = 4.40.


def seed_full(conn: sqlite3.Connection) -> None:
    """Seed the rich multi-account / multi-currency scenario via real write paths."""
    seed_accounts(conn)

    # Instruments (board "" == US / unresolved board).
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Semiconductors", name="TSMC", board="TWSE"))
    upsert_instrument(conn, Instrument(symbol="0056", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="ETF", name="元大高股息", board="TWSE", is_etf=True))
    upsert_instrument(conn, Instrument(symbol="2603", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Shipping", name="長榮", board="TWSE"))
    upsert_instrument(conn, Instrument(symbol="00919", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="ETF", name="群益台灣精選高息", board="TWSE",
                                       is_etf=True))
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                                       sector="Tech", name="Apple"))
    upsert_instrument(conn, Instrument(symbol="MSFT", market=Market.US, quote_ccy=Currency.USD,
                                       sector="Tech", name="Microsoft"))
    upsert_instrument(conn, Instrument(symbol="NVDA", market=Market.US, quote_ccy=Currency.USD,
                                       sector="Tech", name="NVIDIA"))
    upsert_instrument(conn, Instrument(symbol="1155.KL", market=Market.MY, quote_ccy=Currency.MYR,
                                       sector="Financials", name="Maybank", board=".KL"))

    # --- transactions ---
    # 2330: two buys -> weighted average 510 over 2000 shares.
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=_D("1000"), price=_D("500"), fees=_D("0"), tax=_D("0"),
                       trade_date=date(2026, 1, 5))
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=_D("1000"), price=_D("520"), fees=_D("0"), tax=_D("0"),
                       trade_date=date(2026, 2, 5))
    # 0056: buy 10000 @ 36, then sell 2000 @ 40 -> realized.
    insert_transaction(conn, account_id="tw_broker", symbol="0056", side=Side.BUY,
                       quantity=_D("10000"), price=_D("36"), fees=_D("0"), tax=_D("0"),
                       trade_date=date(2026, 1, 6))
    insert_transaction(conn, account_id="tw_broker", symbol="0056", side=Side.SELL,
                       quantity=_D("2000"), price=_D("40"), fees=_D("0"), tax=_D("0"),
                       trade_date=date(2026, 4, 10))
    # 2603: buy 1000 @ 100 (then 配股 below).
    insert_transaction(conn, account_id="tw_broker", symbol="2603", side=Side.BUY,
                       quantity=_D("1000"), price=_D("100"), fees=_D("0"), tax=_D("0"),
                       trade_date=date(2026, 1, 7))
    # 00919: buy 5000 @ 23 (no price stored -> missing-price degradation).
    insert_transaction(conn, account_id="tw_broker", symbol="00919", side=Side.BUY,
                       quantity=_D("5000"), price=_D("23"), fees=_D("0"), tax=_D("0"),
                       trade_date=date(2026, 1, 8))
    # AAPL (Schwab): buy 100 @ 251 (then DRIP below -> 100.4 sh, average a clean 250).
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=_D("100"), price=_D("251"), fees=_D("0"), tax=_D("0"),
                       trade_date=date(2026, 1, 10))
    # MSFT (Schwab): buy 10 @ 400 (stale price below).
    insert_transaction(conn, account_id="schwab", symbol="MSFT", side=Side.BUY,
                       quantity=_D("10"), price=_D("400"), fees=_D("0"), tax=_D("0"),
                       trade_date=date(2026, 1, 12))
    # 1155.KL (Moomoo MY): buy 1000 @ 9 (then MY cash dividend below).
    insert_transaction(conn, account_id="moomoo_my", symbol="1155.KL", side=Side.BUY,
                       quantity=_D("1000"), price=_D("9"), fees=_D("0"), tax=_D("0"),
                       trade_date=date(2026, 2, 1))
    # NVDA (Moomoo MY US): buy 25 @ 118.
    insert_transaction(conn, account_id="moomoo_my", symbol="NVDA", side=Side.BUY,
                       quantity=_D("25"), price=_D("118"), fees=_D("0"), tax=_D("0"),
                       trade_date=date(2026, 2, 12))

    # --- dividends ---
    # 2330 TW cash: reduce adjusted cost by 20,000 (original kept intact).
    insert_dividend(conn, account_id="tw_broker", symbol="2330", div_date=date(2026, 3, 1),
                    div_type="CASH", gross=_D("20000"), withholding=_D("0"), net=_D("20000"))
    # 2603 配股: +100 shares, no cash, no cost change.
    insert_dividend(conn, account_id="tw_broker", symbol="2603", div_date=date(2026, 3, 15),
                    div_type="STOCK", gross=_D("0"), withholding=_D("0"), net=_D("0"),
                    reinvest_shares=_D("100"))
    # AAPL US DRIP: gross 100, 30% withholding, net 70 reinvested -> +0.4 $0-cost shares.
    insert_dividend(conn, account_id="schwab", symbol="AAPL", div_date=date(2026, 3, 20),
                    div_type="DRIP", gross=_D("100"), withholding=_D("30"), net=_D("70"),
                    reinvest_shares=_D("0.4"), reinvest_price=_D("175"))
    # 1155.KL MY cash: net 200 received -> reduce adjusted cost.
    insert_dividend(conn, account_id="moomoo_my", symbol="1155.KL", div_date=date(2026, 4, 5),
                    div_type="CASH", gross=_D("200"), withholding=_D("0"), net=_D("200"))

    # --- FX conversions (currency-exchange ledger) ---
    # Schwab USD pool anchored in TWD: acquire 10,000 USD @ 31, later reconvert 2,000 USD.
    insert_fx_conversion(conn, account_id="schwab", date=date(2026, 1, 8),
                         from_ccy=Currency.TWD, from_amount=_D("310000"),
                         to_ccy=Currency.USD, to_amount=_D("10000"))
    insert_fx_conversion(conn, account_id="schwab", date=date(2026, 5, 1),
                         from_ccy=Currency.USD, from_amount=_D("2000"),
                         to_ccy=Currency.TWD, to_amount=_D("64000"))
    # Moomoo USD pool anchored in MYR: acquire 2,950 USD @ 4.50.
    insert_fx_conversion(conn, account_id="moomoo_my", date=date(2026, 2, 10),
                         from_ccy=Currency.MYR, from_amount=_D("13275"),
                         to_ccy=Currency.USD, to_amount=_D("2950"))

    # --- prices (close, as_of) ---
    upsert_prices(conn, [
        PriceRow(instrument="2330", market=Market.TW, as_of=date(2026, 6, 9),
                 close=_D("600"), source="test"),
        PriceRow(instrument="0056", market=Market.TW, as_of=date(2026, 6, 9),
                 close=_D("38"), source="test"),
        PriceRow(instrument="2603", market=Market.TW, as_of=date(2026, 6, 9),
                 close=_D("110"), source="test"),
        # 00919: intentionally NO price row -> missing-price degradation.
        PriceRow(instrument="AAPL", market=Market.US, as_of=date(2026, 6, 9),
                 close=_D("300"), source="test"),
        # MSFT: as_of 2026-06-06 -> (06-11 - 06-06) = 5 days > 4 -> stale.
        PriceRow(instrument="MSFT", market=Market.US, as_of=date(2026, 6, 6),
                 close=_D("450"), source="test"),
        PriceRow(instrument="NVDA", market=Market.US, as_of=date(2026, 6, 9),
                 close=_D("170"), source="test"),
        # 1155.KL: a genuine 3-dp Bursa price (sub-RM10 tick) — fidelity must survive.
        PriceRow(instrument="1155.KL", market=Market.MY, as_of=date(2026, 6, 9),
                 close=_D("9.875"), source="test"),
    ], fetched_at=GOLDEN_NOW)

    # --- FX rates (current spot dated 2026-06-09 + trade-date anchors) ---
    upsert_fx(conn, [
        FxRow(base=Currency.USD, quote=Currency.TWD, as_of=date(2026, 1, 8),
              rate=_D("31"), source="test"),
        FxRow(base=Currency.USD, quote=Currency.TWD, as_of=date(2026, 2, 12),
              rate=_D("31.5"), source="test"),
        FxRow(base=Currency.USD, quote=Currency.TWD, as_of=date(2026, 6, 9),
              rate=_D("32"), source="test"),
        FxRow(base=Currency.MYR, quote=Currency.TWD, as_of=date(2026, 2, 1),
              rate=_D("6.8"), source="test"),
        FxRow(base=Currency.MYR, quote=Currency.TWD, as_of=date(2026, 6, 9),
              rate=_D("7"), source="test"),
        FxRow(base=Currency.USD, quote=Currency.MYR, as_of=date(2026, 2, 10),
              rate=_D("4.5"), source="test"),
        FxRow(base=Currency.USD, quote=Currency.MYR, as_of=date(2026, 6, 9),
              rate=_D("4.4"), source="test"),
    ], fetched_at=GOLDEN_NOW)


def _dashboard(factory: DashboardClientFactory) -> dict[str, Any]:
    client: TestClient = factory(seed_full)
    r = client.get("/api/dashboard")
    assert r.status_code == 200
    body: dict[str, Any] = r.json()
    return body


def _by_symbol(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {h["symbol"]: h for h in body["holdings"]}


# --- per-holding cost basis + P&L (first-principles oracles) ----------------------


def test_weighted_average_cost_and_tw_cash_dividend_2330(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    h = _by_symbol(_dashboard(dashboard_client_factory))["2330"]
    # 2 buys: (1000@500)+(1000@520) = 1,020,000 over 2000 -> original_avg 510.
    eqd(h["shares"], _D("2000"))
    eqd(h["original_cost_total"], _D("1020000"))
    eqd(h["original_avg"], _D("510"))
    # TW cash dividend 20,000 reduces ADJUSTED cost only; original untouched.
    eqd(h["adjusted_cost_total"], _D("1000000"))
    eqd(h["adjusted_avg"], _D("500"))
    eqd(h["dividend_portion"], _D("20000"))
    assert h["payback_ratio"] == _f(_D("20000") / _D("1020000"))
    # Valuation at 600: market 1,200,000; unrealized vs adjusted; capital_gain vs original.
    eqd(h["market_price"], _D("600"))
    eqd(h["market_value"], _D("600") * _D("2000"))            # 1,200,000
    eqd(h["unrealized_pnl"], (_D("600") - _D("500")) * _D("2000"))   # 200,000
    eqd(h["capital_gain"], (_D("600") - _D("510")) * _D("2000"))     # 180,000
    # Identity: unrealized = capital_gain + dividend_portion (dividend counted once).
    assert _D(h["unrealized_pnl"]) == _D(h["capital_gain"]) + _D(h["dividend_portion"])
    assert h["price_stale"] is False
    assert h["price_as_of"] == "2026-06-09"


def test_partial_sell_realized_pnl_0056(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    body = _dashboard(dashboard_client_factory)
    h = _by_symbol(body)["0056"]
    # Sold 2000 of 10000 -> 8000 remain at unchanged average 36 (no dividend).
    eqd(h["shares"], _D("8000"))
    eqd(h["original_avg"], _D("36"))
    eqd(h["adjusted_avg"], _D("36"))
    eqd(h["dividend_portion"], _D("0"))
    eqd(h["unrealized_pnl"], (_D("38") - _D("36")) * _D("8000"))  # 16,000

    rows = {(r["symbol"], r["account_id"]): r for r in body["realized"]["rows"]}
    r = rows[("0056", "tw_broker")]
    eqd(r["shares_sold"], _D("2000"))
    eqd(r["proceeds_net"], _D("2000") * _D("40"))                 # 80,000
    eqd(r["original_cost_removed"], _D("360000") * (_D("2000") / _D("10000")))  # 72,000
    eqd(r["adjusted_cost_removed"], _D("72000"))
    # realized = proceeds_net - adjusted_cost_removed = 80,000 - 72,000 = 8,000.
    eqd(r["realized"], _D("8000"))
    eqd(body["realized"]["by_currency"]["TWD"], _D("8000"))


def test_stock_dividend_配股_2603(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    h = _by_symbol(_dashboard(dashboard_client_factory))["2603"]
    # 配股 +100 shares, no cash, no cost change -> shares 1100, cost still 100,000.
    eqd(h["shares"], _D("1100"))
    eqd(h["original_cost_total"], _D("100000"))
    eqd(h["adjusted_cost_total"], _D("100000"))
    eqd(h["dividend_portion"], _D("0"))            # stock dividend does not fold into cost
    # average dropped: 100,000 / 1100 (computed on read, never stored rounded).
    assert h["original_avg"] == _f(_D("100000") / _D("1100"))
    eqd(h["market_value"], _D("110") * _D("1100"))     # 121,000
    eqd(h["unrealized_pnl"], (_D("110") - _D("100000") / _D("1100")) * _D("1100"))  # 21,000


def test_us_drip_zero_cost_reinvest_aapl(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    body = _dashboard(dashboard_client_factory)
    h = _by_symbol(body)["AAPL"]
    # DRIP: +0.4 shares at $0 cost -> shares 100.4, cost still 25,100, average 250.
    eqd(h["shares"], _D("100.4"))
    eqd(h["original_cost_total"], _D("25100"))
    eqd(h["adjusted_cost_total"], _D("25100"))     # DRIP does NOT reduce adjusted cost
    eqd(h["dividend_portion"], _D("0"))            # not folded in (counted via $0-cost shares)
    eqd(h["adjusted_avg"], _D("250"))              # 25,100 / 100.4 -> exactly 250
    eqd(h["market_value"], _D("300") * _D("100.4"))    # 30,120
    eqd(h["unrealized_pnl"], (_D("300") - _D("250")) * _D("100.4"))  # 5,020
    # capital_gain == unrealized for a DRIP holding (dividend_portion 0).
    assert _D(h["capital_gain"]) == _D(h["unrealized_pnl"])
    # The DRIP NET (70 USD), withholding applied, surfaces ONCE in the dividend summary.
    eqd(body["dividends"]["total_by_currency"]["USD"], _D("70"))


def test_my_cash_dividend_and_three_dp_price_1155(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    body = _dashboard(dashboard_client_factory)
    h = _by_symbol(body)["1155.KL"]
    # MY cash dividend net 200 -> reduce adjusted cost.
    eqd(h["original_cost_total"], _D("9000"))
    eqd(h["adjusted_cost_total"], _D("8800"))
    eqd(h["adjusted_avg"], _D("8.8"))
    eqd(h["dividend_portion"], _D("200"))
    # 3-dp price fidelity: the exact wire string is preserved (NOT truncated to 2 dp).
    assert h["market_price"] == "9.875"
    eqd(h["market_value"], _D("9.875") * _D("1000"))     # 9,875
    eqd(h["unrealized_pnl"], (_D("9.875") - _D("8.8")) * _D("1000"))   # 1,075
    eqd(h["capital_gain"], (_D("9.875") - _D("9")) * _D("1000"))       # 875
    eqd(body["dividends"]["total_by_currency"]["MYR"], _D("200"))


def test_missing_price_degradation_00919(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    body = _dashboard(dashboard_client_factory)
    h = _by_symbol(body)["00919"]
    # Held (shares > 0) but unpriced: cost basis present, valuation honestly null.
    eqd(h["shares"], _D("5000"))
    eqd(h["original_cost_total"], _D("115000"))
    assert h["market_price"] is None
    assert h["market_value"] is None
    assert h["unrealized_pnl"] is None
    assert h["capital_gain"] is None
    assert h["price_stale"] is True
    assert h["price_as_of"] is None
    assert h["weight"] is None
    assert "00919" in body["freshness"]["missing_prices"]
    assert body["freshness"]["any_stale"] is True


def test_stale_price_flagged_but_valued_msft(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    h = _by_symbol(_dashboard(dashboard_client_factory))["MSFT"]
    # Age-stale (as_of 06-06, > 4 days) -> flagged stale but STILL valued.
    assert h["price_stale"] is True
    assert h["price_as_of"] == "2026-06-06"
    eqd(h["market_value"], _D("450") * _D("10"))      # 4,500
    eqd(h["unrealized_pnl"], (_D("450") - _D("400")) * _D("10"))   # 500


def test_us_board_unresolved_blank(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    # US holdings carry board "" (unresolved/none) — passes through verbatim.
    by_sym = _by_symbol(_dashboard(dashboard_client_factory))
    for sym in ("AAPL", "MSFT", "NVDA"):
        assert by_sym[sym]["board"] == ""


# --- portfolio-level reporting blend + no-double-count invariants -----------------


def test_reporting_blend_and_currency_view(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    body = _dashboard(dashboard_client_factory)
    cv = body["currency_view"]
    # Per-currency market value (priced holdings only; 00919 excluded from TWD).
    eqd(cv["by_currency_value"]["TWD"], _D("1625000"))   # 1.2M + 304k + 121k
    eqd(cv["by_currency_value"]["USD"], _D("30120") + _D("4500") + _D("4250"))  # 38,870
    eqd(cv["by_currency_value"]["MYR"], _D("9.875") * _D("1000"))  # 9,875
    # Reporting total = TWD + USD*32 + MYR*7, each currency converted ONCE at spot.
    expected_total = _D("1625000") + _D("38870") * _D("32") + _D("9875") * _D("7")
    eqd(cv["reporting_total_value"], expected_total)             # 2,937,965
    eqd(body["kpis"]["total_market_value"], expected_total)


def test_kpis_total_return_excludes_fx_double_count(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    body = _dashboard(dashboard_client_factory)
    k = body["kpis"]
    # realized: only the TWD 0056 sell -> 8,000.
    eqd(k["realized_total"], _D("8000"))
    # unrealized reporting = 237,000(TWD) + 6,820(USD)*32 + 1,075(MYR)*7.
    expected_unreal = _D("237000") + _D("6820") * _D("32") + _D("1075") * _D("7")
    eqd(k["unrealized_total"], expected_unreal)                  # 462,765
    expected_total_return = _D("8000") + expected_unreal         # 470,765
    eqd(k["total_return"], expected_total_return)
    # INVARIANT #6: total_return == realized + unrealized. FX gain/loss is an
    # attribution breakdown of this figure — NEVER added on top.
    assert _D(k["total_return"]) == _D(k["realized_total"]) + _D(k["unrealized_total"])
    # FX KPIs are present (a decomposition), but do not change total_return.
    assert "fx_realized" in k
    assert "fx_unrealized" in k
    # Realized FX is independently hand-computable: the Schwab USD pool acquired
    # 10,000 USD @ 31 (TWD-anchored); reconverting 2,000 USD for 64,000 TWD realizes
    # 64,000 - 2,000*31 = 2,000 TWD. It is reported as attribution, not added above.
    eqd(k["fx_realized"], _D("64000") - _D("2000") * _D("31"))   # 2,000


def test_returns_by_currency_never_cross_summed(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    by_ccy = _dashboard(dashboard_client_factory)["returns"]["by_currency"]
    # TWD: realized 8,000 + unrealized 237,000; gross = all TWD buys = 1,595,000.
    twd = by_ccy["TWD"]
    eqd(twd["realized"], _D("8000"))
    eqd(twd["unrealized"], _D("237000"))
    eqd(twd["total_return"], _D("245000"))
    eqd(twd["gross_invested"], _D("1595000"))
    assert twd["rate"] == _f(_D("245000") / _D("1595000"))
    # USD: unrealized 6,820; gross = 25,100 + 4,000 + 2,950 = 32,050.
    usd = by_ccy["USD"]
    eqd(usd["realized"], _D("0"))
    eqd(usd["unrealized"], _D("6820"))
    eqd(usd["gross_invested"], _D("32050"))
    # MYR: unrealized 1,075; gross 9,000.
    myr = by_ccy["MYR"]
    eqd(myr["unrealized"], _D("1075"))
    eqd(myr["gross_invested"], _D("9000"))
    # Each currency stands alone — there is no combined raw sum key mixing them.
    assert set(by_ccy) == {"TWD", "USD", "MYR"}


def test_sector_allocation_reporting(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    alloc = _dashboard(dashboard_client_factory)["allocation"]["by_sector"]
    # R6 (GICS): the seed's 'Semiconductors' (2330) and 'Tech' (AAPL/MSFT/NVDA) BOTH fold into
    # Information Technology, and 'Shipping' (2603) folds into Industrials, at the donut grouping
    # seam. Information Technology = 2330 (1,200,000 TWD) + AAPL+MSFT+NVDA (38,870 USD * 32 =
    # 1,243,840 TWD) = 2,443,840 TWD — the exact sum of the two former slices.
    eqd(alloc["Information Technology"], _D("1200000") + _D("38870") * _D("32"))
    # Industrials = 2603 長榮 (former Shipping slice) = 121,000 TWD (unchanged value).
    eqd(alloc["Industrials"], _D("121000"))
    # ETF = 0056 only (00919 unpriced -> excluded). 304,000 TWD (unchanged).
    eqd(alloc["ETF"], _D("304000"))
    # Financials = 1155.KL = 9,875 MYR * 7 = 69,125 TWD (unchanged).
    eqd(alloc["Financials"], _D("9875") * _D("7"))
    # The folded-away FU-D31 keys no longer appear as their own slices.
    assert not ({"Semiconductors", "Shipping", "Technology"} & set(alloc))


def test_xirr_all_or_nothing_on_missing_price(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    body = _dashboard(dashboard_client_factory)
    # 00919 (held) has no current price -> XIRR cannot form a terminal value -> None,
    # with an honest reason (all-or-nothing, never a fabricated number).
    assert body["kpis"]["xirr"] is None
    assert body["freshness"]["xirr_unavailable_reason"] is not None


# --- holdings subtotals (filter re-aggregation; Wave A3) ---------------------------
# The 合計 footer + filtered CSV/report select these server-computed cells; each is a
# regrouping of the SAME per-holding reporting values that feed kpis, so first-principles
# oracles reuse the same magnitudes verified above (spot USD/TWD=32, MYR/TWD=7).


def _subtotal(body: dict[str, Any], account: str | None, market: str | None) -> dict[str, Any]:
    hits = [
        s for s in body["holdings_subtotals"]
        if s["account_id"] == account and s["market"] == market
    ]
    assert len(hits) == 1, f"expected exactly one cell for ({account}, {market}), got {hits}"
    cell: dict[str, Any] = hits[0]
    return cell


def test_holdings_subtotals_grand_equals_kpis(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The grand cell (account=None, market=None) reproduces the whole-portfolio KPI
    exactly — total_market_value BYTE-identical (same combined_view summation), unrealized
    numerically identical (re-aggregation of the same per-holding values)."""
    body = _dashboard(dashboard_client_factory)
    grand = _subtotal(body, None, None)
    # total_market_value: identical terms in identical order -> byte-exact string match.
    assert grand["total_market_value"] == body["kpis"]["total_market_value"]
    eqd(grand["total_market_value"], _D("2937965"))
    # unrealized_total: numerically equal (trailing-zero scale noise is not a contract diff).
    eqd(grand["unrealized_total"], _D(body["kpis"]["unrealized_total"]))
    eqd(grand["unrealized_total"], _D("462765"))


def test_holdings_subtotals_per_account(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    body = _dashboard(dashboard_client_factory)
    # tw_broker (TW only): 2330 1,200,000 + 0056 304,000 + 2603 121,000 (00919 unpriced ->
    # EXCLUDED, exactly as the KPI excludes it). ur = 200,000 + 16,000 + 21,000.
    tw = _subtotal(body, "tw_broker", None)
    eqd(tw["total_market_value"], _D("1625000"))
    eqd(tw["unrealized_total"], _D("237000"))
    # schwab (US only): AAPL 30,120 + MSFT 4,500 USD, at spot 32. ur (5,020+500)*32.
    schwab = _subtotal(body, "schwab", None)
    eqd(schwab["total_market_value"], (_D("30120") + _D("4500")) * _D("32"))
    eqd(schwab["unrealized_total"], (_D("5020") + _D("500")) * _D("32"))
    # moomoo_my (US + MY): NVDA 4,250 USD * 32 + 1155.KL 9,875 MYR * 7.
    moomoo = _subtotal(body, "moomoo_my", None)
    eqd(moomoo["total_market_value"], _D("4250") * _D("32") + _D("9875") * _D("7"))
    eqd(moomoo["unrealized_total"], _D("1300") * _D("32") + _D("1075") * _D("7"))


def test_holdings_subtotals_per_market_multi_currency(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """Per-market cells blend currencies at spot: US spans schwab (USD) + moomoo_my (USD),
    MY is MYR — each converted through the shared FX helper, never raw-summed."""
    body = _dashboard(dashboard_client_factory)
    # TW = tw_broker only.
    eqd(_subtotal(body, None, "TW")["total_market_value"], _D("1625000"))
    # US = (AAPL 30,120 + MSFT 4,500 + NVDA 4,250) USD * 32 = 1,243,840 TWD.
    us = _subtotal(body, None, "US")
    eqd(us["total_market_value"], (_D("30120") + _D("4500") + _D("4250")) * _D("32"))
    eqd(us["unrealized_total"], (_D("5020") + _D("500") + _D("1300")) * _D("32"))
    # MY = 1155.KL 9,875 MYR * 7 = 69,125 TWD.
    my = _subtotal(body, None, "MY")
    eqd(my["total_market_value"], _D("9875") * _D("7"))
    eqd(my["unrealized_total"], _D("1075") * _D("7"))


def test_holdings_subtotals_per_cell_and_dual_market_account(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """Per (account, market) cells split the dual-market moomoo_my account into its US and
    MY halves — the finest filter combo the dashboard chips can select."""
    body = _dashboard(dashboard_client_factory)
    # moomoo_my US = NVDA only; moomoo_my MY = 1155.KL only.
    eqd(_subtotal(body, "moomoo_my", "US")["total_market_value"], _D("4250") * _D("32"))
    eqd(_subtotal(body, "moomoo_my", "US")["unrealized_total"], _D("1300") * _D("32"))
    eqd(_subtotal(body, "moomoo_my", "MY")["total_market_value"], _D("9875") * _D("7"))
    eqd(_subtotal(body, "moomoo_my", "MY")["unrealized_total"], _D("1075") * _D("7"))
    # schwab US == the schwab account cell (schwab holds only US).
    eqd(
        _subtotal(body, "schwab", "US")["total_market_value"],
        (_D("30120") + _D("4500")) * _D("32"),
    )
    # A combo with no holdings (schwab has no MY) emits NO cell -> the frontend zero-fallbacks.
    assert not any(
        s["account_id"] == "schwab" and s["market"] == "MY"
        for s in body["holdings_subtotals"]
    )


def test_holdings_subtotals_grand_sums_to_the_parts(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """Adding the per-account (market=None) cells reproduces the grand cell — an internal
    consistency check on the re-aggregation (Decimal-exact at these magnitudes)."""
    body = _dashboard(dashboard_client_factory)
    per_account = [
        s for s in body["holdings_subtotals"]
        if s["account_id"] is not None and s["market"] is None
    ]
    total_mv = sum((_D(s["total_market_value"]) for s in per_account), _D("0"))
    total_ur = sum((_D(s["unrealized_total"]) for s in per_account), _D("0"))
    grand = _subtotal(body, None, None)
    assert total_mv == _D(grand["total_market_value"])
    assert total_ur == _D(grand["unrealized_total"])
    # Money must stay a STRING on every cell (never a JSON number).
    for s in body["holdings_subtotals"]:
        assert isinstance(s["total_market_value"], str)
        assert isinstance(s["unrealized_total"], str)


# --- frozen golden snapshot (regression lock; spec-17 §17.6.1) --------------------

_GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "dashboard_full.json"


def test_dashboard_full_golden_snapshot(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The whole payload is pinned to a committed snapshot (deterministic under the
    frozen clock). A diff here is the change-review surface; updating it REQUIRES a
    cited contract change in the same commit (spec-17 §17.7.2)."""
    body = _dashboard(dashboard_client_factory)
    assert _GOLDEN.exists(), (
        f"missing golden snapshot {_GOLDEN}; regenerate it deliberately with "
        f"scripts/regen_golden_full.py and review the diff before committing."
    )
    expected = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    assert body == expected
