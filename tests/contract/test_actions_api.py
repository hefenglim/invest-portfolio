import sqlite3
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import insert_transaction
from portfolio_dash.shared.models.enums import Side


def test_refresh_quotes_all_markets(api_client: TestClient) -> None:
    r = api_client.post("/api/actions/refresh-quotes", json={})
    assert r.status_code == 200
    b = r.json()
    assert set(b["jobs"]) == {"quotes_tw", "quotes_us", "quotes_my"}
    assert len(b["run_ids"]) == 3 and all(isinstance(x, int) for x in b["run_ids"])


def test_refresh_quotes_subset(api_client: TestClient) -> None:
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
