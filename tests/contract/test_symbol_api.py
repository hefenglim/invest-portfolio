"""Contract tests for GET /api/symbol/{symbol}/detail (spec 01).

Read-only: price_history comes from STORED prices (no live backfill). cost_basis binds
to the account holding the most shares (Q1); null for a non-held / watchlist symbol.
"""

import sqlite3
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_dividend,
    insert_transaction,
    upsert_instrument,
    upsert_opening,
)
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.pricing.store import upsert_fx, upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW, DashboardClientFactory, _seed_dual_account


def test_symbol_detail_held_symbol_full_shape(api_client: TestClient) -> None:
    r = api_client.get("/api/symbol/2330/detail")
    assert r.status_code == 200
    body = r.json()

    assert body["symbol"] == "2330"
    # as_of is the frozen clock's date.
    assert body["as_of"] == "2026-06-11"

    # Registry enrichment (FU-D24): name + market for the drawer title.
    assert body["name"] == "TSMC"
    assert body["market"] == "TW"

    # cost_basis -> Q1 most-shares account; money as Decimal strings.
    cb = body["cost_basis"]
    assert cb["account_id"] == "tw_broker"
    assert cb["original_avg"] == "500"
    assert cb["adjusted_avg"] == "495"  # 5000 cash div reduced adj cost over 1000 sh

    # price_history from stored prices (read-only), latest stored date 2026-06-09.
    ph = body["price_history"]
    assert ph["available"] is True
    assert ph["points"]  # non-empty
    assert ph["last_date"] == "2026-06-09"
    assert ph["partial"] is False
    assert all(isinstance(p["close"], str) for p in ph["points"])

    # dividend_events: the 2026-03-01 cash div, lowercase type, UPPER ccy.
    cash = [d for d in body["dividend_events"] if d["type"] == "cash"]
    assert len(cash) == 1
    assert cash[0]["net"] == "5000"
    assert cash[0]["ccy"] == "TWD"

    # trade_events: the buy, lowercase side, money as strings.
    buys = [t for t in body["trade_events"] if t["side"] == "buy"]
    assert len(buys) == 1
    assert buys[0]["shares"] == "1000"
    assert buys[0]["price"] == "500"

    # realized_rows filtered to this symbol (no sells -> empty).
    assert body["realized_rows"] == []


def test_symbol_detail_us_account_resolution(api_client: TestClient) -> None:
    body = api_client.get("/api/symbol/AAPL/detail").json()
    assert body["cost_basis"]["account_id"] == "schwab"
    assert body["cost_basis"]["original_avg"] == "100"


def test_symbol_detail_non_held_symbol_null_cost_basis(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    # A registered watchlist instrument with no holdings/transactions.
    upsert_instrument(
        golden_db,
        Instrument(symbol="NVDA", market=Market.US, quote_ccy=Currency.USD,
                   sector="Tech", name="NVIDIA"),
    )
    golden_db.commit()

    body = api_client.get("/api/symbol/NVDA/detail").json()
    assert body["symbol"] == "NVDA"
    # Registry enrichment is present for a registered-but-unheld watchlist symbol.
    assert body["name"] == "NVIDIA"
    assert body["market"] == "US"
    assert body["cost_basis"] is None
    # No stored prices for NVDA -> price_history unavailable, with a note.
    assert body["price_history"]["available"] is False
    assert body["price_history"]["note"] is not None
    assert body["dividend_events"] == []
    assert body["trade_events"] == []
    assert body["realized_rows"] == []


def test_symbol_detail_days_window(api_client: TestClient) -> None:
    # days controls the lower bound of the stored-price window; the 2026-06-09 point
    # is within 180 days of 2026-06-11 but outside a 1-day window.
    far = api_client.get("/api/symbol/2330/detail?days=1").json()
    assert far["price_history"]["available"] is False

    near = api_client.get("/api/symbol/2330/detail?days=180").json()
    assert near["price_history"]["available"] is True
    assert any(p["date"] == "2026-06-09" for p in near["price_history"]["points"])


def test_symbol_detail_unregistered_symbol_null_name_market(api_client: TestClient) -> None:
    # A symbol with no instrument row: name/market degrade to null (FU-D24).
    body = api_client.get("/api/symbol/ZZZZ/detail").json()
    assert body["symbol"] == "ZZZZ"
    assert body["name"] is None
    assert body["market"] is None
    assert body["cost_basis"] is None
    assert body["price_history"]["available"] is False


def test_symbol_detail_money_and_date_field(api_client: TestClient) -> None:
    body = api_client.get("/api/symbol/2330/detail").json()
    # as_of is a plain date string (not a datetime).
    assert date.fromisoformat(body["as_of"]) == date(2026, 6, 11)
    # adjusted_avg parses as a Decimal.
    assert Decimal(body["cost_basis"]["adjusted_avg"]) == Decimal("495")


# --- round-8.1 Wave A: unified activity + reconciliation (owner #2a) ----------------

def _seed_full_activity(conn: sqlite3.Connection) -> None:
    """A single-account symbol exercising EVERY share-affecting event kind (schwab / AAPL):

      opening 5 sh (cost 400) + buy 10 @ 100 + sell 3 @ 120 + DRIP reinvest 0.5 sh @ 130
      → book shares 5 + 10 − 3 + 0.5 = 12.5, so 交易明細 must show 4 event kinds and the
        reconciliation footer must balance (net == book).
    """
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                                       sector="Tech", name="Apple"))
    upsert_opening(conn, account_id="schwab", symbol="AAPL", shares=Decimal("5"),
                   original_cost_total=Decimal("400"), build_date=date(2026, 1, 1))
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("10"), price=Decimal("100"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 5))
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.SELL,
                       quantity=Decimal("3"), price=Decimal("120"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 2, 1))
    insert_dividend(conn, account_id="schwab", symbol="AAPL", div_date=date(2026, 3, 1),
                    div_type="DRIP", gross=Decimal("65"), withholding=Decimal("0"),
                    net=Decimal("65"), reinvest_shares=Decimal("0.5"),
                    reinvest_price=Decimal("130"))
    upsert_prices(conn, [
        PriceRow(instrument="AAPL", market=Market.US, as_of=date(2026, 6, 9),
                 close=Decimal("120"), source="test"),
    ], fetched_at=GOLDEN_NOW)
    upsert_fx(conn, [
        FxRow(base=Currency.USD, quote=Currency.TWD, as_of=date(2026, 6, 9),
              rate=Decimal("33"), source="test"),
    ], fetched_at=GOLDEN_NOW)
    conn.commit()


def test_symbol_detail_activity_all_event_kinds_present(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed_full_activity)
    body = client.get("/api/symbol/AAPL/detail").json()

    activity = body["activity"]
    sides = [a["side"] for a in activity]
    # opening, buy, sell, and the DRIP reinvest row are ALL present (owner #2a).
    assert set(sides) == {"open", "buy", "sell", "drip"}
    # chronological (open 01-01 → buy 01-05 → sell 02-01 → drip 03-01).
    assert [a["date"] for a in activity] == [
        "2026-01-01", "2026-01-05", "2026-02-01", "2026-03-01"]
    # every row is account-tagged with id + display name.
    assert all(a["account_id"] == "schwab" and a["account"] for a in activity)
    # opening carries no fee/tax and uses original_avg (400/5 = 80) as its price.
    opening = next(a for a in activity if a["side"] == "open")
    assert opening["price"] == "80" and opening["fee"] is None and opening["tax"] is None
    # the DRIP reinvest row carries its reinvest price and zero cash total.
    drip = next(a for a in activity if a["side"] == "drip")
    assert drip["shares"] == "0.5" and drip["price"] == "130" and Decimal(drip["total"]) == 0
    # signed cash total: buy −(10×100) = −1000; sell +(3×120) = +360.
    buy = next(a for a in activity if a["side"] == "buy")
    sell = next(a for a in activity if a["side"] == "sell")
    assert Decimal(buy["total"]) == Decimal("-1000")
    assert Decimal(sell["total"]) == Decimal("360")


def test_symbol_detail_activity_reconciles_with_position(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed_full_activity)
    body = client.get("/api/symbol/AAPL/detail").json()

    rec = body["activity_reconcile"]["total"]
    assert rec["opening_shares"] == "5"
    assert rec["buy_shares"] == "10"
    assert rec["sell_shares"] == "3"
    assert rec["reinvest_shares"] == "0.5"
    # 5 + 10 − 3 + 0.5 = 12.5 = the book (部位摘要) shares → the identity balances.
    assert Decimal(rec["net_shares"]) == Decimal("12.5")
    assert Decimal(rec["book_shares"]) == Decimal("12.5")
    assert rec["balances"] is True
    # the aggregate 部位摘要 share count matches the reconciliation's book figure.
    assert Decimal(body["position"]["shares"]) == Decimal(rec["book_shares"])
    # single account → per-account reconcile mirrors the total.
    assert body["activity_reconcile"]["by_account"]["schwab"]["balances"] is True


# --- round-8.1 Wave A: cross-account aggregate position (owner #2c) ------------------

def test_symbol_detail_position_multi_account_aggregate(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """AAPL held in schwab (30 @100) + moomoo_my (10 @110): 部位摘要 is the AGGREGATE, and
    the aggregate money equals the sum of the per-account rows (server-computed Decimal)."""
    client = dashboard_client_factory(_seed_dual_account)
    body = client.get("/api/symbol/AAPL/detail").json()

    pos = body["position"]
    accts = body["position_accounts"]
    assert pos["account_count"] == 2
    assert len(accts) == 2
    # aggregate shares = 30 + 10.
    assert Decimal(pos["shares"]) == Decimal("40")
    # blended original average = (3000 + 1100) / 40 = 102.5 (shares-weighted, on read).
    assert Decimal(pos["original_avg"]) == Decimal("102.5")
    # AGGREGATE == Σ per-account, proven for every money field (no JS could do this safely).
    for field in ("market_value", "unrealized_pnl", "capital_gain",
                  "original_cost_total", "adjusted_cost_total"):
        agg = Decimal(pos[field])
        per = sum(Decimal(a[field]) for a in accts)
        assert agg == per, f"{field}: aggregate {agg} != Σ per-account {per}"
    # concrete: 30×120 + 10×120 = 4800 market value; (120−100)×30 + (120−110)×10 = 700 unreal.
    assert Decimal(pos["market_value"]) == Decimal("4800")
    assert Decimal(pos["unrealized_pnl"]) == Decimal("700")
    # weight aggregate is the Σ of per-account weights (reporting-currency ratio, server-side).
    if pos["weight"] is not None:
        assert Decimal(pos["weight"]) == sum(Decimal(a["weight"]) for a in accts)
    # cost_basis still binds to the most-shares account (schwab, 30 > 10).
    assert body["cost_basis"]["account_id"] == "schwab"


def test_symbol_detail_position_single_account(api_client: TestClient) -> None:
    """A single-account symbol: 部位摘要 aggregate == that one account (unchanged behaviour)."""
    body = api_client.get("/api/symbol/2330/detail").json()
    pos = body["position"]
    assert pos["account_count"] == 1
    assert Decimal(pos["shares"]) == Decimal("1000")
    assert Decimal(pos["original_avg"]) == Decimal("500")
    assert Decimal(pos["adjusted_avg"]) == Decimal("495")  # dividend-adjusted
    assert Decimal(pos["market_value"]) == Decimal("600000")  # 600 × 1000
    assert len(body["position_accounts"]) == 1


def test_symbol_detail_position_null_for_unheld(api_client: TestClient) -> None:
    body = api_client.get("/api/symbol/ZZZZ/detail").json()
    assert body["position"] is None
    assert body["position_accounts"] == []
    assert body["activity"] == []
    # an empty ledger still reconciles (0 == 0).
    assert body["activity_reconcile"]["total"]["balances"] is True
