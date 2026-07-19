"""FU-D18 contract (supersedes FU-D13's three-tier delete): DELETE now SOFT-deletes any
non-held symbol (archived=1, no data removed); the held ⇒ 422 tier is unchanged and the
former has_history 422 tier is gone. Restore doors: (a) un-archive PUT, (b) register on an
archived symbol — both return last_price_date and schedule a background gap backfill (mocked
here so tests never hit the network). Also: GET carries `archived`, the money-core invariant
(soft-deleting / archiving never changes /api/dashboard), and the scope exclusions (worklist
/ signals / news "all") with the explicit single-symbol news scope still reaching an archived
name.
"""

import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest

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


@pytest.fixture(autouse=True)
def _no_bg_backfill(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Neutralize the FU-D18 gap backfill in the TestClient (it would open its own real DB
    session and hit the network — pytest-socket is lifted inside the factory). Records the
    symbols scheduled so a test can assert the restore doors trigger it. Patches BOTH the
    router-local reference (doors a/b) and the service-local one (door c / quick_register)."""
    calls: list[str] = []

    def _fake(symbol: str, *, now: object, conn: object = None) -> None:
        calls.append(symbol)

    monkeypatch.setattr("portfolio_dash.api.routers.instruments.gap_backfill", _fake)
    monkeypatch.setattr("portfolio_dash.api.instrument_service.gap_backfill", _fake)
    yield calls


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


# --- DELETE: 404 → held (422) → else SOFT delete (archive), never hard delete -----


def test_delete_watch_only_soft_deletes(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """A never-traded watch symbol soft-deletes: 200 removed, still registered but archived
    (FU-D18 — no hard delete; re-adding restores it)."""
    client = dashboard_client_factory(_seed_watch_closed_held)
    r = client.delete("/api/instruments/WATCH")
    assert r.status_code == 200 and r.json() == {"ok": True, "removed": True}
    by = {i["symbol"]: i for i in client.get("/api/instruments").json()["list"]}
    assert "WATCH" in by and by["WATCH"]["archived"] is True


def test_delete_closed_history_soft_deletes(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """A closed-with-history symbol soft-deletes too — the former has_history 422 is gone."""
    client = dashboard_client_factory(_seed_watch_closed_held)
    r = client.delete("/api/instruments/CLSD")
    assert r.status_code == 200 and r.json() == {"ok": True, "removed": True}
    by = {i["symbol"]: i for i in client.get("/api/instruments").json()["list"]}
    assert by["CLSD"]["archived"] is True


def test_delete_unknown_404(dashboard_client_factory: DashboardClientFactory) -> None:
    client = dashboard_client_factory(_seed_watch_closed_held)
    r = client.delete("/api/instruments/NOPE")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_delete_held_422(dashboard_client_factory: DashboardClientFactory) -> None:
    client = dashboard_client_factory(_seed_watch_closed_held)
    r = client.delete("/api/instruments/2330")
    assert r.status_code == 422 and r.json()["error"]["code"] == "held"
    # a held symbol is never archived by the refused delete
    by = {i["symbol"]: i for i in client.get("/api/instruments").json()["list"]}
    assert by["2330"]["archived"] is False


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


def test_unarchive_succeeds_and_schedules_backfill(
    dashboard_client_factory: DashboardClientFactory, _no_bg_backfill: list[str]
) -> None:
    """FU-D18 door (a): un-archiving returns last_price_date (CLSD has no price row → None)
    and schedules a background gap backfill for the restored symbol."""
    client = dashboard_client_factory(_seed_watch_closed_held)
    client.put("/api/instruments/CLSD/archive", json={"archived": True})
    r = client.put("/api/instruments/CLSD/archive", json={"archived": False})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "archived": False, "last_price_date": None}
    assert "CLSD" in _no_bg_backfill  # background backfill scheduled
    by = {i["symbol"]: i for i in client.get("/api/instruments").json()["list"]}
    assert by["CLSD"]["archived"] is False


def test_unarchive_reports_last_price_date(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """WATCH has a stored price (2026-06-09); restoring it reports that as the last data
    on file (the boundary the backfill fills forward from)."""
    client = dashboard_client_factory(_seed_watch_closed_held)
    client.delete("/api/instruments/WATCH")  # soft delete = archive
    r = client.put("/api/instruments/WATCH/archive", json={"archived": False})
    assert r.status_code == 200
    assert r.json()["last_price_date"] == "2026-06-09"


# --- door (b): POST /instruments on an archived symbol RESTORES it -------------


def test_register_on_archived_restores(
    dashboard_client_factory: DashboardClientFactory, _no_bg_backfill: list[str]
) -> None:
    """FU-D18 door (b): registering an existing archived symbol un-archives it, applies the
    provided metadata, returns restored + last_price_date, and schedules a background gap
    backfill — no 409 duplicate, no synchronous network fetch."""
    client = dashboard_client_factory(_seed_watch_closed_held)
    client.delete("/api/instruments/WATCH")  # soft delete = archive
    r = client.post("/api/instruments", json={
        "symbol": "WATCH", "market": "US", "sector": "NewSector", "name": "Renamed",
    })
    assert r.status_code == 201
    body = r.json()
    assert body["restored"] is True
    assert body["last_price_date"] == "2026-06-09"
    assert body["archived"] is False
    assert body["sector"] == "NewSector" and body["name"] == "Renamed"
    assert "WATCH" in _no_bg_backfill


def test_register_on_active_duplicate_still_409(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """A non-archived duplicate still 409s (restore is archived-only)."""
    client = dashboard_client_factory(_seed_watch_closed_held)
    r = client.post("/api/instruments", json={"symbol": "WATCH", "market": "US"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "duplicate_symbol"


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


def _seed_golden_plus_watch(conn: sqlite3.Connection) -> None:
    """The golden oracle scenario + two never-traded watch symbols (with price rows) to
    soft-delete via the DELETE endpoint."""
    _seed_golden(conn)
    for sym in ("WCH1", "WCH2"):
        upsert_instrument(conn, Instrument(symbol=sym, market=Market.US,
                                           quote_ccy=Currency.USD, sector="Tech", name=sym))
        upsert_prices(conn, [PriceRow(instrument=sym, market=Market.US, as_of=date(2026, 6, 9),
                                      close=Decimal("50"), source="test")], fetched_at=GOLDEN_NOW)
    conn.commit()


def test_soft_delete_symbols_leaves_dashboard_byte_identical(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """FU-D18: soft-deleting (DELETE) N never-traded watch symbols must not move ANY dashboard
    number — a soft delete only sets archived=1, and archived symbols stay registered and are
    never held, so build_dashboard sees the exact same ledger + registry. The frozen-clock
    payload is byte-for-byte identical before and after the N deletes."""
    client = dashboard_client_factory(_seed_golden_plus_watch)
    before = client.get("/api/dashboard")
    assert before.status_code == 200
    for sym in ("WCH1", "WCH2"):
        r = client.delete(f"/api/instruments/{sym}")
        assert r.status_code == 200 and r.json() == {"ok": True, "removed": True}
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
