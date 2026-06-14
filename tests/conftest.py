"""Shared test fixtures: deterministic golden DB, injected clock, API client.

Determinism (spec 17.3): the golden DB is seeded via the real write paths so its
GET /api/dashboard output is a regression oracle; time is injected via the get_now
dependency override (frozen GOLDEN_NOW); the network is banned at the pytest level.
"""

import os
import sqlite3
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from pytest_socket import disable_socket, enable_socket

from portfolio_dash.api.app import create_app
from portfolio_dash.api.auth_store import create_auth_tables
from portfolio_dash.api.deps import get_conn, get_now, get_reporting
from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_dividend,
    insert_fx_conversion,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.llm_insight.alerts_bridge import ensure_tables as ensure_alert_events_tables
from portfolio_dash.llm_insight.composer_store import ensure_seeded as ensure_composer_seeded
from portfolio_dash.llm_insight.insights_store import ensure_tables as ensure_insights_tables
from portfolio_dash.llm_insight.system_prompt import ensure_system_prompt_seeded
from portfolio_dash.pricing import datasources_store, snapshots_store
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.pricing.store import upsert_fx, upsert_prices
from portfolio_dash.scheduler.jobs import create_scheduler_tables
from portfolio_dash.shared.config import get_settings
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.strategy.rules_config import ensure_alert_rules_seeded

GOLDEN_NOW = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))


@pytest.fixture(autouse=True, scope="session")
def _safe_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Safety net: point the configured DB at a throwaway file + disable the scheduler,
    so any code path that opens the real connection cannot touch data/portfolio.db."""
    os.environ["PD_DISABLE_SCHEDULER"] = "1"
    os.environ["DB_PATH"] = str(tmp_path_factory.mktemp("db") / "test.db")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _seed_golden(conn: sqlite3.Connection) -> None:
    """Reproduce a known scenario (subset of mock-data.js) via real write paths."""
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Semiconductors", name="TSMC", board="TWSE"))
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                                       sector="Tech", name="Apple"))
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("1000"), price=Decimal("500"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 5))
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("10"), price=Decimal("100"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 10))
    insert_dividend(conn, account_id="tw_broker", symbol="2330", div_date=date(2026, 3, 1),
                    div_type="CASH", gross=Decimal("5000"), withholding=Decimal("0"),
                    net=Decimal("5000"))
    insert_fx_conversion(conn, account_id="schwab", date=date(2026, 1, 8),
                         from_ccy=Currency.TWD, from_amount=Decimal("32000"),
                         to_ccy=Currency.USD, to_amount=Decimal("1000"))
    upsert_prices(conn, [
        PriceRow(instrument="2330", market=Market.TW, as_of=date(2026, 6, 9),
                 close=Decimal("600"), source="test"),
        PriceRow(instrument="AAPL", market=Market.US, as_of=date(2026, 6, 9),
                 close=Decimal("120"), source="test"),
    ], fetched_at=GOLDEN_NOW)
    upsert_fx(conn, [
        FxRow(base=Currency.USD, quote=Currency.TWD, as_of=date(2026, 1, 8),
              rate=Decimal("32"), source="test"),
        FxRow(base=Currency.USD, quote=Currency.TWD, as_of=date(2026, 6, 9),
              rate=Decimal("33"), source="test"),
        FxRow(base=Currency.MYR, quote=Currency.TWD, as_of=date(2026, 6, 9),
              rate=Decimal("7"), source="test"),
        FxRow(base=Currency.USD, quote=Currency.MYR, as_of=date(2026, 6, 9),
              rate=Decimal("4.4"), source="test"),
    ], fetched_at=GOLDEN_NOW)
    conn.commit()


@pytest.fixture
def golden_db() -> Iterator[sqlite3.Connection]:
    # check_same_thread=False: TestClient drives the ASGI app through an anyio portal
    # worker thread, so a route reading this connection runs off the fixture's thread.
    # Requests are serialized through the single portal (no true concurrency), so the
    # shared in-memory connection is safe to use across threads here.
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    bootstrap_db(conn)
    create_pricing_tables(conn)
    create_scheduler_tables(conn)
    snapshots_store.ensure_tables(conn)  # external_snapshots: created EMPTY -> ext vars degrade
    datasources_store.ensure_seeded(conn)  # data_sources tables (spec 14)
    ensure_alert_rules_seeded(conn)  # alert-rules config (spec 03)
    create_auth_tables(conn)  # empty auth tables -> guest mode (spec 09)
    ensure_system_prompt_seeded(conn)  # global system prompt single-row (spec 06a)
    ensure_composer_seeded(conn)  # insight-composer tables: created EMPTY (spec 04a)
    ensure_insights_tables(conn)  # insights cards table: created EMPTY (spec 04b)
    ensure_alert_events_tables(conn)  # alert_events + dispatch log: created EMPTY (spec 04b)
    _seed_golden(conn)
    yield conn
    conn.close()


@pytest.fixture
def api_client(golden_db: sqlite3.Connection) -> Iterator[TestClient]:
    """TestClient with the golden DB + frozen clock injected. No lifespan (hermetic).

    Starlette's TestClient drives the ASGI app through an anyio portal; on Windows
    that portal runs a ProactorEventLoop whose internal self-pipe is a real socket
    (not a unix socket), which the global --disable-socket ban blocks. Re-enable
    sockets for the duration of this fixture (the request transport is in-process,
    not network I/O), then restore the ban on teardown so no real network leaks.
    """
    enable_socket()
    app = create_app()
    app.dependency_overrides[get_conn] = lambda: golden_db
    app.dependency_overrides[get_now] = lambda: GOLDEN_NOW
    app.dependency_overrides[get_reporting] = lambda: Currency.TWD
    client = TestClient(app)   # no `with`: lifespan not run
    try:
        yield client
    finally:
        app.dependency_overrides.clear()
        disable_socket(allow_unix_socket=True)
