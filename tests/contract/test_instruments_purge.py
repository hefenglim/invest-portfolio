"""FU-D32 permanent-removal (硬 purge) contract + the additive ``has_history`` list wire.

The watchlist delete dialog now offers two destructive tiers: 移除（隱藏）(the FU-D18 soft
delete, unchanged and covered by ``test_instruments_delete_archive``) and 永久移除, routed here
via ``POST /api/instruments/{symbol}/purge``. This module pins the purge gate matrix:

  404 unknown → 422 ``held`` → 422 ``has_history`` (ANY ledger history incl. closed positions)
  → else ``delete_instrument`` (registry row + derived/cache rows gone).

Plus the benchmark guard (a purge of a benchmark storage key keeps its ``prices`` rows so the
dashboard TWR series survives) and the additive ``has_history`` field the dialog pre-disables on.
"""

import sqlite3
from datetime import date
from decimal import Decimal

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.pricing.benchmarks import BENCHMARKS
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW, DashboardClientFactory


def _held_tw(conn: sqlite3.Connection) -> None:
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Semi", name="TSMC", board="TWSE"))
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("1000"), price=Decimal("500"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 1, 5))


def _watch_only(conn: sqlite3.Connection) -> None:
    """A never-traded watch symbol with a stored price row (so its cleanup is observable)."""
    upsert_instrument(conn, Instrument(symbol="WATCH", market=Market.US, quote_ccy=Currency.USD,
                                       sector="Tech", name="Watch"))
    upsert_prices(conn, [PriceRow(instrument="WATCH", market=Market.US, as_of=date(2026, 6, 9),
                                  close=Decimal("50"), source="test")], fetched_at=GOLDEN_NOW)


def _closed_us(conn: sqlite3.Connection) -> None:
    upsert_instrument(conn, Instrument(symbol="CLSD", market=Market.US, quote_ccy=Currency.USD,
                                       sector="Tech", name="Closed"))
    insert_transaction(conn, account_id="schwab", symbol="CLSD", side=Side.BUY,
                       quantity=Decimal("10"), price=Decimal("100"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 1, 10))
    insert_transaction(conn, account_id="schwab", symbol="CLSD", side=Side.SELL,
                       quantity=Decimal("10"), price=Decimal("120"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 2, 10))


def _seed_tiers(conn: sqlite3.Connection) -> None:
    """Held (2330), never-traded watch (WATCH), closed-with-history (CLSD) in one DB."""
    seed_accounts(conn)
    _held_tw(conn)
    _watch_only(conn)
    _closed_us(conn)
    conn.commit()


def _capture_seed(seed: object) -> tuple[object, dict[str, sqlite3.Connection]]:
    """Wrap a seed fn so the test can read the app's connection AFTER the request (there is no
    price-inspection API; the purge cleanup / benchmark-survival must be checked at the table)."""
    holder: dict[str, sqlite3.Connection] = {}

    def _wrapped(conn: sqlite3.Connection) -> None:
        holder["conn"] = conn
        seed(conn)  # type: ignore[operator]

    return _wrapped, holder


# --- purge gate matrix: 404 → held → has_history → else hard delete -------------------


def test_purge_never_traded_200_and_cleans_tables(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    seed, holder = _capture_seed(_seed_tiers)
    client = dashboard_client_factory(seed)  # type: ignore[arg-type]
    r = client.post("/api/instruments/WATCH/purge")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "purged": True, "preserved_market_data": False}
    conn = holder["conn"]
    # registry row AND the market-data rows are gone (a plain non-benchmark purge).
    assert conn.execute("SELECT 1 FROM instruments WHERE symbol='WATCH'").fetchone() is None
    assert conn.execute(
        "SELECT COUNT(*) c FROM prices WHERE instrument='WATCH'").fetchone()["c"] == 0
    # and it no longer appears in the list.
    listed = {i["symbol"] for i in client.get("/api/instruments").json()["list"]}
    assert "WATCH" not in listed


def test_purge_closed_history_422(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """A closed-with-history symbol can NEVER be purged (its trades still feed cashflow/XIRR)."""
    seed, holder = _capture_seed(_seed_tiers)
    client = dashboard_client_factory(seed)  # type: ignore[arg-type]
    r = client.post("/api/instruments/CLSD/purge")
    assert r.status_code == 422 and r.json()["error"]["code"] == "has_history"
    # nothing removed — the row (and its history) survive.
    assert holder["conn"].execute(
        "SELECT 1 FROM instruments WHERE symbol='CLSD'").fetchone() is not None


def test_purge_held_422(dashboard_client_factory: DashboardClientFactory) -> None:
    seed, holder = _capture_seed(_seed_tiers)
    client = dashboard_client_factory(seed)  # type: ignore[arg-type]
    r = client.post("/api/instruments/2330/purge")
    assert r.status_code == 422 and r.json()["error"]["code"] == "held"
    assert holder["conn"].execute(
        "SELECT 1 FROM instruments WHERE symbol='2330'").fetchone() is not None


def test_purge_unknown_404(dashboard_client_factory: DashboardClientFactory) -> None:
    client = dashboard_client_factory(_seed_tiers)
    r = client.post("/api/instruments/NOPE/purge")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


# --- benchmark guard: a benchmark-key purge KEEPS its market-data rows ----------------


def _benchmark_key() -> str:
    """A benchmark storage key that IS a valid user symbol (0050; ^GSPC cannot be one)."""
    for b in BENCHMARKS:
        if not b.storage_key.startswith("^"):
            return b.storage_key
    raise AssertionError("no user-registerable benchmark key")


def test_purge_benchmark_key_preserves_market_data(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """Purging a symbol that is also a benchmark storage key removes the registry row + personal
    artifacts but PRESERVES prices, so the dashboard TWR benchmark series is not orphaned."""
    key = _benchmark_key()  # "0050"

    def _seed(conn: sqlite3.Connection) -> None:
        seed_accounts(conn)
        upsert_instrument(conn, Instrument(symbol=key, market=Market.TW, quote_ccy=Currency.TWD,
                                           sector="ETF", name="元大台灣50", board="TWSE",
                                           is_etf=True))
        upsert_prices(conn, [PriceRow(instrument=key, market=Market.TW, as_of=date(2026, 6, 9),
                                      close=Decimal("180"), source="test")], fetched_at=GOLDEN_NOW)

    seed, holder = _capture_seed(_seed)
    client = dashboard_client_factory(seed)  # type: ignore[arg-type]
    r = client.post(f"/api/instruments/{key}/purge")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "purged": True, "preserved_market_data": True}
    conn = holder["conn"]
    # registry row gone, but the benchmark's price series SURVIVES.
    assert conn.execute("SELECT 1 FROM instruments WHERE symbol=?", (key,)).fetchone() is None
    assert conn.execute(
        "SELECT COUNT(*) c FROM prices WHERE instrument=?", (key,)).fetchone()["c"] >= 1


# --- additive ``has_history`` list wire (the dialog pre-disable field) -----------------


def test_list_carries_has_history(dashboard_client_factory: DashboardClientFactory) -> None:
    client = dashboard_client_factory(_seed_tiers)
    by = {i["symbol"]: i for i in client.get("/api/instruments").json()["list"]}
    assert all(isinstance(i["has_history"], bool) for i in by.values())
    assert by["2330"]["has_history"] is True   # held ⇒ has ledger history
    assert by["CLSD"]["has_history"] is True   # closed-with-history
    assert by["WATCH"]["has_history"] is False  # never traded


def test_soft_delete_still_unchanged(dashboard_client_factory: DashboardClientFactory) -> None:
    """The plain DELETE tier stays a soft delete (archive), byte-identical to FU-D18."""
    client = dashboard_client_factory(_seed_tiers)
    r = client.delete("/api/instruments/WATCH")
    assert r.status_code == 200 and r.json() == {"ok": True, "removed": True}
    by = {i["symbol"]: i for i in client.get("/api/instruments").json()["list"]}
    assert by["WATCH"]["archived"] is True  # still registered, just hidden
