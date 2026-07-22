"""GET /api/db-stats — read-only row-count statistics over BOTH SQLite files.

Owner decision (2026-07-07): surface per-table row counts + the OLDEST record date of
the append-only/large stores so the owner can OBSERVE growth and later decide retention
windows. Strictly read-only — NO pruning/cleanup lives here (deferred by decision).

Tables are enumerated from ``sqlite_master`` but described by a HAND-MAINTAINED registry
(zh label, category, date column) — unknown tables still appear in an 其他 group so
nothing hides. The news DB is a separate file (``news/store.news_db_path``); when the
file is absent that section degrades honestly (``present: false``) instead of creating it.
"""

import sqlite3
from pathlib import Path
from typing import Any, NamedTuple

from fastapi import APIRouter, Depends

from portfolio_dash.api.deps import get_conn
from portfolio_dash.news import store as news_store
from portfolio_dash.shared.config import get_settings

router = APIRouter()


class _TableSpec(NamedTuple):
    name: str
    label: str
    category: str
    date_col: str | None  # MIN(date_col) = oldest record (None -> no date shown)


# Hand-maintained registry: name -> zh label, category, oldest-record date column.
# Category order below (_PORTFOLIO_CATEGORIES) drives the display grouping.
_PORTFOLIO_REGISTRY: tuple[_TableSpec, ...] = (
    # 帳本 — the permanent sources of truth
    _TableSpec("transactions", "交易帳本", "帳本", "trade_date"),
    _TableSpec("dividends", "股利帳本", "帳本", "date"),
    _TableSpec("fx_conversions", "換匯帳本", "帳本", "date"),
    _TableSpec("opening_inventory", "期初庫存", "帳本", "build_date"),
    _TableSpec("cash_movements", "資金收支", "帳本", "date"),
    _TableSpec("accounts", "帳戶", "帳本", None),
    _TableSpec("instruments", "標的清單", "帳本", None),
    # 市場資料 — fetched quotes / rates / external datasets
    _TableSpec("prices", "每日收盤價", "市場資料", "as_of_date"),
    _TableSpec("fx_rates", "匯率", "市場資料", "as_of_date"),
    _TableSpec("dividend_events", "股利事件偵測", "市場資料", "ex_date"),
    _TableSpec("external_snapshots", "外部數據快照", "市場資料", "fetched_at"),
    _TableSpec("portfolio_snapshots", "月度 KPI 快照", "市場資料", "month"),
    # AI 記錄 — per-call / per-card AI history
    _TableSpec("llm_usage", "AI 請求明細", "AI 記錄", "ts"),
    _TableSpec("insights", "洞察卡", "AI 記錄", "created_at"),
    _TableSpec("insight_evaluations", "洞察評分", "AI 記錄", "evaluated_at"),
    _TableSpec("calibration_prompts", "校正提示詞版本", "AI 記錄", "created_at"),
    _TableSpec("llm_budget_events", "額度加值事件", "AI 記錄", "ts"),
    _TableSpec("insight_types", "洞察任務", "AI 記錄", None),
    _TableSpec("insight_type_strategies", "任務-策略關聯", "AI 記錄", None),
    _TableSpec("strategy_prompts", "策略提示詞", "AI 記錄", None),
    # 系統記錄 — operational history
    _TableSpec("job_runs", "排程執行紀錄", "系統記錄", "started_at"),
    _TableSpec("action_log", "系統操作記錄", "系統記錄", "ts"),
    _TableSpec("ledger_audit", "帳本操作稽核", "系統記錄", "at"),
    _TableSpec("alert_events", "預警事件", "系統記錄", "fired_at"),
    _TableSpec("alert_dispatch_log", "預警派發紀錄", "系統記錄", "dispatched_at"),
    _TableSpec("signal_states", "技術訊號狀態", "系統記錄", "updated_at"),
    _TableSpec("pending_dividend_skips", "配息略過記錄", "系統記錄", "skipped_at"),
    _TableSpec("rebate_skips", "折讓款略過記錄", "系統記錄", "skipped_at"),
    _TableSpec("digests", "摘要卡", "系統記錄", "digest_date"),
    _TableSpec("whatsnew_seen", "新功能已讀記錄", "系統記錄", "seen_at"),
    _TableSpec("auth_users", "授權用戶", "系統記錄", None),
    _TableSpec("auth_sessions", "登入工作階段", "系統記錄", None),
    # 設定 — small config stores (registered so they don't read as "unknown")
    _TableSpec("llm_models", "LLM 模型註冊表", "設定", None),
    _TableSpec("llm_defaults", "LLM 角色預設", "設定", None),
    _TableSpec("llm_quota_config", "LLM 額度設定", "設定", None),
    _TableSpec("schedule_config", "排程設定", "設定", None),
    _TableSpec("data_sources", "資料來源", "設定", None),
    _TableSpec("data_source_fallbacks", "資料源帳戶順位", "設定", None),
    _TableSpec("data_source_market_order", "資料源市場順位", "設定", None),
    _TableSpec("data_source_health", "資料源健康狀態", "設定", None),
    _TableSpec("alert_rules_config", "預警規則", "設定", None),
    _TableSpec("evolution_config", "AI 進化設定", "設定", None),
    _TableSpec("system_prompt_config", "系統提示詞", "設定", None),
    _TableSpec("news_prompt_config", "新聞整理提示詞", "設定", None),
    _TableSpec("target_weights_config", "目標配置", "設定", None),
    _TableSpec("notify_config", "通知設定", "設定", None),
    _TableSpec("digest_config", "摘要設定", "設定", None),
    _TableSpec("whatsnew_config", "新功能面板設定", "設定", None),
    _TableSpec("fee_rule_overrides", "費率調整", "設定", "updated_at"),
    _TableSpec("ui_prefs_config", "介面偏好", "設定", None),
    _TableSpec("account_market_rules", "帳戶市場規則", "設定", None),
    _TableSpec("settings_meta", "設定種子記錄", "設定", None),
)
_PORTFOLIO_CATEGORIES = ("帳本", "市場資料", "AI 記錄", "系統記錄", "設定")

_NEWS_REGISTRY: tuple[_TableSpec, ...] = (
    _TableSpec("organized_news", "整理後新聞", "新聞庫", "news_date"),
    _TableSpec("news_mentions", "新聞標的索引", "新聞庫", None),
)
_NEWS_CATEGORIES = ("新聞庫",)

_OTHER = "其他"


def _table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [str(r["name"]) for r in rows]


def _count(conn: sqlite3.Connection, table: str) -> int:
    # `table` comes from sqlite_master / the registry (never user input).
    row = conn.execute(f'SELECT COUNT(*) AS n FROM "{table}"').fetchone()
    return int(row["n"])


def _oldest(conn: sqlite3.Connection, table: str, col: str) -> str | None:
    """MIN(date/ts column) as stored, or None (empty table / legacy column drift)."""
    try:
        row = conn.execute(f'SELECT MIN("{col}") AS m FROM "{table}"').fetchone()
    except sqlite3.Error:
        return None  # legacy DB missing the column — degrade, never 500
    value = row["m"]
    return str(value)[:19] if value is not None else None


def _groups(
    conn: sqlite3.Connection,
    registry: tuple[_TableSpec, ...],
    categories: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Registry-described tables grouped by category + an 其他 group for unknowns."""
    present = set(_table_names(conn))
    known = {spec.name for spec in registry}
    by_category: dict[str, list[dict[str, Any]]] = {c: [] for c in categories}
    for spec in registry:
        if spec.name not in present:
            continue  # registered but not created on this DB (older install) — skip
        by_category[spec.category].append({
            "name": spec.name,
            "label": spec.label,
            "count": _count(conn, spec.name),
            "oldest": _oldest(conn, spec.name, spec.date_col) if spec.date_col else None,
        })
    groups = [
        {"category": c, "tables": tables}
        for c, tables in by_category.items()
        if tables
    ]
    unknown = sorted(present - known)
    if unknown:
        groups.append({
            "category": _OTHER,
            "tables": [
                {"name": n, "label": n, "count": _count(conn, n), "oldest": None}
                for n in unknown
            ],
        })
    return groups


def _size_bytes(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None  # file absent (e.g. in-memory test DB) — honest null, never fabricate


@router.get("/db-stats")
def db_stats(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    """Read-only statistics of ALL data categories across BOTH SQLite files.

    ``portfolio`` reads the request connection; ``news`` opens the separate news DB
    only when its file exists (absent -> ``present: false`` with empty groups — the
    stats endpoint must never CREATE the news DB as a side effect).
    """
    db_path = get_settings().db_path
    portfolio: dict[str, Any] = {
        "file": db_path.name,
        "size_bytes": _size_bytes(db_path),
        "groups": _groups(conn, _PORTFOLIO_REGISTRY, _PORTFOLIO_CATEGORIES),
    }
    news_path = news_store.news_db_path()
    news: dict[str, Any] = {
        "file": news_path.name,
        "present": news_path.exists(),
        "size_bytes": None,
        "groups": [],
    }
    if news["present"]:
        news["size_bytes"] = _size_bytes(news_path)
        with news_store.news_session() as news_conn:
            news["groups"] = _groups(news_conn, _NEWS_REGISTRY, _NEWS_CATEGORIES)
    return {"portfolio": portfolio, "news": news}


__all__ = ["router"]
