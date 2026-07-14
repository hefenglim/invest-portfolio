import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from portfolio_dash.llm_insight import insights_store
from portfolio_dash.llm_insight.cards import InsightCard
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


def test_dashboard_llm_quota_carries_ai_active_false_on_golden(
    api_client: TestClient,
) -> None:
    """3B: llm_quota exposes ai_active (smallest honest surface for the quota chip). The
    golden DB is AI-off (no model bound to any role) -> ai_active is False."""
    body = api_client.get("/api/dashboard").json()
    assert body["llm_quota"]["ai_active"] is False


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


# --- insights embed (spec 08/04 I3) -------------------------------------------

_TPE = ZoneInfo("Asia/Taipei")


def _seed_card(
    conn: sqlite3.Connection, *, insight_type_id: int, title: str, symbol: str | None,
    when: datetime, cost: str = "0.0021", is_shadow: bool = False,
) -> None:
    card = InsightCard(title=title, summary=f"{title} summary", body_md=f"# {title}",
                       tags=[], symbol=symbol)
    insights_store.add_card(
        conn, insight_type_id=insight_type_id, card=card,
        fingerprint=insights_store.fingerprint(insight_type_id, title, "d", "v1"),
        calibration_version=None, horizon_days=5, input_snapshot="{}", model="m",
        cost_usd=Decimal(cost), now=when, is_shadow=is_shadow,
    )


def test_dashboard_insights_empty_when_no_cards(api_client: TestClient) -> None:
    # The golden DB seeds the insights table EMPTY -> the dashboard embeds [].
    body = api_client.get("/api/dashboard").json()
    assert body["insights"] == []


def test_dashboard_embeds_real_cards_newest_first(
    golden_db: sqlite3.Connection, api_client: TestClient
) -> None:
    # golden_db and api_client share the same in-memory connection: seed, then GET.
    _seed_card(golden_db, insight_type_id=1, title="older", symbol="2330",
               when=datetime(2026, 6, 9, 9, 0, tzinfo=_TPE))
    _seed_card(golden_db, insight_type_id=1, title="newer", symbol=None,
               when=datetime(2026, 6, 10, 9, 0, tzinfo=_TPE), cost="0.0050")
    # a shadow card on the newest day must NOT surface
    _seed_card(golden_db, insight_type_id=1, title="shadow", symbol="AAPL",
               when=datetime(2026, 6, 11, 9, 0, tzinfo=_TPE), is_shadow=True)

    body = api_client.get("/api/dashboard").json()
    insights = body["insights"]
    assert [c["title"] for c in insights] == ["newer", "older"]  # newest first, shadow gone

    newer = insights[0]
    assert set(newer.keys()) == {
        "id", "title", "summary", "body_md", "symbol", "created_at", "cost_usd",
        # AI attribution (2026-07-07): every LLM output carries model + token usage.
        "model", "tokens_in", "tokens_out",
    }
    assert newer["summary"] == "newer summary"
    assert newer["body_md"] == "# newer"
    assert newer["symbol"] is None  # portfolio card -> null symbol passes through
    assert isinstance(newer["id"], str)  # id stringified to match the stub wire type
    assert isinstance(newer["cost_usd"], str)  # money is a STRING, never a number
    assert newer["cost_usd"] == "0.0050"  # canonical Decimal string, untouched
    assert insights[1]["symbol"] == "2330"


def test_dashboard_insights_capped_at_three(
    golden_db: sqlite3.Connection, api_client: TestClient
) -> None:
    for i in range(5):
        _seed_card(golden_db, insight_type_id=1, title=f"c{i}", symbol="2330",
                   when=datetime(2026, 6, 6 + i, 9, 0, tzinfo=_TPE))
    body = api_client.get("/api/dashboard").json()
    assert [c["title"] for c in body["insights"]] == ["c4", "c3", "c2"]  # latest 3


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
