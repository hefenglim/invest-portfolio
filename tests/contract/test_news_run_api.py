"""Contract tests for the manual news fetch (P3 batch 3 · 3C): POST /api/news/run.

Self-contained (mirrors ``tests/contract/test_digest_api.py``): an in-memory DB, a local
FastAPI app mounting ONLY ``news.router``, ``get_conn``/``get_now`` overridden, sockets
re-enabled for the in-process TestClient transport. The default ``client`` runs PROTECTED
(one auth user — the router's guest lockdown checks ``auth_store.is_protected``);
``guest_client`` runs the same app over a guest DB (auth tables present, ZERO users) for the
403 test. ``run_news_for`` is monkeypatched so the background thread never touches the
network/LLM (the assertions only check the synchronous HTTP response anyway).
"""

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest_socket import disable_socket, enable_socket

from portfolio_dash.api import news_service
from portfolio_dash.api.auth_store import create_auth_tables, create_user
from portfolio_dash.api.deps import get_conn, get_now
from portfolio_dash.api.errors import register_error_handlers
from portfolio_dash.api.routers import news as news_router
from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.store import upsert_instrument
from portfolio_dash.scheduler.jobs import create_scheduler_tables
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument

NOW = datetime(2026, 7, 14, 12, 0, tzinfo=ZoneInfo("Asia/Taipei"))


def _make_conn(*, protected: bool) -> sqlite3.Connection:
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    bootstrap_db(c)          # instruments table (list_instruments) + ledgers
    create_scheduler_tables(c)  # job_runs (start_job_run / latest_run_unfinished)
    create_auth_tables(c)
    upsert_instrument(c, Instrument(symbol="2330", market=Market.TW,
                                    quote_ccy=Currency.TWD, sector="X", name="TSMC",
                                    board="TWSE"))
    upsert_instrument(c, Instrument(symbol="AAPL", market=Market.US,
                                    quote_ccy=Currency.USD, sector="Tech", name="Apple"))
    if protected:
        create_user(c, name="Owner", username="owner", password="password123", now=NOW)
    return c


def _make_client(conn: sqlite3.Connection) -> TestClient:
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(news_router.router, prefix="/api")
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_now] = lambda: NOW
    return TestClient(app)


@pytest.fixture(autouse=True)
def _sockets() -> Iterator[None]:
    enable_socket()
    yield
    disable_socket(allow_unix_socket=True)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the background thread off the network/LLM regardless of what it finds."""
    monkeypatch.setattr(
        news_service, "run_news_for",
        lambda conn, universe, *, now: {"organized": 0, "headline_only": 0,
                                        "skipped_existing": 0},
    )


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = _make_conn(protected=True)
    yield c
    c.close()


@pytest.fixture
def client(conn: sqlite3.Connection) -> Iterator[TestClient]:
    yield _make_client(conn)


@pytest.fixture
def guest_client() -> Iterator[TestClient]:
    c = _make_conn(protected=False)
    yield _make_client(c)
    c.close()


def test_run_all_202(client: TestClient) -> None:
    r = client.post("/api/news/run", json={"scope": "all"})
    assert r.status_code == 202
    body = r.json()
    assert body["scope"] == "all" and isinstance(body["run_id"], int)


def test_run_single_symbol_202(client: TestClient) -> None:
    r = client.post("/api/news/run", json={"scope": "2330"})
    assert r.status_code == 202 and r.json()["scope"] == "2330"


def test_run_bad_scope_400(client: TestClient) -> None:
    r = client.post("/api/news/run", json={"scope": "NOSUCH"})
    assert r.status_code == 400 and r.json()["error"]["field"] == "scope"


def test_run_409_while_in_flight(client: TestClient) -> None:
    first = client.post("/api/news/run", json={"scope": "all"})
    assert first.status_code == 202
    # The injected conn keeps the running row (the worker uses its own session), so a
    # second immediate run is refused.
    second = client.post("/api/news/run", json={"scope": "all"})
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "already_running"


def test_run_guest_403(guest_client: TestClient) -> None:
    r = guest_client.post("/api/news/run", json={"scope": "all"})
    assert r.status_code == 403
