import sqlite3
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import insert_transaction
from portfolio_dash.pricing.registry import Registry
from portfolio_dash.scheduler import jobs
from portfolio_dash.shared.models.enums import Side


@pytest.fixture
def _hermetic_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make refresh-quotes jobs network-free.

    The ``api_client`` fixture calls ``enable_socket()`` (a Windows TestClient
    self-pipe workaround) which re-enables ALL outbound sockets, so the real
    ``default_registry()`` would let the refresh jobs hit live TWSE/yfinance
    (spec 17.3 bans network in tests). Patch the factory the jobs module looks up
    to return an empty ``Registry``: its provider chain is empty, so no provider —
    and therefore no HTTP — is ever invoked. ``refresh_quotes`` still returns a
    ``RefreshSummary`` (everything ``failed``) and ``run_job`` logs it, so the
    endpoint contract (200 + ``jobs`` + ``run_ids``) is unchanged.
    """
    monkeypatch.setattr(
        jobs, "default_registry", lambda conn=None: Registry(providers={}, order={})
    )


def test_refresh_quotes_all_markets(api_client: TestClient, _hermetic_registry: None) -> None:
    r = api_client.post("/api/actions/refresh-quotes", json={})
    assert r.status_code == 200
    b = r.json()
    assert set(b["jobs"]) == {"quotes_tw", "quotes_us", "quotes_my"}
    assert len(b["run_ids"]) == 3 and all(isinstance(x, int) for x in b["run_ids"])


def test_refresh_quotes_subset(api_client: TestClient, _hermetic_registry: None) -> None:
    r = api_client.post("/api/actions/refresh-quotes", json={"markets": ["TW"]})
    assert r.status_code == 200
    assert r.json()["jobs"] == ["quotes_tw"] and len(r.json()["run_ids"]) == 1


def test_refresh_quotes_unknown_market_400(api_client: TestClient) -> None:
    r = api_client.post("/api/actions/refresh-quotes", json={"markets": ["XX"]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "validation_error"


def test_recompute_ok(api_client: TestClient) -> None:
    r = api_client.post("/api/actions/recompute", json={})
    assert r.status_code == 200
    b = r.json()
    assert b["rebuilt"] is True and "as_of" in b


def test_recompute_oversell_422(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    # Seed a sell exceeding AAPL holdings (10 held) directly via the store insert
    # helper (no soft-issue guard there), so replaying the ledger raises OversellError.
    insert_transaction(golden_db, account_id="schwab", symbol="AAPL", side=Side.SELL,
                       quantity=Decimal("9999"), price=Decimal("100"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 2, 1))
    r = api_client.post("/api/actions/recompute", json={})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "oversell"
