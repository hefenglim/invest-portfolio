"""Contract: 月度快照 (R6 item 8) + inbox count badge endpoint (item 4)."""

import sqlite3
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from portfolio_dash.api.snapshots import write_snapshot

_NOW = datetime(2026, 6, 11, 14, 30, tzinfo=UTC)


def test_snapshot_write_and_read(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    detail = write_snapshot(golden_db, now=_NOW)
    assert "2026-06" in detail
    body = api_client.get("/api/snapshots").json()
    assert body["total_count"] == 1
    row = body["rows"][0]
    assert row["month"] == "2026-06"
    assert row["reporting_ccy"] == "TWD"
    assert row["total_value"] is not None  # golden holds priced positions
    assert "TWD" in row["by_currency"]


def test_snapshot_upserts_same_month(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    write_snapshot(golden_db, now=_NOW)
    write_snapshot(golden_db, now=datetime(2026, 6, 30, 23, 50, tzinfo=UTC))
    body = api_client.get("/api/snapshots").json()
    assert body["total_count"] == 1  # one row per month, latest wins
    assert body["rows"][0]["as_of"].startswith("2026-06-30")


def test_inbox_count_endpoint(api_client: TestClient) -> None:
    r = api_client.get("/api/dividend-inbox/count")
    assert r.status_code == 200
    assert isinstance(r.json()["count"], int)
