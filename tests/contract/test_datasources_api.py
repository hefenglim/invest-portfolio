"""Contract tests for the data-source management API (spec 14).

Self-contained (does NOT use the shared ``api_client``/conftest fixtures): a local
in-memory connection bootstrapped with the ledger tables + the three data_sources
tables + seeded accounts, and a local FastAPI app mounting ONLY the datasources
router. The ``/test`` endpoint's probe is monkeypatched so the hermetic test never
touches the network (a real provider call only happens in production).
"""

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest_socket import disable_socket, enable_socket

from portfolio_dash.api.deps import get_conn, get_now
from portfolio_dash.api.errors import register_error_handlers
from portfolio_dash.api.routers import datasources as ds_router
from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.pricing import datasources_store as store

NOW = datetime(2026, 6, 12, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    seed_accounts(c)
    store.create_tables(c)
    store.seed(c)
    yield c
    c.close()


@pytest.fixture
def client(conn: sqlite3.Connection) -> Iterator[TestClient]:
    enable_socket()
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(ds_router.router, prefix="/api")
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_now] = lambda: NOW
    test_client = TestClient(app)
    try:
        yield test_client
    finally:
        app.dependency_overrides.clear()
        disable_socket(allow_unix_socket=True)


# --- GET /api/datasources -----------------------------------------------------


def test_get_lists_sources_grouped_with_masking(client: TestClient) -> None:
    r = client.get("/api/datasources")
    assert r.status_code == 200
    body = r.json()
    by_id = {s["id"]: s for s in body["sources"]}
    # All known sources are present, grouped by type.
    assert "twse" in by_id and "finmind" in by_id and "fx_ecb" in by_id
    assert by_id["finmind"]["type"] == "dividend"
    assert by_id["twse"]["type"] == "stock"
    # auth:"none" source has no token and (no key) is not "off".
    assert by_id["twse"]["auth"] == "none"
    assert by_id["twse"]["token_masked"] is None
    # apikey source with no key set -> token null + status "off".
    assert by_id["finmind"]["auth"] == "apikey"
    assert by_id["finmind"]["token_masked"] is None
    assert by_id["finmind"]["status"] == "off"


def test_get_includes_provides_and_status_catalog(client: TestClient) -> None:
    by_id = {s["id"]: s for s in client.get("/api/datasources").json()["sources"]}
    # spec-20.1 catalog: full source list with provides + status.
    assert {"twstock", "stockprices_dev", "klsescreener", "malaysiastock", "cnn_fng",
            "alphavantage", "finnhub", "fred", "schwab", "bursa"} <= set(by_id)
    # provides is a list of data types per source.
    assert "quote_latest" in by_id["twse"]["provides"]
    assert set(by_id["finmind"]["provides"]) >= {"dividend", "institutional", "margin"}
    # pending token sources surface status "pending" with no key.
    assert by_id["finnhub"]["status"] == "pending"
    assert by_id["finnhub"]["token_masked"] is None
    assert by_id["fred"]["status"] == "pending"
    # blocked source surfaces status "blocked".
    assert by_id["bursa"]["status"] == "blocked"
    # live key-less source keeps its dynamic health status (not overridden).
    assert by_id["twstock"]["status"] in ("unknown", "ok", "error", "off")


def test_get_includes_account_fallbacks_and_names(client: TestClient) -> None:
    body = client.get("/api/datasources").json()
    fb = body["account_fallbacks"]
    # Seeded from the market default quote chains (spec 20.8 appended free fallbacks).
    assert fb["tw_broker"] == ["twse", "tpex", "yfinance", "twstock"]
    assert fb["schwab"] == ["yfinance", "stockprices_dev"]
    names = body["account_names"]
    assert names["tw_broker"] == "TW Broker"
    assert set(names) == {"tw_broker", "schwab", "moomoo_my_us", "moomoo_my_my"}


# --- PUT /api/datasources/{id}/key --------------------------------------------


def test_put_key_masks_and_resets_health(client: TestClient) -> None:
    r = client.put("/api/datasources/finmind/key", json={"api_key": "fm-secret9b1"})
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "finmind"
    assert body["token_masked"] == "fm-•••9b1"
    assert body["status"] == "unknown"
    # GET now reflects the masked key (key itself never echoed in full).
    by_id = {s["id"]: s for s in client.get("/api/datasources").json()["sources"]}
    assert by_id["finmind"]["token_masked"] == "fm-•••9b1"
    assert by_id["finmind"]["status"] == "unknown"


def test_put_key_empty_clears(client: TestClient) -> None:
    client.put("/api/datasources/finmind/key", json={"api_key": "fm-secret9b1"})
    r = client.put("/api/datasources/finmind/key", json={"api_key": ""})
    assert r.status_code == 200
    assert r.json()["token_masked"] is None


def test_put_key_unknown_source_404(client: TestClient) -> None:
    r = client.put("/api/datasources/nope/key", json={"api_key": "x"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_put_key_on_auth_none_source_400(client: TestClient) -> None:
    r = client.put("/api/datasources/twse/key", json={"api_key": "x"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "validation_error"


# --- POST /api/datasources/{id}/test (hermetic) -------------------------------


def test_post_test_success_records_health(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Hermetic: replace the probe so no real provider call / network occurs.
    monkeypatch.setattr(ds_router, "probe_source", lambda sid, key: (True, None))
    r = client.post("/api/datasources/twse/test")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "twse"
    assert body["status"] == "ok"
    assert body["latency_ms"] is not None and body["latency_ms"] >= 0
    assert body["last_test"] == NOW.isoformat()
    # Health is persisted and surfaced by GET.
    by_id = {s["id"]: s for s in client.get("/api/datasources").json()["sources"]}
    assert by_id["twse"]["status"] == "ok"


def test_post_test_failure_is_200(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        ds_router, "probe_source", lambda sid, key: (False, "HTTP 502 from provider")
    )
    r = client.post("/api/datasources/klsescreener/test")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "error"
    assert body["latency_ms"] is None
    assert body["detail"] == "HTTP 502 from provider"


def test_post_test_exception_is_200_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(sid: str, key: str | None) -> tuple[bool, str | None]:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(ds_router, "probe_source", _boom)
    r = client.post("/api/datasources/twse/test")
    assert r.status_code == 200
    assert r.json()["status"] == "error"
    assert "connection refused" in r.json()["detail"]


def test_post_test_unknown_source_404(client: TestClient) -> None:
    r = client.post("/api/datasources/nope/test")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


# --- PUT /api/datasources/fallbacks -------------------------------------------


def test_put_fallbacks_overwrites(client: TestClient) -> None:
    r = client.put(
        "/api/datasources/fallbacks",
        json={"account_fallbacks": {"moomoo_my_my": ["yfinance", "klsescreener"]}},
    )
    assert r.status_code == 200
    assert r.json()["account_fallbacks"]["moomoo_my_my"] == ["yfinance", "klsescreener"]
    # Persisted: GET reflects the new chain.
    fb = client.get("/api/datasources").json()["account_fallbacks"]
    assert fb["moomoo_my_my"] == ["yfinance", "klsescreener"]


def test_put_fallbacks_unknown_source_400(client: TestClient) -> None:
    r = client.put(
        "/api/datasources/fallbacks",
        json={"account_fallbacks": {"tw_broker": ["twse", "ghost"]}},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "validation_error"


def test_put_fallbacks_empty_chain_400(client: TestClient) -> None:
    r = client.put(
        "/api/datasources/fallbacks",
        json={"account_fallbacks": {"tw_broker": []}},
    )
    assert r.status_code == 400


def test_put_fallbacks_unknown_account_400(client: TestClient) -> None:
    r = client.put(
        "/api/datasources/fallbacks",
        json={"account_fallbacks": {"ghost_acct": ["twse"]}},
    )
    assert r.status_code == 400
