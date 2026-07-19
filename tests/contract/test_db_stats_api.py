"""Contract: GET /api/db-stats — read-only row-count statistics over both DB files.

Owner decision (2026-07-07): observation only (retention windows decided later) — the
endpoint must never write/prune anything and must not CREATE the news DB when absent.
The hermetic golden DB seeds 2 transactions / 1 dividend / 1 fx conversion, so those
counts + the oldest trade date are exact oracles.
"""

import sqlite3

from fastapi.testclient import TestClient

from portfolio_dash.api.routers.db_stats import _PORTFOLIO_REGISTRY


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


# --- registry completeness guard (FU-D8) -----------------------------------------------------
# Build the FULL portfolio-DB schema the running app can create — EVERY ensure/create helper,
# including the lazily-created tables (fee_rule_overrides / rebate_skips / pending_dividend_skips
# / action_log / portfolio_snapshots) that no startup path touches until their first write — then
# assert every enumerated table is described by the db-stats registry. A NEW table that ships
# without a registry entry lands in this failing test instead of the silent 其他 bucket.


def _build_full_portfolio_schema(conn: sqlite3.Connection) -> None:
    """Create every table the app persists in portfolio.db (news.db tables are separate)."""
    from portfolio_dash.api.action_log import ensure_table as ensure_action_log_table
    from portfolio_dash.api.auth_store import create_auth_tables
    from portfolio_dash.api.dividend_inbox import ensure_tables as ensure_dividend_inbox_tables
    from portfolio_dash.api.rebates import ensure_tables as ensure_rebate_tables
    from portfolio_dash.api.snapshots import ensure_table as ensure_snapshots_table
    from portfolio_dash.bootstrap import bootstrap_db
    from portfolio_dash.data_ingestion.fee_overrides import ensure_tables as ensure_fee_tables
    from portfolio_dash.llm_insight.alerts_bridge import ensure_tables as ensure_alert_events
    from portfolio_dash.llm_insight.composer_store import ensure_seeded as ensure_composer_seeded
    from portfolio_dash.llm_insight.evaluations_store import ensure_tables as ensure_evaluations
    from portfolio_dash.llm_insight.insights_store import ensure_tables as ensure_insights_tables
    from portfolio_dash.llm_insight.system_prompt import ensure_system_prompt_seeded
    from portfolio_dash.news.organizer_prompt import ensure_news_prompt_seeded
    from portfolio_dash.ops import digest as digest_ops
    from portfolio_dash.ops import notify as notify_ops
    from portfolio_dash.pricing import datasources_store, snapshots_store
    from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
    from portfolio_dash.scheduler.jobs import create_scheduler_tables
    from portfolio_dash.shared.ui_prefs import ensure_ui_prefs_seeded
    from portfolio_dash.shared.whatsnew import ensure_whatsnew_seeded
    from portfolio_dash.strategy.rules_config import ensure_alert_rules_seeded
    from portfolio_dash.strategy.signal_states import ensure_table as ensure_signal_states_table
    from portfolio_dash.strategy.target_weights import ensure_target_weights_seeded

    bootstrap_db(conn)  # ledger + ledger_audit + LLM config tables
    create_pricing_tables(conn)  # prices / fx_rates / dividend_events
    create_scheduler_tables(conn)  # schedule_config / job_runs
    snapshots_store.ensure_tables(conn)  # external_snapshots
    datasources_store.ensure_seeded(conn)  # data_sources* (+ settings_meta)
    ensure_alert_rules_seeded(conn)  # alert_rules_config
    ensure_signal_states_table(conn)  # signal_states
    ensure_target_weights_seeded(conn)  # target_weights_config
    ensure_composer_seeded(conn)  # strategy_prompts / insight_types / ... / evolution_config
    ensure_insights_tables(conn)  # insights
    ensure_alert_events(conn)  # alert_events / alert_dispatch_log
    ensure_evaluations(conn)  # insight_evaluations
    ensure_system_prompt_seeded(conn)  # system_prompt_config
    ensure_news_prompt_seeded(conn)  # news_prompt_config (lives in the portfolio DB)
    create_auth_tables(conn)  # auth_users / auth_sessions
    ensure_ui_prefs_seeded(conn)  # ui_prefs_config
    ensure_whatsnew_seeded(conn)  # whatsnew_config / whatsnew_seen
    notify_ops.ensure_seeded(conn)  # notify_config
    digest_ops.ensure_seeded(conn)  # digests / digest_config
    # Lazily-created tables (no startup path builds these — trigger their ensure/create helper):
    ensure_action_log_table(conn)  # action_log
    ensure_dividend_inbox_tables(conn)  # pending_dividend_skips
    ensure_rebate_tables(conn)  # rebate_skips
    ensure_fee_tables(conn)  # fee_rule_overrides
    ensure_snapshots_table(conn)  # portfolio_snapshots


def test_every_portfolio_table_is_registered() -> None:
    """Every table the app creates in portfolio.db must be described by the registry.

    Guard against the 其他 (unknown) bucket: a future table added without a matching
    ``_TableSpec`` fails HERE (a named, actionable failure) rather than silently degrading
    the stats panel to a raw name under 其他.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        _build_full_portfolio_schema(conn)
        present = {
            str(r["name"])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
    finally:
        conn.close()

    registered = {spec.name for spec in _PORTFOLIO_REGISTRY}
    unregistered = present - registered
    assert not unregistered, (
        f"{len(unregistered)} portfolio table(s) not in _PORTFOLIO_REGISTRY (they would fall "
        f"into 其他): {sorted(unregistered)} — add a _TableSpec for each in db_stats.py"
    )
    # Sanity: the FU-D8 additions actually materialized in the built schema.
    for name in (
        "ledger_audit", "target_weights_config", "whatsnew_config", "whatsnew_seen",
        "rebate_skips", "notify_config", "digests", "digest_config", "fee_rule_overrides",
    ):
        assert name in present, f"expected {name} in the built schema"
        assert name in registered, f"expected {name} registered in db_stats.py"
