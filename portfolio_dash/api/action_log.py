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
# ⚠ Every ``*/preview`` endpoint belongs here — ``tests/contract/test_action_log_labels.py``
# enumerates the router table and fails on one that is missing. Five previews were listed and
# ``/api/ledgers/corporate-actions/preview`` was not (found 2026-09-02); that form previews on
# a DEBOUNCE, so one 補登 wrote a burst of phantom "operations" into a log whose own docstring
# promises previews are excluded.
_EXCLUDED_PREFIXES = (
    "/api/input/manual/preview",
    "/api/input/ai/preview",
    "/api/import/preview",
    "/api/prompts/preview",
    "/api/rebalance/preview",
    "/api/ledgers/corporate-actions/preview",
    "/api/whatif",
    "/api/auth/session",
)

# Ordered (method, path-prefix, label) — first match wins. Labels are the
# user-facing Chinese action names shown in 設定 › 排程 › 系統操作記錄.
#
# ⚠ This table must cover EVERY mutating ``/api/*`` route the app registers: the fallback in
# ``label_for`` prints the raw English path, so a missing row shows the owner
# ``POST /api/news/run`` where the row above it says 「手動更新報價」. It is not maintained by
# memory — ``tests/contract/test_action_log_labels.py`` walks FastAPI's own route table and
# fails, naming the endpoint, the day a new one lands without a label (2026-09-02: 43 routes
# had drifted out, four of them user-visible).
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
    # --- 2026-09-02 sweep: the endpoints that had drifted out of the table -------------
    # spec 07 §7.0 makes ``/api/insight-tasks/*`` a FULL alias of ``/api/insight-types/*``
    # (one handler, two paths). The label must therefore be the SAME string for both, or the
    # log records which PAGE fired the click instead of what happened — which is exactly how
    # 「AI 洞察任務操作」 and ``POST /api/insight-tasks/official-pack`` came to sit in one list.
    ("POST", "/api/insight-tasks", "AI 洞察任務操作"),
    ("PUT", "/api/insight-types", "AI 洞察任務設定變更"),
    ("PUT", "/api/insight-tasks", "AI 洞察任務設定變更"),
    ("DELETE", "/api/insight-types", "AI 洞察任務刪除"),
    ("DELETE", "/api/insight-tasks", "AI 洞察任務刪除"),
    ("POST", "/api/calibrations/", "校正版本封存"),
    ("POST", "/api/strategy-prompts", "策略模板新增"),
    ("PUT", "/api/strategy-prompts", "策略模板變更"),
    ("DELETE", "/api/strategy-prompts", "策略模板刪除"),
    # 公司行動 write door (its /preview sibling is excluded above, so the order is safe).
    ("POST", "/api/ledgers/corporate-actions", "公司行動新增"),
    ("DELETE", "/api/instruments", "刪除標的"),
    ("DELETE", "/api/import/batches/", "匯入批次刪除"),
    ("POST", "/api/broker/convert", "券商匯出檔轉換"),
    ("POST", "/api/dividend-inbox/unskip", "配息取消略過"),
    ("POST", "/api/rebates/unskip", "折讓款取消略過"),
    # reset-all first: it also matches the generic /api/fee-rules/ prefix below.
    ("POST", "/api/fee-rules/reset-all", "費稅規則全部重設"),
    ("POST", "/api/fee-rules/", "費稅規則重設"),
    ("PUT", "/api/fee-rules/", "費稅規則變更"),
    # llm-fail-log's export lives OUTSIDE export.py on purpose (its router docstring: to stay
    # clear of the test_export_endpoints_have_callers contract), which is precisely why the
    # "/api/export/" prefix above never reached it. All 15 exports now read 「匯出報表」.
    ("POST", "/api/llm-fail-log/export", "匯出報表"),
    ("DELETE", "/api/llm-fail-log", "AI 失敗紀錄清除"),
    ("DELETE", "/api/llm/", "LLM 設定刪除"),
    ("POST", "/api/news/run", "新聞抓取"),
    ("PUT", "/api/news-prompt", "新聞提示詞變更"),
    ("POST", "/api/news-prompt/reset", "新聞提示詞重設"),
    ("POST", "/api/system-prompt/reset", "系統提示詞重設"),
    ("POST", "/api/prompts/test", "提示詞測試"),
    ("PUT", "/api/digest/config", "摘要設定變更"),
    ("POST", "/api/digest/run", "摘要手動產生"),
    ("PUT", "/api/notify/config", "通知設定變更"),
    ("POST", "/api/notify/test", "通知測試發送"),
    ("PUT", "/api/target-weights", "目標配置變更"),
    ("PUT", "/api/ui-prefs", "介面偏好設定"),
    ("POST", "/api/whats-new/seen", "更新說明已讀"),
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
