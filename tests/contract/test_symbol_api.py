"""Contract tests for GET /api/symbol/{symbol}/detail (spec 01).

Read-only: price_history comes from STORED prices (no live backfill). cost_basis binds
to the account holding the most shares (Q1); null for a non-held / watchlist symbol.
"""

import sqlite3
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import upsert_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument


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
