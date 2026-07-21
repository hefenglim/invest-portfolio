"""Contract tests for GET /api/input/holdings (FU-D35 dividend picker + FU-D44 sell hints).

Per-account {held, closed} derived from the ledger:

  * held   = symbols whose CURRENT net shares in that account are > 0. FU-D44 (additive):
             each held entry also carries ``shares`` + ``adjusted_avg`` as Decimal STRINGS
             — adjusted_avg from the VERIFIED cost-basis replay (build_book;
             adjusted_total / shares computed on read, domain-ledger.md), null when the
             book cannot value the position (never-500 degradation).
  * closed = symbols with ANY ledger history in that account (transactions / opening /
             dividends) whose current net shares there are 0 (a closed position can still
             pay a dividend after its ex-date — owner 假設 2). Stays {symbol, name}.

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
    insert_dividend,
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
    # tw_broker: 2330 held — with a CASH dividend so adjusted_avg is DIVIDEND-ADJUSTED
    # (FU-D44: proves the read reuses the real build_book cost basis, not original cost).
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("1000"), price=Decimal("500"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 5))
    insert_dividend(conn, account_id="tw_broker", symbol="2330", div_date=date(2026, 3, 5),
                    div_type="CASH", gross=Decimal("2500"), withholding=Decimal("0"),
                    net=Decimal("2500"))
    # tw_broker: 2317 closed — opening inventory fully sold (exercises the opening source
    # in the symbol-universe union, not just transactions).
    upsert_opening(conn, account_id="tw_broker", symbol="2317", shares=Decimal("1000"),
                   original_cost_total=Decimal("100000"),
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


def test_holdings_held_carries_shares_and_adjusted_avg(
    dashboard_client_factory: DashboardClientFactory
) -> None:
    """FU-D44: held entries carry shares + adjusted_avg as EXACT Decimal strings.

    adjusted_avg comes from the verified ``build_book`` replay (adjusted_total / shares,
    computed on read — domain-ledger.md): 2330 = (1000×500 − 2,500 cash dividend) / 1000
    = 497.5, the DIVIDEND-ADJUSTED value — a naive original-cost average would read 500,
    so this pins the reuse of the real cost-basis path. Closed entries stay
    {symbol, name} (the extension is additive to held only).
    """
    client: TestClient = dashboard_client_factory(_seed_holdings)
    b = client.get("/api/input/holdings?account=tw_broker").json()
    held = {h["symbol"]: h for h in b["held"]}
    assert held["2330"]["shares"] == "1000"
    assert held["2330"]["adjusted_avg"] == "497.5"
    moo = client.get("/api/input/holdings?account=moomoo_my_us").json()
    aapl = {h["symbol"]: h for h in moo["held"]}["AAPL"]
    assert aapl["shares"] == "10"
    assert aapl["adjusted_avg"] == "110"          # 10 × 110 / 10 — no dividend here
    closed = {h["symbol"]: h for h in b["closed"]}["2317"]
    assert set(closed.keys()) == {"symbol", "name"}


def test_holdings_unbookable_ledger_degrades_adjusted_avg_to_null(
    dashboard_client_factory: DashboardClientFactory
) -> None:
    """Never-500 at the build_book call site: an orphan dividend (dated before ANY
    position exists) makes the ledger un-bookable — the read still serves held/closed +
    shares, with adjusted_avg degraded to null (the hint hides; nothing crashes)."""
    def seed(conn: sqlite3.Connection) -> None:
        _seed_holdings(conn)
        insert_dividend(conn, account_id="tw_broker", symbol="2317",
                        div_date=date(2025, 1, 1), div_type="CASH",
                        gross=Decimal("1"), withholding=Decimal("0"), net=Decimal("1"))
        conn.commit()

    client: TestClient = dashboard_client_factory(seed)
    r = client.get("/api/input/holdings?account=tw_broker")
    assert r.status_code == 200
    held = {h["symbol"]: h for h in r.json()["held"]}
    assert held["2330"]["shares"] == "1000"       # share replay is independent of the book
    assert held["2330"]["adjusted_avg"] is None


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
