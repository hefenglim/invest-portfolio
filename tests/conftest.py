"""Shared test fixtures: deterministic golden DB, injected clock, API client.

Determinism (spec 17.3): the golden DB is seeded via the real write paths so its
GET /api/dashboard output is a regression oracle; time is injected via the get_now
dependency override (frozen GOLDEN_NOW); the network is banned at the pytest level.
"""

import os
import sqlite3
from collections.abc import Callable, Iterator
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest_socket import disable_socket, enable_socket

from portfolio_dash.api import auth_store
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
from portfolio_dash.llm_insight.evaluations_store import ensure_tables as ensure_evaluations_tables
from portfolio_dash.llm_insight.insights_store import ensure_tables as ensure_insights_tables
from portfolio_dash.llm_insight.system_prompt import ensure_system_prompt_seeded
from portfolio_dash.portfolio.settlement_store import create_tables as create_settle_tables
from portfolio_dash.pricing import datasources_store, snapshots_store
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.pricing.store import upsert_fx, upsert_prices
from portfolio_dash.scheduler.jobs import create_scheduler_tables, migrate_job_runs_results
from portfolio_dash.shared import child_runner, result_cache
from portfolio_dash.shared.config import get_settings
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.strategy.rules_config import ensure_alert_rules_seeded
from portfolio_dash.strategy.signal_history import (
    ensure_table as ensure_signal_history_table,
)
from portfolio_dash.strategy.signal_states import ensure_table as ensure_signal_states_table

GOLDEN_NOW = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))


@pytest.fixture(autouse=True, scope="session")
def _safe_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Safety net: point the configured DB at a throwaway file + disable the scheduler,
    so any code path that opens the real connection cannot touch data/portfolio.db."""
    os.environ["PD_DISABLE_SCHEDULER"] = "1"
    os.environ["DB_PATH"] = str(tmp_path_factory.mktemp("db") / "test.db")
    # Settlement flags (PERF plan P5): the store on, every run and job in-process and
    # SYNCHRONOUS, no pre-warm. A test that needs another mode sets it with monkeypatch; the
    # e2e servers override PD_SETTLE (tests/e2e/conftest.py).
    os.environ["PD_SETTLE"] = "on"
    os.environ["PD_SETTLE_MODE"] = "inline"
    os.environ["PD_JOB_MODE"] = "inline"
    os.environ["PD_PREWARM"] = "0"
    # LiteLLM is imported on first use (PERF, spec §16), now inside a test with sockets banned
    # instead of at collection: read its bundled model-cost map, never the remote one.
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _fresh_result_cache() -> Iterator[None]:
    """Every test starts and ends with an empty result cache (``shared/result_cache.py``):
    an entry must never leak into the next test, and the cache's version connections must
    not keep a test's temporary database (and its ``-wal``) open on Windows."""
    result_cache.reset()
    yield
    result_cache.reset()


@pytest.fixture(autouse=True)
def _child_runner_open() -> Iterator[None]:
    """A test that stops the process-wide child runner (``child_runner.stop_all``, as the
    lifespan's stop does) leaves it refusing to start a child: every test starts and ends with
    it open, as the next lifespan would (``child_runner.reopen``)."""
    child_runner.reopen()
    yield
    child_runner.reopen()


@pytest.fixture(autouse=True)
def _no_pending_session_touch() -> Iterator[None]:
    """A session touch armed by a test (``auth_store.schedule_touch``: a daemon timer, a few
    seconds out) never fires into the next test: every test ends with none pending."""
    yield
    auth_store.cancel_pending_touches(timeout=0)


class _FakeTimer:
    def __init__(self, at: float, fn: Callable[[], None]) -> None:
        self.at = at
        self.fn = fn
        self.cancelled = False
        self.fired = False

    def cancel(self) -> None:
        self.cancelled = True


class TouchClock:
    """Drives the deferred session touch without waiting: a fake monotonic clock and the
    timers ``auth_store`` arms on it, fired on the TEST's thread when the clock passes them
    (so a statement the touch runs is traced in the test's order)."""

    def __init__(self) -> None:
        self.now = 0.0
        self.timers: list[_FakeTimer] = []

    def monotonic(self) -> float:
        return self.now

    def start(self, delay: float, fn: Callable[[], None]) -> _FakeTimer:
        timer = _FakeTimer(self.now + delay, fn)
        self.timers.append(timer)
        return timer

    @property
    def armed(self) -> list[_FakeTimer]:
        """The timers that have neither fired nor been cancelled."""
        return [t for t in self.timers if not (t.fired or t.cancelled)]

    def advance(self, seconds: float) -> None:
        """Move the clock by *seconds*, firing every timer that falls due on the way, in
        order (a timer that fires may arm the next one)."""
        target = self.now + seconds
        while due := [t for t in self.armed if t.at <= target]:
            timer = min(due, key=lambda t: t.at)
            self.now = max(self.now, timer.at)
            timer.fired = True
            timer.fn()
        self.now = target

    def settle(self) -> None:
        """Let every pending touch fire: past its settle window and its cap."""
        self.advance(auth_store.SESSION_TOUCH_MAX_DEFER_S + auth_store.SESSION_TOUCH_SETTLE_S)


@pytest.fixture
def touch_clock(monkeypatch: pytest.MonkeyPatch) -> TouchClock:
    """``auth_store``'s pending touch on a :class:`TouchClock`: nothing fires until the test
    advances it."""
    clock = TouchClock()
    monkeypatch.setattr(auth_store, "_monotonic", clock.monotonic)
    monkeypatch.setattr(auth_store, "_start_timer", clock.start)
    return clock


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


def _seed_dual_account(conn: sqlite3.Connection) -> None:
    """A portfolio where ONE symbol (AAPL) is genuinely held in TWO accounts.

    AAPL: schwab 30 sh @100 + moomoo_my 10 sh @110 (both US, USD) — the cross-account
    duplicate the combined-aware rebalance engine must treat as a single position. 2330 in
    tw_broker (1000 sh) gives a TW single-account symbol alongside it. Current: AAPL 120 USD,
    2330 600 TWD; USD/TWD 33. Used by the dual-account rebalance flow/contract tests — kept
    OUT of the golden oracle (`_seed_golden`) so its byte-exact regression values are stable.
    """
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Semiconductors", name="TSMC", board="TWSE"))
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                                       sector="Tech", name="Apple"))
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("1000"), price=Decimal("500"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 5))
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("30"), price=Decimal("100"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 10))
    insert_transaction(conn, account_id="moomoo_my", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("10"), price=Decimal("110"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 12))
    upsert_prices(conn, [
        PriceRow(instrument="2330", market=Market.TW, as_of=date(2026, 6, 9),
                 close=Decimal("600"), source="test"),
        PriceRow(instrument="AAPL", market=Market.US, as_of=date(2026, 6, 9),
                 close=Decimal("120"), source="test"),
    ], fetched_at=GOLDEN_NOW)
    upsert_fx(conn, [
        FxRow(base=Currency.USD, quote=Currency.TWD, as_of=date(2026, 6, 9),
              rate=Decimal("33"), source="test"),
        FxRow(base=Currency.USD, quote=Currency.MYR, as_of=date(2026, 6, 9),
              rate=Decimal("4.4"), source="test"),
        FxRow(base=Currency.MYR, quote=Currency.TWD, as_of=date(2026, 6, 9),
              rate=Decimal("7"), source="test"),
    ], fetched_at=GOLDEN_NOW)
    conn.commit()


def init_golden_base(conn: sqlite3.Connection) -> None:
    """Create the full empty schema (the same ordered table setup golden_db uses) on
    *conn*, WITHOUT any ledger rows. Callers seed their own scenario afterwards. Reused
    by golden_db (subset scenario) and the dashboard_client_factory (custom scenarios),
    so the table-setup sequence lives in exactly one place (DRY)."""
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
    ensure_evaluations_tables(conn)  # insight_evaluations: created EMPTY (spec 04c)
    ensure_signal_states_table(conn)  # signal_states derived cache: created EMPTY (P2 batch 2)
    ensure_signal_history_table(conn)  # signal_history per-day states: created EMPTY (W6)
    create_settle_tables(conn)  # the settlement store (PERF): created EMPTY, as at boot
    migrate_job_runs_results(conn)  # job_runs.results (PERF P3): the boot migration


@pytest.fixture
def golden_db() -> Iterator[sqlite3.Connection]:
    # check_same_thread=False: TestClient drives the ASGI app through an anyio portal
    # worker thread, so a route reading this connection runs off the fixture's thread.
    # Requests are serialized through the single portal (no true concurrency), so the
    # shared in-memory connection is safe to use across threads here.
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_golden_base(conn)
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


SeedFn = Callable[[sqlite3.Connection], None]


class DashboardClientFactory(Protocol):
    """A factory that builds a TestClient over a fresh, custom-seeded golden-base DB."""

    def __call__(
        self,
        seed: SeedFn,
        *,
        reporting: Currency = ...,
        now: datetime = ...,
    ) -> TestClient: ...


@pytest.fixture
def dashboard_client_factory() -> Iterator[DashboardClientFactory]:
    """Build a TestClient over a FRESH golden-base DB seeded by a caller-supplied
    ``seed(conn)`` function — for custom multi-account / multi-currency scenarios that
    the fixed subset ``golden_db`` does not cover (spec-17 §17.2 financial verification).

    Mirrors ``api_client``'s socket handling. Each call yields an independent in-memory
    DB + app; multiple clients per test are supported. All connections and dependency
    overrides are torn down when the test ends.
    """
    apps: list[FastAPI] = []
    conns: list[sqlite3.Connection] = []
    enable_socket()

    def _make(
        seed: SeedFn,
        *,
        reporting: Currency = Currency.TWD,
        now: datetime = GOLDEN_NOW,
    ) -> TestClient:
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        init_golden_base(conn)
        seed(conn)
        conn.commit()
        app = create_app()
        app.dependency_overrides[get_conn] = lambda: conn
        app.dependency_overrides[get_now] = lambda: now
        app.dependency_overrides[get_reporting] = lambda: reporting
        apps.append(app)
        conns.append(conn)
        return TestClient(app)

    try:
        yield _make
    finally:
        for app in apps:
            app.dependency_overrides.clear()
        for conn in conns:
            conn.close()
        disable_socket(allow_unix_socket=True)
