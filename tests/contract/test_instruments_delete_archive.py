"""FU-D13 contract: DELETE + archive endpoints, GET carries `archived`, the money-core
invariant (archiving a closed position never changes /api/dashboard), and the scope
exclusions (worklist / signals / news "all") — with the explicit single-symbol news scope
still reaching an archived name.
"""

import sqlite3
from datetime import date
from decimal import Decimal

from portfolio_dash.api.news_service import resolve_news_scope
from portfolio_dash.api.signals_service import _registered_symbols
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_transaction,
    set_instrument_archived,
    upsert_instrument,
)
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.scheduler.jobs import build_worklist
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import (
    GOLDEN_NOW,
    DashboardClientFactory,
    _seed_golden,
    init_golden_base,
)


def _held_tw(conn: sqlite3.Connection) -> None:
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Semi", name="TSMC", board="TWSE"))
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("1000"), price=Decimal("500"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 1, 5))


def _closed_us(conn: sqlite3.Connection) -> None:
    upsert_instrument(conn, Instrument(symbol="CLSD", market=Market.US, quote_ccy=Currency.USD,
                                       sector="Tech", name="Closed"))
    insert_transaction(conn, account_id="schwab", symbol="CLSD", side=Side.BUY,
                       quantity=Decimal("10"), price=Decimal("100"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 1, 10))
    insert_transaction(conn, account_id="schwab", symbol="CLSD", side=Side.SELL,
                       quantity=Decimal("10"), price=Decimal("120"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 2, 10))


def _seed_watch_closed_held(conn: sqlite3.Connection) -> None:
    """A held symbol (2330), a never-traded watch-only symbol with a price row (WATCH), and
    a closed-with-history symbol (CLSD) — the three deletion tiers in one DB."""
    seed_accounts(conn)
    _held_tw(conn)
    upsert_instrument(conn, Instrument(symbol="WATCH", market=Market.US, quote_ccy=Currency.USD,
                                       sector="Tech", name="Watch"))
    upsert_prices(conn, [PriceRow(instrument="WATCH", market=Market.US, as_of=date(2026, 6, 9),
                                  close=Decimal("50"), source="test")], fetched_at=GOLDEN_NOW)
    _closed_us(conn)
    conn.commit()


def _seed_golden_plus_closed(conn: sqlite3.Connection) -> None:
    """The golden oracle scenario + one closed-with-history symbol to archive."""
    _seed_golden(conn)
    _closed_us(conn)
    conn.commit()


# --- DELETE: 404 → held → has_history → success -------------------------------


def test_delete_watch_only_succeeds(dashboard_client_factory: DashboardClientFactory) -> None:
    client = dashboard_client_factory(_seed_watch_closed_held)
    r = client.delete("/api/instruments/WATCH")
    assert r.status_code == 200 and r.json() == {"ok": True, "symbol": "WATCH"}
    # cleanup depth (prices / signal_states / …) is unit-tested in
    # tests/data_ingestion/test_instruments_delete.py; here the row leaves the registry.
    listed = {i["symbol"] for i in client.get("/api/instruments").json()["list"]}
    assert "WATCH" not in listed


def test_delete_unknown_404(dashboard_client_factory: DashboardClientFactory) -> None:
    client = dashboard_client_factory(_seed_watch_closed_held)
    r = client.delete("/api/instruments/NOPE")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_delete_held_422(dashboard_client_factory: DashboardClientFactory) -> None:
    client = dashboard_client_factory(_seed_watch_closed_held)
    r = client.delete("/api/instruments/2330")
    assert r.status_code == 422 and r.json()["error"]["code"] == "held"


def test_delete_closed_history_422(dashboard_client_factory: DashboardClientFactory) -> None:
    client = dashboard_client_factory(_seed_watch_closed_held)
    r = client.delete("/api/instruments/CLSD")
    body = r.json()
    assert r.status_code == 422 and body["error"]["code"] == "has_history"
    assert body["error"]["field"] == "symbol"


# --- archive PUT matrix -------------------------------------------------------


def test_archive_closed_succeeds_and_get_reflects(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed_watch_closed_held)
    r = client.put("/api/instruments/CLSD/archive", json={"archived": True})
    assert r.status_code == 200 and r.json() == {"ok": True, "archived": True}
    by = {i["symbol"]: i for i in client.get("/api/instruments").json()["list"]}
    assert by["CLSD"]["archived"] is True


def test_archive_held_422(dashboard_client_factory: DashboardClientFactory) -> None:
    client = dashboard_client_factory(_seed_watch_closed_held)
    r = client.put("/api/instruments/2330/archive", json={"archived": True})
    assert r.status_code == 422 and r.json()["error"]["code"] == "held"


def test_unarchive_succeeds(dashboard_client_factory: DashboardClientFactory) -> None:
    client = dashboard_client_factory(_seed_watch_closed_held)
    client.put("/api/instruments/CLSD/archive", json={"archived": True})
    r = client.put("/api/instruments/CLSD/archive", json={"archived": False})
    assert r.status_code == 200 and r.json() == {"ok": True, "archived": False}
    by = {i["symbol"]: i for i in client.get("/api/instruments").json()["list"]}
    assert by["CLSD"]["archived"] is False


def test_archive_unknown_404(dashboard_client_factory: DashboardClientFactory) -> None:
    client = dashboard_client_factory(_seed_watch_closed_held)
    r = client.put("/api/instruments/NOPE/archive", json={"archived": True})
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_get_carries_archived(dashboard_client_factory: DashboardClientFactory) -> None:
    client = dashboard_client_factory(_seed_watch_closed_held)
    lst = client.get("/api/instruments").json()["list"]
    assert all(isinstance(i["archived"], bool) for i in lst)
    by = {i["symbol"]: i for i in lst}
    assert by["2330"]["archived"] is False


# --- money-core invariant: archiving never changes the dashboard --------------


def test_archive_closed_position_leaves_dashboard_byte_identical(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """Archiving a closed-with-history symbol must not move ANY dashboard number: archived
    symbols stay registered, so build_dashboard sees the exact same ledger + registry. The
    frozen-clock payload is byte-for-byte identical before and after the archive PUT."""
    client = dashboard_client_factory(_seed_golden_plus_closed)
    before = client.get("/api/dashboard")
    assert before.status_code == 200
    r = client.put("/api/instruments/CLSD/archive", json={"archived": True})
    assert r.status_code == 200
    after = client.get("/api/dashboard")
    assert after.status_code == 200
    assert after.text == before.text


# --- scope exclusions (conn-level: worklist / signals / news) -----------------


def _conn_with_archived() -> sqlite3.Connection:
    """Golden registry (2330 + AAPL held) + a registered-then-archived watch symbol."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_golden_base(conn)
    _seed_golden(conn)
    upsert_instrument(conn, Instrument(symbol="WREG", market=Market.US, quote_ccy=Currency.USD,
                                       sector="Tech", name="WReg"))
    set_instrument_archived(conn, "WREG", True)
    return conn


def test_build_worklist_excludes_archived() -> None:
    conn = _conn_with_archived()
    try:
        refs, _fx = build_worklist(conn, None)
        syms = {r.symbol for r in refs}
        assert "WREG" not in syms and "2330" in syms
    finally:
        conn.close()


def test_registered_symbols_excludes_archived() -> None:
    conn = _conn_with_archived()
    try:
        syms = _registered_symbols(conn)
        assert "WREG" not in syms and "2330" in syms
    finally:
        conn.close()


def test_news_scope_all_excludes_but_explicit_allows_archived() -> None:
    conn = _conn_with_archived()
    try:
        scope_all = resolve_news_scope(conn, "all")
        assert scope_all is not None
        assert "WREG" not in {s for s, _m in scope_all}
        # an EXPLICIT single-symbol scope still targets the archived name (the user asked).
        assert resolve_news_scope(conn, "WREG") == [("WREG", "US")]
    finally:
        conn.close()
