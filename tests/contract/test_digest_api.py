"""Contract tests for the digest API (P3 batch 3 · Wave 1): latest / history / run / config.

Self-contained: an in-memory DB, a local FastAPI app mounting ONLY ``digest.router``,
``get_conn``/``get_now`` overridden, sockets re-enabled for the in-process TestClient
transport. Mirrors ``tests/contract/test_notify_api.py``: the default ``client`` runs
PROTECTED (one auth user — the router's guest lockdown checks ``auth_store.is_protected``),
``guest_client`` runs the same app over a guest DB (auth tables present, ZERO users) for the
403-lockdown tests.
"""

import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest_socket import disable_socket, enable_socket

from portfolio_dash.api.auth_store import create_auth_tables, create_user
from portfolio_dash.api.deps import get_conn, get_now
from portfolio_dash.api.errors import register_error_handlers
from portfolio_dash.api.routers import digest as digest_router
from portfolio_dash.ops import digest as digest_store
from portfolio_dash.scheduler.jobs import create_scheduler_tables

NOW = datetime(2026, 7, 14, 12, 0, tzinfo=ZoneInfo("Asia/Taipei"))


def _make_conn(*, protected: bool) -> sqlite3.Connection:
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    digest_store.ensure_seeded(c)
    create_scheduler_tables(c)  # job_runs (run/409) + schedule_config
    create_auth_tables(c)
    if protected:
        create_user(c, name="Owner", username="owner", password="password123", now=NOW)
    return c


def _make_client(conn: sqlite3.Connection) -> TestClient:
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(digest_router.router, prefix="/api")
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_now] = lambda: NOW
    return TestClient(app)


@pytest.fixture(autouse=True)
def _sockets() -> Iterator[None]:
    enable_socket()
    yield
    disable_socket(allow_unix_socket=True)


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


def _seed(conn: sqlite3.Connection, kind: str, date: str, payload: dict[str, object]) -> None:
    digest_store.upsert_digest(
        conn, kind=kind, digest_date=date, payload=json.dumps(payload), generated_at=date
    )


# --- latest -------------------------------------------------------------------


def test_latest_null_when_empty(client: TestClient) -> None:
    r = client.get("/api/digest/latest", params={"kind": "daily"})
    assert r.status_code == 200
    assert r.json() is None


def test_latest_returns_stored_payload(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed(conn, "daily", "2026-07-14", {"schema_version": 1, "kind": "daily"})
    r = client.get("/api/digest/latest", params={"kind": "daily"})
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "daily" and body["digest_date"] == "2026-07-14"
    assert body["payload"]["schema_version"] == 1


def test_latest_bad_kind_400(client: TestClient) -> None:
    r = client.get("/api/digest/latest", params={"kind": "monthly"})
    assert r.status_code == 400
    assert r.json()["error"]["field"] == "kind"


# --- history ------------------------------------------------------------------


def test_history_pages_and_total_constant(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    for d in ("2026-07-10", "2026-07-11", "2026-07-12"):
        _seed(conn, "daily", d, {"d": d})
    p1 = client.get("/api/digest/history", params={"kind": "daily", "offset": 0, "limit": 2}).json()
    assert p1["total"] == 3 and p1["offset"] == 0
    assert [r["digest_date"] for r in p1["rows"]] == ["2026-07-12", "2026-07-11"]
    p2 = client.get("/api/digest/history", params={"kind": "daily", "offset": 2, "limit": 2}).json()
    assert p2["total"] == 3
    assert [r["digest_date"] for r in p2["rows"]] == ["2026-07-10"]


def test_history_validation_400s(client: TestClient) -> None:
    def _hist(params: dict[str, object]) -> int:
        return int(client.get("/api/digest/history", params=params).status_code)

    assert _hist({"kind": "daily", "limit": 0}) == 400
    assert _hist({"kind": "daily", "limit": 21}) == 400
    assert _hist({"kind": "daily", "offset": -1}) == 400
    assert _hist({"kind": "bad"}) == 400


# --- config -------------------------------------------------------------------


def test_config_get_default_off(client: TestClient) -> None:
    assert client.get("/api/digest/config").json() == {"llm_summary_enabled": False}


def test_config_put_round_trip(client: TestClient) -> None:
    r = client.put("/api/digest/config", json={"llm_summary_enabled": True})
    assert r.status_code == 200 and r.json() == {"llm_summary_enabled": True}
    assert client.get("/api/digest/config").json() == {"llm_summary_enabled": True}


# --- run (async) --------------------------------------------------------------


def test_run_daily_202_then_409_while_in_flight(client: TestClient) -> None:
    first = client.post("/api/digest/run", json={"kind": "daily"})
    assert first.status_code == 202
    assert first.json()["kind"] == "daily" and isinstance(first.json()["run_id"], int)
    # The injected conn's running row persists (the worker uses its own session), so a
    # second immediate run is refused.
    second = client.post("/api/digest/run", json={"kind": "daily"})
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "already_running"


def test_run_bad_kind_400(client: TestClient) -> None:
    r = client.post("/api/digest/run", json={"kind": "monthly"})
    assert r.status_code == 400


# --- guest gate (FU-D4: run now OPEN; config PUT stays 403; reads open) --------


def test_guest_run_now_is_open_202(guest_client: TestClient) -> None:
    # FU-D4: the manual run is a compute+cache action → open in guest/demo mode (the
    # outbound push is separately suppressed in digest_service._push). Async 202, same as
    # the protected path (the background worker uses its own session; a second immediate
    # run is refused with 409 exactly as the protected in-flight test asserts).
    run = guest_client.post("/api/digest/run", json={"kind": "daily"})
    assert run.status_code == 202
    assert run.json()["kind"] == "daily" and isinstance(run.json()["run_id"], int)


def test_guest_config_put_stays_403(guest_client: TestClient) -> None:
    cfg = guest_client.put("/api/digest/config", json={"llm_summary_enabled": True})
    assert cfg.status_code == 403


def test_guest_reads_are_open(guest_client: TestClient) -> None:
    assert guest_client.get("/api/digest/latest", params={"kind": "daily"}).status_code == 200
    assert guest_client.get("/api/digest/config").status_code == 200
    assert guest_client.get("/api/digest/history", params={"kind": "weekly"}).status_code == 200
