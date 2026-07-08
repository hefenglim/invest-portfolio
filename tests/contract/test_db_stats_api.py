"""Contract: GET /api/db-stats — read-only row-count statistics over both DB files.

Owner decision (2026-07-07): observation only (retention windows decided later) — the
endpoint must never write/prune anything and must not CREATE the news DB when absent.
The hermetic golden DB seeds 2 transactions / 1 dividend / 1 fx conversion, so those
counts + the oldest trade date are exact oracles.
"""

from fastapi.testclient import TestClient


def _get(api_client: TestClient) -> dict[str, object]:
    r = api_client.get("/api/db-stats")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, dict)
    return dict(body)


def _tables_by_name(section: dict[str, object]) -> dict[str, dict[str, object]]:
    out: dict[str, dict[str, object]] = {}
    groups = section["groups"]
    assert isinstance(groups, list)
    for g in groups:
        assert isinstance(g, dict)
        for t in g["tables"]:
            out[str(t["name"])] = dict(t)
    return out


def test_portfolio_counts_and_oldest_dates(api_client: TestClient) -> None:
    body = _get(api_client)
    portfolio = body["portfolio"]
    assert isinstance(portfolio, dict)
    tables = _tables_by_name(portfolio)
    # Golden ledger oracles (seeded via the real write paths).
    assert tables["transactions"]["count"] == 2
    assert tables["transactions"]["oldest"] == "2026-01-05"
    assert tables["dividends"]["count"] == 1
    assert tables["fx_conversions"]["count"] == 1
    assert tables["prices"]["count"] == 2
    assert tables["prices"]["oldest"] == "2026-06-09"
    # Append-only AI/system stores appear even when empty (count 0, oldest null).
    assert tables["llm_usage"]["count"] == 0
    assert tables["llm_usage"]["oldest"] is None
    assert tables["job_runs"]["count"] == 0
    # Every count is an int; oldest is str-or-None (never a fabricated date).
    for t in tables.values():
        assert isinstance(t["count"], int)
        assert t["oldest"] is None or isinstance(t["oldest"], str)


def test_categories_grouping_and_labels(api_client: TestClient) -> None:
    body = _get(api_client)
    portfolio = body["portfolio"]
    assert isinstance(portfolio, dict)
    groups = portfolio["groups"]
    assert isinstance(groups, list)
    cats = [g["category"] for g in groups]
    for expected in ("帳本", "市場資料", "AI 記錄", "系統記錄", "設定"):
        assert expected in cats, f"missing category {expected}: {cats!r}"
    # zh labels ride along with the raw table name.
    tables = _tables_by_name(portfolio)
    assert tables["transactions"]["label"] == "交易帳本"
    assert tables["llm_usage"]["label"] == "AI 請求明細"


def test_file_sizes_are_numbers_or_null(api_client: TestClient) -> None:
    body = _get(api_client)
    portfolio = body["portfolio"]
    news = body["news"]
    assert isinstance(portfolio, dict) and isinstance(news, dict)
    # The hermetic client runs on an in-memory conn; the configured file may be absent.
    assert portfolio["size_bytes"] is None or isinstance(portfolio["size_bytes"], int)
    assert news["size_bytes"] is None or isinstance(news["size_bytes"], int)
    assert isinstance(news["present"], bool)
    if not news["present"]:
        # Honest degradation: absent file -> no groups, and it must NOT be created.
        assert news["groups"] == []
        assert news["size_bytes"] is None


def test_db_stats_is_read_only(api_client: TestClient) -> None:
    """Calling the stats endpoint twice must not change any count (no writes)."""
    p1 = _get(api_client)["portfolio"]
    p2 = _get(api_client)["portfolio"]
    assert isinstance(p1, dict) and isinstance(p2, dict)
    first = _tables_by_name(p1)
    second = _tables_by_name(p2)
    assert {k: v["count"] for k, v in first.items()} == {
        k: v["count"] for k, v in second.items()
    }
