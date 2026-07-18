"""Contract tests for GET /api/input/holdings (FU-D35, dividend symbol picker).

Per-account {held:[{symbol,name}], closed:[{symbol,name}]} derived from the ledger:

  * held   = symbols whose CURRENT net shares in that account are > 0.
  * closed = symbols with ANY ledger history in that account (transactions / opening /
             dividends) whose current net shares there are 0 (a closed position can still
             pay a dividend after its ex-date — owner 假設 2).

Classification is strictly per (account, symbol): the SAME symbol may be held in one
account and closed in another. Names resolve from the instruments registry. Unknown
account -> 404. Share math is server-side Decimal (never duplicates cost-basis math).
"""

import sqlite3
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_transaction,
    upsert_instrument,
    upsert_opening,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import DashboardClientFactory


def _seed_holdings(conn: sqlite3.Connection) -> None:
    """Multi-account scenario exercising held / closed / cross-account isolation:

    * tw_broker  — 2330 HELD (buy 1000); 2317 CLOSED via opening 1000 + full sell 1000.
    * schwab     — AAPL CLOSED (buy 10, then sell 10).
    * moomoo_my_us — AAPL HELD (buy 10)  → same symbol, opposite class vs schwab.
    """
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Semiconductors", name="TSMC", board="TWSE"))
    upsert_instrument(conn, Instrument(symbol="2317", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Electronics", name="Hon Hai", board="TWSE"))
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                                       sector="Tech", name="Apple"))
    # tw_broker: 2330 held.
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("1000"), price=Decimal("500"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 5))
    # tw_broker: 2317 closed — opening inventory fully sold (exercises the opening source
    # in the symbol-universe union, not just transactions).
    upsert_opening(conn, account_id="tw_broker", symbol="2317", shares=Decimal("1000"),
                   original_avg_cost=Decimal("100"), original_cost_total=Decimal("100000"),
                   build_date=date(2025, 12, 1))
    insert_transaction(conn, account_id="tw_broker", symbol="2317", side=Side.SELL,
                       quantity=Decimal("1000"), price=Decimal("110"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 20))
    # schwab: AAPL closed (buy then full sell).
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("10"), price=Decimal("100"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 10))
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.SELL,
                       quantity=Decimal("10"), price=Decimal("120"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 2, 10))
    # moomoo_my_us: AAPL held.
    insert_transaction(conn, account_id="moomoo_my_us", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("10"), price=Decimal("110"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 12))
    conn.commit()


def _symbols(items: list[dict[str, str]]) -> set[str]:
    return {i["symbol"] for i in items}


def test_holdings_held_and_closed_split(
    dashboard_client_factory: DashboardClientFactory
) -> None:
    client: TestClient = dashboard_client_factory(_seed_holdings)
    b = client.get("/api/input/holdings?account=tw_broker").json()
    assert _symbols(b["held"]) == {"2330"}
    assert _symbols(b["closed"]) == {"2317"}      # opening fully sold -> closed
    # names resolve from the registry.
    assert {i["symbol"]: i["name"] for i in b["held"]}["2330"] == "TSMC"
    assert {i["symbol"]: i["name"] for i in b["closed"]}["2317"] == "Hon Hai"


def test_holdings_fully_sold_is_closed(
    dashboard_client_factory: DashboardClientFactory
) -> None:
    client: TestClient = dashboard_client_factory(_seed_holdings)
    b = client.get("/api/input/holdings?account=schwab").json()
    assert _symbols(b["held"]) == set()
    assert _symbols(b["closed"]) == {"AAPL"}      # bought then fully sold


def test_holdings_per_account_isolation(
    dashboard_client_factory: DashboardClientFactory
) -> None:
    """AAPL is CLOSED in schwab but HELD in moomoo_my_us — classification is per account."""
    client: TestClient = dashboard_client_factory(_seed_holdings)
    schwab = client.get("/api/input/holdings?account=schwab").json()
    moomoo = client.get("/api/input/holdings?account=moomoo_my_us").json()
    assert "AAPL" in _symbols(schwab["closed"]) and "AAPL" not in _symbols(schwab["held"])
    assert "AAPL" in _symbols(moomoo["held"]) and "AAPL" not in _symbols(moomoo["closed"])


def test_holdings_empty_account(
    dashboard_client_factory: DashboardClientFactory
) -> None:
    """An account with no ledger history returns empty held + closed (honest empty)."""
    client: TestClient = dashboard_client_factory(_seed_holdings)
    b = client.get("/api/input/holdings?account=moomoo_my_my").json()
    assert b == {"held": [], "closed": []}


def test_holdings_unknown_account_404(
    dashboard_client_factory: DashboardClientFactory
) -> None:
    client: TestClient = dashboard_client_factory(_seed_holdings)
    r = client.get("/api/input/holdings?account=does_not_exist")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_holdings_requires_account_param(
    dashboard_client_factory: DashboardClientFactory
) -> None:
    """The account query param is required (FastAPI 422 for a missing required query)."""
    client: TestClient = dashboard_client_factory(_seed_holdings)
    r = client.get("/api/input/holdings")
    assert r.status_code in (400, 422)
