"""Contract tests for the data-source management API (spec 14).

Self-contained (does NOT use the shared ``api_client``/conftest fixtures): a local
in-memory connection bootstrapped with the ledger tables + the three data_sources
tables + seeded accounts, and a local FastAPI app mounting ONLY the datasources
router. The ``/test`` endpoint's probe is monkeypatched so the hermetic test never
touches the network (a real provider call only happens in production).
"""

import sqlite3
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
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
from portfolio_dash.pricing import sentiment_source
from portfolio_dash.pricing.providers.finmind_provider import FinMindProvider
from portfolio_dash.pricing.providers.tpex_provider import TpexProvider
from portfolio_dash.pricing.providers.twse_provider import TwseProvider
from portfolio_dash.pricing.providers.yfinance_provider import YFinanceProvider
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.shared.enums import Market

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


def test_get_includes_tier_and_tiers(client: TestClient) -> None:
    by_id = {s["id"]: s for s in client.get("/api/datasources").json()["sources"]}
    # tier (current marking) defaults to null; tiers lists the selectable options.
    assert by_id["finmind"]["tier"] is None
    assert by_id["finmind"]["tiers"] == ["free", "backer", "sponsor", "sponsorpro"]
    assert by_id["alphavantage"]["tiers"] == ["free", "premium"]
    # auth:"none" sources have no selectable tiers.
    assert by_id["twse"]["tiers"] is None
    assert by_id["twse"]["tier"] is None


# --- PUT /api/datasources/{id}/tier -------------------------------------------


def test_put_tier_sets_and_get_reflects(client: TestClient) -> None:
    r = client.put("/api/datasources/finmind/tier", json={"tier": "backer"})
    assert r.status_code == 200
    assert r.json()["id"] == "finmind" and r.json()["tier"] == "backer"
    by_id = {s["id"]: s for s in client.get("/api/datasources").json()["sources"]}
    assert by_id["finmind"]["tier"] == "backer"


def test_put_tier_unknown_tier_400(client: TestClient) -> None:
    r = client.put("/api/datasources/finmind/tier", json={"tier": "platinum"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "validation_error"


def test_put_tier_on_auth_none_source_400(client: TestClient) -> None:
    r = client.put("/api/datasources/twse/tier", json={"tier": "free"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "validation_error"


def test_put_tier_unknown_source_404(client: TestClient) -> None:
    r = client.put("/api/datasources/nope/tier", json={"tier": "free"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_get_includes_market_order_and_available(client: TestClient) -> None:
    """Item 9 (2026-07-03): the wire carries the REAL per-market quote chain +
    the capability pick list (supersedes the per-account fallback wire)."""
    body = client.get("/api/datasources").json()
    mo = body["market_order"]
    # Defaults mirror DEFAULT_PROVIDER_ORDER (spec 20.8 free fallbacks appended).
    assert mo["TW"] == ["twse", "tpex", "yfinance", "twstock"]
    assert mo["US"] == ["yfinance", "stockprices_dev"]
    assert mo["MY"] == ["yfinance", "klsescreener", "malaysiastock"]
    avail = body["market_order_available"]
    assert set(mo["TW"]).issubset(set(avail["TW"]))
    assert set(mo["MY"]).issubset(set(avail["MY"]))


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


# --- PUT /api/datasources/market-order ------------------------------------------


def test_put_market_order_persists_and_registry_honors_it(client: TestClient) -> None:
    """The stored order is the REAL chain: default_registry(conn) must walk it."""
    r = client.put(
        "/api/datasources/market-order",
        json={"market": "TW", "order": ["yfinance", "twse"]},
    )
    assert r.status_code == 200
    assert r.json()["market_order"]["TW"] == ["yfinance", "twse"]
    # Persisted: GET reflects the new chain.
    mo = client.get("/api/datasources").json()["market_order"]
    assert mo["TW"] == ["yfinance", "twse"]


def test_put_market_order_unknown_source_400(client: TestClient) -> None:
    r = client.put(
        "/api/datasources/market-order",
        json={"market": "TW", "order": ["twse", "ghost"]},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "validation_error"


def test_put_market_order_incapable_source_400(client: TestClient) -> None:
    # twse only quotes TW — putting it into the US chain must be refused.
    r = client.put(
        "/api/datasources/market-order",
        json={"market": "US", "order": ["twse"]},
    )
    assert r.status_code == 400


def test_put_market_order_empty_400(client: TestClient) -> None:
    r = client.put(
        "/api/datasources/market-order", json={"market": "TW", "order": []}
    )
    assert r.status_code == 400


def test_put_market_order_duplicates_400(client: TestClient) -> None:
    r = client.put(
        "/api/datasources/market-order",
        json={"market": "TW", "order": ["twse", "twse"]},
    )
    assert r.status_code == 400


# --- probe_source wiring (primary live sources now have a real probe) ----------


def _fake_price(symbol: str) -> PriceRow:
    return PriceRow(
        instrument=symbol, market=Market.TW, as_of=date(2026, 6, 12),
        close=Decimal("100"), source="test",
    )


@pytest.mark.parametrize(
    "source_id, provider_cls",
    [("yfinance", YFinanceProvider), ("twse", TwseProvider), ("tpex", TpexProvider)],
)
def test_probe_quote_sources_are_wired(
    monkeypatch: pytest.MonkeyPatch, source_id: str, provider_cls: type
) -> None:
    """The primary key-less quote sources dispatch to a real provider call (no stub)."""
    monkeypatch.setattr(
        provider_cls, "fetch_quote_latest", lambda self, refs: [_fake_price(refs[0].symbol)]
    )
    ok, detail = ds_router.probe_source(source_id, None)
    assert ok is True
    assert detail is not None and "尚未實作" not in detail


def test_probe_finmind_with_key_is_wired(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FinMindProvider, "fetch_dividends", lambda self, refs: [])
    ok, detail = ds_router.probe_source("finmind", "fm-key")
    assert ok is True
    assert detail is not None and "尚未實作" not in detail


def test_probe_finmind_without_key_reports_missing() -> None:
    ok, detail = ds_router.probe_source("finmind", None)
    assert ok is False
    assert detail == "尚未設定金鑰"


def test_probe_fx_ecb_is_pending_not_stub() -> None:
    ok, detail = ds_router.probe_source("fx_ecb", None)
    assert ok is False
    assert detail == "待測試（尚未線上驗證）"


def test_no_live_source_falls_through_to_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression guard: every ``live`` source must have a wired probe. Mock all network
    boundaries so no live source reaches the ``尚未實作連線測試`` fallback."""
    monkeypatch.setattr(ds_router, "_probe_quote_provider", lambda prov, ref: (True, "mock"))
    monkeypatch.setattr(FinMindProvider, "fetch_dividends", lambda self, refs: [])
    monkeypatch.setattr(sentiment_source, "fetch_fear_greed", lambda: {"score": 50})
    for info in store.SOURCE_INFO:
        if info.status != "live":
            continue
        key = "k" if info.auth in ("apikey", "oauth") else None
        _ok, detail = ds_router.probe_source(info.id, key)
        assert detail != "尚未實作連線測試", f"{info.id} still returns the not-implemented stub"


def test_fx_ecb_wire_status_is_pending(client: TestClient) -> None:
    by_id = {s["id"]: s for s in client.get("/api/datasources").json()["sources"]}
    assert by_id["fx_ecb"]["status"] == "pending"
