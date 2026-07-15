"""System action log (系統操作記錄, 2026-07-03 item 8).

Records every MUTATING ``/api/*`` request (POST/PUT/DELETE) so the user can see
exactly what the system did and when: timestamp, actor, a Chinese action label,
the endpoint, HTTP outcome, and duration. Request/response BODIES are never
stored (passwords, ledger amounts, API keys stay out of the log by design —
the ledgers themselves are the record for financial data).

Previews/what-ifs are excluded (they compute, they do not change state), so the
log reads as "things that happened", not request noise. The table is pruned to
the newest ``_KEEP`` rows on insert. Written by the app middleware (best-effort:
a logging failure never breaks the request).
"""

import sqlite3
from datetime import datetime

_KEEP = 5000

_DDL = """
CREATE TABLE IF NOT EXISTS action_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    username TEXT,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    action TEXT NOT NULL,
    status INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL
);
"""

# Paths that mutate nothing (pure computation / auth chatter): not logged.
_EXCLUDED_PREFIXES = (
    "/api/input/manual/preview",
    "/api/input/ai/preview",
    "/api/import/preview",
    "/api/prompts/preview",
    "/api/rebalance/preview",
    "/api/whatif",
    "/api/auth/session",
)

# Ordered (method, path-prefix, label) — first match wins. Labels are the
# user-facing Chinese action names shown in 設定 › 排程 › 系統操作記錄.
_LABELS: list[tuple[str, str, str]] = [
    ("POST", "/api/input/manual/commit", "手動交易寫入"),
    ("POST", "/api/import/commit", "匯入寫入（CSV / 單筆表單）"),
    ("POST", "/api/input/ai", "AI 輸入解析"),
    ("POST", "/api/cash/movements", "入金／出金"),
    ("PUT", "/api/cash/movements", "現金紀錄更正"),
    ("DELETE", "/api/cash/movements", "現金紀錄刪除"),
    ("POST", "/api/cash/fx", "資金換匯"),
    ("POST", "/api/dividend-inbox/confirm", "配息確認入帳"),
    ("POST", "/api/dividend-inbox/skip", "配息略過"),
    ("POST", "/api/rebates/confirm", "折讓款確認入帳"),
    ("POST", "/api/rebates/skip", "折讓款略過"),
    ("POST", "/api/instruments/quick", "一步新增標的"),
    ("POST", "/api/instruments/probe", "板別探測"),
    ("POST", "/api/instruments", "註冊標的"),
    ("PUT", "/api/instruments/", "編輯標的"),
    ("PUT", "/api/ledgers/", "帳本更正"),
    ("DELETE", "/api/ledgers/", "帳本刪除"),
    ("POST", "/api/actions/refresh-quotes", "手動更新報價"),
    ("POST", "/api/actions/recompute", "重算（重建統計）"),
    ("POST", "/api/actions/backfill-history", "歷史報價回補"),
    ("POST", "/api/scheduler/jobs", "排程手動執行"),
    ("PUT", "/api/scheduler/jobs", "排程設定變更"),
    ("PUT", "/api/datasources/order", "資料源順位調整"),
    ("PUT", "/api/datasources/", "資料源設定變更"),
    ("POST", "/api/datasources/", "資料源操作"),
    ("POST", "/api/export/", "匯出報表"),
    ("POST", "/api/auth/login", "登入"),
    ("POST", "/api/auth/logout", "登出"),
    ("POST", "/api/auth/lock", "鎖定畫面"),
    ("POST", "/api/users", "授權用戶管理"),
    ("DELETE", "/api/users", "授權用戶刪除"),
    ("PUT", "/api/alert-rules", "警示規則變更"),
    ("PUT", "/api/evolution-config", "AI 進化設定變更"),
    ("PUT", "/api/system-prompt", "系統提示詞變更"),
    ("POST", "/api/insight-types", "AI 洞察任務操作"),
    ("PUT", "/api/llm/", "LLM 設定變更"),
    ("POST", "/api/llm/", "LLM 操作"),
]


def should_log(method: str, path: str) -> bool:
    """True when the request is a state-changing /api call worth recording."""
    if method not in ("POST", "PUT", "DELETE"):
        return False
    if not path.startswith("/api/"):
        return False
    return not any(path.startswith(p) for p in _EXCLUDED_PREFIXES)


def label_for(method: str, path: str) -> str:
    """The user-facing Chinese action label for a request (fallback: raw path)."""
    for m, prefix, label in _LABELS:
        if method == m and path.startswith(prefix):
            return label
    return f"{method} {path}"


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    conn.commit()


def record(
    conn: sqlite3.Connection,
    *,
    ts: datetime,
    username: str | None,
    method: str,
    path: str,
    status: int,
    duration_ms: int,
) -> None:
    """Insert one action row and prune the table to the newest ``_KEEP`` rows."""
    ensure_table(conn)
    conn.execute(
        "INSERT INTO action_log (ts, username, method, path, action, status, duration_ms) "
        "VALUES (?,?,?,?,?,?,?)",
        (ts.isoformat(), username, method, path, label_for(method, path), status,
         duration_ms),
    )
    conn.execute(
        "DELETE FROM action_log WHERE id NOT IN "
        "(SELECT id FROM action_log ORDER BY id DESC LIMIT ?)",
        (_KEEP,),
    )
    conn.commit()


def list_actions(
    conn: sqlite3.Connection, *, limit: int = 100, offset: int = 0
) -> dict[str, object]:
    """Newest-first page of action rows + total count (wire-ready dict)."""
    ensure_table(conn)
    total = conn.execute("SELECT COUNT(*) AS n FROM action_log").fetchone()["n"]
    rows = conn.execute(
        "SELECT ts, username, method, path, action, status, duration_ms "
        "FROM action_log ORDER BY id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return {
        "rows": [
            {
                "ts": r["ts"], "username": r["username"], "method": r["method"],
                "path": r["path"], "action": r["action"], "status": r["status"],
                "duration_ms": r["duration_ms"],
            }
            for r in rows
        ],
        "total_count": int(total),
    }
