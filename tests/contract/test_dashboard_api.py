from pathlib import Path

from fastapi.testclient import TestClient

from portfolio_dash.ops import backup as backup_ops
from portfolio_dash.shared.config import get_settings


def test_dashboard_money_fields_are_strings(api_client: TestClient) -> None:
    r = api_client.get("/api/dashboard")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["kpis"]["total_market_value"], str)
    assert body["kpis"]["total_market_value"] == "639600"      # 2330 600k + AAPL 1200@33
    assert body["reporting_currency"] == "TWD"
    assert body["as_of"].startswith("2026-06-11T14:30")        # frozen clock, +08:00


def test_dashboard_holdings_enriched_and_llm_quota_present(api_client: TestClient) -> None:
    body = api_client.get("/api/dashboard").json()
    by_symbol = {h["symbol"]: h for h in body["holdings"]}
    assert by_symbol["2330"]["name"] == "TSMC"
    assert by_symbol["2330"]["market_value"] == "600000"
    assert isinstance(by_symbol["2330"]["spark_30d"], list)
    assert "llm_quota" in body


def test_dashboard_freshness_and_currency_kept_uppercase(api_client: TestClient) -> None:
    body = api_client.get("/api/dashboard").json()
    assert body["currency_view"]["by_currency_value"]["USD"] == "1200"   # Currency stays UPPER
    assert body["freshness"]["missing_prices"] == []


def test_dashboard_last_backup_at_none_when_no_backups(api_client: TestClient) -> None:
    # The test DB_PATH temp dir has no `backups/` subdir → reader returns None,
    # the router still surfaces the key (present, explicitly null).
    body = api_client.get("/api/dashboard").json()
    assert "last_backup_at" in body["freshness"]
    assert body["freshness"]["last_backup_at"] is None


def test_dashboard_last_backup_at_surfaces_latest_iso(api_client: TestClient) -> None:
    # Write a backup into the resolved default backup dir (<DB_PATH parent>/backups),
    # hermetic to this run's temp dir. The router must pass the ISO string through.
    backup_dir = get_settings().db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    gz = backup_dir / "portfolio_2026-06-15.db.gz"
    gz.write_bytes(b"stub-backup")
    try:
        expected = backup_ops.latest_backup_at()
        assert expected is not None  # populated case: an ISO string, not None
        assert expected.endswith("+00:00")  # timezone-aware UTC ISO-8601

        body = api_client.get("/api/dashboard").json()
        assert body["freshness"]["last_backup_at"] == expected
    finally:
        gz.unlink(missing_ok=True)


def test_latest_backup_at_newest_wins_and_none_paths(tmp_path: Path) -> None:
    # Missing dir → None.
    missing = tmp_path / "no_such_backups"
    assert backup_ops.latest_backup_at(missing) is None

    # Empty dir → None.
    empty = tmp_path / "empty"
    empty.mkdir()
    assert backup_ops.latest_backup_at(empty) is None

    # Two backups: the newest mtime wins.
    import os

    older = empty / "portfolio_2026-06-10.db.gz"
    newer = empty / "portfolio_2026-06-15.db.gz"
    older.write_bytes(b"old")
    newer.write_bytes(b"new")
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))

    result = backup_ops.latest_backup_at(empty)
    assert result is not None
    from datetime import UTC, datetime

    assert result == datetime.fromtimestamp(2_000_000, tz=UTC).isoformat()
