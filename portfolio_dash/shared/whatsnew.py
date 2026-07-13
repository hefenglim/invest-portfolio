"""What's-new feature catalog + per-FEATURE seen-state (WP-WN; round 3, 2026-07-13).

A small, hand-maintained changelog of *user-facing* features surfaced by two surfaces:

* the ✦ 新功能 panel — the most recent ``_MAX_VERSIONS`` versions, with a per-feature
  "NEW" badge that clears only when the user actually acts on a feature (前往 / 知道了 /
  全部標示已讀), and
* the 版本發佈資訊 history browser (settings 一般) — the FULL catalog, paged newest-first
  via ``all_visible_versions`` + the router's ``offset/limit`` slice. Paging is the
  scalability answer: the browser only renders the pages it has loaded, so an
  ever-growing catalog can never degrade page performance.

Seen-state is per feature (round 3, replacing the old version-level ack): a
``whatsnew_seen`` table of ``"<version>:<id>"`` keys. A one-time migration folds any
legacy ``whatsnew_config.seen_version`` acknowledgement into per-feature rows; the
legacy table is kept in place (never dropped — a legacy-schema boot crash is a known
lesson in this repo) but is no longer written.

Follows the ``config_store`` create-always/seed-once pattern. Lives in ``shared/`` and
imports nothing internal beyond ``config_store`` so any layer may read it; only the api
router writes it. Strings and counts only — no money anywhere here.

CATALOG maintenance rule: when a version ships, add/update its entry here (+
``VERSION_DATES``) so both surfaces stay current. See the ship-version checklist.
"""

import re
import sqlite3
from datetime import datetime

from pydantic import BaseModel

from portfolio_dash.shared import config_store

_CATEGORY = "whatsnew"
_MAX_VERSIONS = 6
_VERSION_RE = re.compile(r"^\d+(\.\d+)*$")
# Default legacy acknowledged version: lower than every real version. "0" -> key (0,)
# sorts below any (0, 1, N). Only used by the migration source + the legacy seed row.
_SEED_SEEN_VERSION = "0"
# Fixed seen_at stamp for migrated rows (config_store-style constant; not user-facing).
_MIGRATION_AT = datetime(2026, 7, 13).isoformat()


class Feature(BaseModel):
    """One user-facing feature announcement, bound to the version that introduced it."""

    version: str
    id: str  # kebab-case, unique within its version
    title: str  # zh-TW
    desc: str  # zh-TW one-liner
    href: str | None  # e.g. "settings.html#alerts"; None -> no 前往 button (知道了 instead)
    area: str  # zh-TW breadcrumb, e.g. "系統設定 → 預警規則"
    # Optional CSS selector for the precise in-page element the feature lives at, used by
    # the arrival callout/flash to point exactly WHERE it changed. Presentation metadata
    # only (no logic here). Only meaningful when ``href`` is set; None -> no precise anchor.
    target: str | None = None


# Hand-maintained, newest version first. 2-4 features per version, phrased for the end
# user (what they can now do), not implementation detail. The v0.1.18 entry is the
# what's-new system itself; it stays hidden until __version__ is bumped at ship time
# (visible_versions filters it out while current == 0.1.17 — this is intentional).
#
# v0.1.0 -> v0.1.11 were backfilled from CHANGELOG.md (round 3) so the history browser has
# the full release story; those older entries carry href=None (the ✦ panel caps at the
# newest 6 versions, so they surface only in 版本發佈資訊, which renders title/desc/area).
CATALOG: list[Feature] = [
    Feature(
        version="0.1.18",
        id="whats-new-panel",
        title="新功能通知",
        desc="每次改版的新功能一覽，可一鍵前往對應的設定或頁面",
        href=None,
        area="全站 → 頂列 ✦",
    ),
    Feature(
        version="0.1.17",
        id="market-risk-alerts",
        title="市場風險預警",
        desc="新增回檔、波動、再平衡漂移與分析師共識四項自動預警，可推播到手機",
        href="settings.html#alerts",
        area="系統設定 → 預警規則",
        target="#alert-rules-wrap",
    ),
    Feature(
        version="0.1.17",
        id="target-weights",
        title="目標配置比重",
        desc="為每檔持股設定目標權重，驅動再平衡漂移預警與再平衡試算",
        href="settings.html#alerts",
        area="系統設定 → 預警規則",
        target="#target-weights-panel",
    ),
    Feature(
        version="0.1.16",
        id="channel-setup-guides",
        title="通知通道設定教學",
        desc="ntfy／Telegram／Email 各通道新增逐步設定說明，降低設定門檻",
        href="settings.html#alerts",
        area="系統設定 → 預警規則",
        target=".nt-cards",
    ),
    Feature(
        version="0.1.16",
        id="dispatch-timing-note",
        title="推播時段說明",
        desc="面板標示實際預警發送時段（工作日收盤後約 15:00 台北）",
        href="settings.html#alerts",
        area="系統設定 → 預警規則",
        target=".nt-cards",
    ),
    Feature(
        version="0.1.15",
        id="channel-toggle-persist",
        title="通道開關即時儲存",
        desc="通知通道與勿擾時段的開關改為點擊即存，不需再另按儲存",
        href="settings.html#alerts",
        area="系統設定 → 預警規則",
        target=".nt-cards",
    ),
    Feature(
        version="0.1.15",
        id="test-send-error-reason",
        title="測試發送錯誤原因",
        desc="測試發送失敗時顯示供應商回報的具體原因，便於排除設定問題",
        href="settings.html#alerts",
        area="系統設定 → 預警規則",
        target=".nt-cards",
    ),
    Feature(
        version="0.1.14",
        id="push-channels",
        title="多通道推播通知",
        desc="預警與訊號事件可推播到 ntfy／Telegram／Email，任一通道獨立運作",
        href="settings.html#alerts",
        area="系統設定 → 預警規則",
        target=".nt-cards",
    ),
    Feature(
        version="0.1.14",
        id="quiet-hours",
        title="勿擾時段",
        desc="設定勿擾時段，期間內的通知會延後送出",
        href="settings.html#alerts",
        area="系統設定 → 預警規則",
        target="#nt-qh-enabled",
    ),
    Feature(
        version="0.1.14",
        id="per-rule-subscriptions",
        title="逐規則通知訂閱",
        desc="可分別選擇要接收哪些預警與訊號事件的推播",
        href="settings.html#alerts",
        area="系統設定 → 預警規則",
        target="#nt-subs",
    ),
    Feature(
        version="0.1.13",
        id="rules-engine",
        title="技術規則訊號引擎",
        desc="持股與觀察標的計算 TechScore 與趨勢、交叉、動能、RSI 四項技術訊號",
        href="instruments.html",
        area="觀察清單",
    ),
    Feature(
        version="0.1.13",
        id="drawer-signal-chips",
        title="個股技術訊號卡",
        desc="點開任一標的抽屜即可看到 TechScore、各規則證據與判讀說明",
        href="instruments.html",
        area="觀察清單 → 個股抽屜",
    ),
    Feature(
        version="0.1.13",
        id="signal-transition-events",
        title="訊號轉折事件",
        desc="趨勢、交叉與動能轉折會自動進入預警與推播串流",
        href="index.html",
        area="儀表板",
    ),
    Feature(
        version="0.1.12",
        id="trading-volume",
        title="成交量資料",
        desc="三大市場的成交量納入資料庫，供技術訊號與 AI 健檢引用",
        href="instruments.html",
        area="觀察清單",
    ),
    Feature(
        version="0.1.12",
        id="five-year-history",
        title="五年價格歷史",
        desc="價格歷史回補延長至五年，52 週位置與長期指標更完整",
        href="instruments.html",
        area="觀察清單",
    ),
    Feature(
        version="0.1.12",
        id="analyst-consensus",
        title="分析師共識變數",
        desc="新增分析師目標價與評等共識，作為 AI 洞察的判讀依據",
        href="insights.html",
        area="AI 洞察",
    ),
    # --- backfill: v0.1.11 -> v0.1.0 (history browser only; href=None) ---------------
    Feature(
        version="0.1.11",
        id="official-task-pack",
        title="官方洞察任務包",
        desc="一鍵建立持倉週報、個股健檢與市場週報三項官方排程任務",
        href=None,
        area="系統設定 → 排程",
    ),
    Feature(
        version="0.1.11",
        id="news-pipeline",
        title="新聞內容管線",
        desc="自動擷取中英文新聞、以 AI 整理並建立個股新聞索引，新增新聞庫頁面",
        href=None,
        area="新聞庫",
    ),
    Feature(
        version="0.1.11",
        id="db-stats-panel",
        title="資料庫統計面板",
        desc="設定頁可檢視各資料表列數、最舊紀錄日期與檔案大小",
        href=None,
        area="系統設定 → 一般",
    ),
    Feature(
        version="0.1.11",
        id="site-pagination",
        title="全站分頁與每頁筆數",
        desc="各清單與帳本支援分頁，並可自訂每頁顯示筆數",
        href=None,
        area="全站",
    ),
    Feature(
        version="0.1.10",
        id="llm-insights-live",
        title="AI 洞察正式啟用",
        desc="AI 洞察在正式模型供應商上穩定運作，產出結構化洞察卡",
        href=None,
        area="AI 洞察",
    ),
    Feature(
        version="0.1.10",
        id="official-templates",
        title="官方提示詞範本庫",
        desc="系統提示詞與策略卡官方範本，可一鍵重置回官方版或複製自訂",
        href=None,
        area="系統設定 → 提示詞",
    ),
    Feature(
        version="0.1.10",
        id="scoring-rubric",
        title="洞察評分準則",
        desc="AI 洞察以方向、引用、情境與時效四面向評分，作為學習依據",
        href=None,
        area="AI 洞察",
    ),
    Feature(
        version="0.1.9",
        id="mobile-layout",
        title="手機版介面",
        desc="全站支援手機（iPhone）版面：側欄改為抽屜、表格獨立捲動、觸控更順手",
        href=None,
        area="全站",
    ),
    Feature(
        version="0.1.8",
        id="cash-management",
        title="資金管理頁",
        desc="各帳戶現金池集中管理：入金、出金、換匯與現金異動帳本",
        href=None,
        area="資金管理",
    ),
    Feature(
        version="0.1.8",
        id="monthly-snapshot",
        title="月度成績快照",
        desc="每月自動記錄總值、報酬率與 XIRR，儀表板新增月度成績面板",
        href=None,
        area="儀表板",
    ),
    Feature(
        version="0.1.8",
        id="inbox-badge",
        title="待確認股利提醒",
        desc="側欄交易帳本顯示待確認股利筆數徽章",
        href=None,
        area="側欄 → 交易帳本",
    ),
    Feature(
        version="0.1.7",
        id="all-market-dividend",
        title="全市場配息偵測",
        desc="股利待確認依帳戶模型支援台股現金、美股 DRIP、馬股淨額與台股配股",
        href=None,
        area="交易帳本 → 股利待確認",
    ),
    Feature(
        version="0.1.7",
        id="dividend-daily-scan",
        title="配息每日自動掃描",
        desc="每日收盤後自動掃描新配息事件並回報待確認筆數",
        href=None,
        area="交易帳本 → 股利待確認",
    ),
    Feature(
        version="0.1.6",
        id="tw-dividend-inbox",
        title="台股配息待確認匯入",
        desc="自動偵測台股現金配息並列入待確認清單，確認後才入帳，絕不自動入帳",
        href=None,
        area="交易帳本 → 股利待確認",
    ),
    Feature(
        version="0.1.6",
        id="smart-backfill",
        title="智慧歷史回補",
        desc="價格與匯率歷史自動回補至最早持有日，走勢圖與 XIRR 更完整",
        href=None,
        area="觀察清單",
    ),
    Feature(
        version="0.1.5",
        id="single-entry-forms",
        title="股利／換匯／期初單筆輸入",
        desc="股利、換匯與期初庫存提供專屬輸入表單，並支援 CSV 拖放匯入",
        href=None,
        area="交易輸入",
    ),
    Feature(
        version="0.1.5",
        id="system-action-log",
        title="系統操作記錄",
        desc="每筆異動操作自動記錄，可追溯時間、對象與結果",
        href=None,
        area="系統設定 → 排程",
    ),
    Feature(
        version="0.1.5",
        id="per-market-quote-order",
        title="各市場報價來源順序",
        desc="可分別設定台、美、馬三市場的報價來源優先順序",
        href=None,
        area="系統設定 → 資料來源",
    ),
    Feature(
        version="0.1.5",
        id="instruments-full-edit",
        title="標的完整欄位編輯",
        desc="名稱、產業、板別、ETF 與目標價皆可編輯（含美股）",
        href=None,
        area="觀察清單",
    ),
    Feature(
        version="0.1.4",
        id="one-step-add",
        title="一步新增標的",
        desc="輸入代號與市場即可完成註冊，自動帶入名稱並回補歷史走勢",
        href=None,
        area="觀察清單",
    ),
    Feature(
        version="0.1.4",
        id="ledger-row-edit",
        title="帳本逐筆修改",
        desc="交易、股利、換匯與期初資料皆可逐筆編輯或刪除，並自動重播檢查賣超",
        href=None,
        area="交易帳本",
    ),
    Feature(
        version="0.1.4",
        id="progress-visibility",
        title="全站進度提示",
        desc="更新報價、重算、歷史回補等長操作顯示進度條與提示，不再像卡住",
        href=None,
        area="全站",
    ),
    Feature(
        version="0.1.4",
        id="build-identity",
        title="版本與建置資訊",
        desc="側欄顯示版本與建置編號，未發行版另有標示",
        href=None,
        area="側欄",
    ),
    Feature(
        version="0.1.3",
        id="refresh-recompute-buttons",
        title="更新報價與重算按鈕",
        desc="頂列「更新報價」「重算」按鈕實際連動後端，隨時取得最新價格與重新計算",
        href=None,
        area="頂列",
    ),
    Feature(
        version="0.1.3",
        id="instant-first-quote",
        title="註冊即抓報價",
        desc="新增標的後立即抓取最新報價，不必等待收盤排程",
        href=None,
        area="觀察清單",
    ),
    Feature(
        version="0.1.3",
        id="symbol-search",
        title="標的快速搜尋",
        desc="以 Cmd／Ctrl＋K 搜尋已註冊標的並快速跳轉",
        href=None,
        area="全站",
    ),
    Feature(
        version="0.1.2",
        id="datasource-conn-test",
        title="資料來源連線測試",
        desc="設定頁可實際測試 yfinance／TWSE／TPEx／FinMind 是否連線正常",
        href=None,
        area="系統設定 → 資料來源",
    ),
    Feature(
        version="0.1.2",
        id="version-display",
        title="系統版本顯示",
        desc="側欄與設定頁顯示目前系統版本，方便確認更新狀態",
        href=None,
        area="側欄",
    ),
    Feature(
        version="0.1.1",
        id="first-run-bootstrap",
        title="全新安裝開箱即用",
        desc="首次啟動自動建立資料表與券商帳戶，全新安裝即可直接輸入交易",
        href=None,
        area="系統",
    ),
    Feature(
        version="0.1.0",
        id="dashboard-launch",
        title="投資組合儀表板上線",
        desc="持股、已實現／未實現損益、報酬率、產業配置與 XIRR 集中一頁檢視",
        href=None,
        area="儀表板",
    ),
    Feature(
        version="0.1.0",
        id="daily-backup",
        title="每日自動備份",
        desc="每日自動備份資料庫並檢查完整性，資料更有保障",
        href=None,
        area="系統",
    ),
    Feature(
        version="0.1.0",
        id="multi-account-login",
        title="多帳戶與登入保護",
        desc="支援多券商帳戶與多幣別，並以登入保護個人財務資料",
        href=None,
        area="全站",
    ),
]

# version -> ISO delivery date (from the CHANGELOG headings). v0.1.18 is intentionally
# absent until it ships (GET serializes a missing entry as date: null).
VERSION_DATES: dict[str, str] = {
    "0.1.17": "2026-07-13",
    "0.1.16": "2026-07-12",
    "0.1.15": "2026-07-12",
    "0.1.14": "2026-07-12",
    "0.1.13": "2026-07-11",
    "0.1.12": "2026-07-09",
    "0.1.11": "2026-07-08",
    "0.1.10": "2026-07-05",
    "0.1.9": "2026-07-03",
    "0.1.8": "2026-07-03",
    "0.1.7": "2026-07-03",
    "0.1.6": "2026-07-03",
    "0.1.5": "2026-07-03",
    "0.1.4": "2026-07-02",
    "0.1.3": "2026-07-02",
    "0.1.2": "2026-07-02",
    "0.1.1": "2026-06-19",
    "0.1.0": "2026-06-19",
}


# --- version ordering -------------------------------------------------------


def is_valid_version(version: str) -> bool:
    """True iff *version* matches ``^\\d+(\\.\\d+)*$`` (the format the catalog uses)."""
    return _VERSION_RE.match(version) is not None


def _version_key(version: str) -> tuple[int, ...]:
    """Numeric tuple for ordering, so "0.1.9" < "0.1.10" orders correctly.

    Assumes a validated ``^\\d+(\\.\\d+)*$`` string (callers validate first).
    """
    return tuple(int(part) for part in version.split("."))


def visible_versions(current: str) -> list[str]:
    """Catalog versions with ``key <= key(current)``, newest first, capped at 6.

    Feeds the ✦ panel only. A catalog version newer than *current* stays hidden (e.g. the
    v0.1.18 entry while ``__version__`` is still 0.1.17). Deduplicated across the
    multi-feature catalog.
    """
    current_key = _version_key(current)
    versions = {f.version for f in CATALOG if _version_key(f.version) <= current_key}
    return sorted(versions, key=_version_key, reverse=True)[:_MAX_VERSIONS]


def all_visible_versions(current: str) -> list[str]:
    """Every catalog version with ``key <= key(current)``, newest first, UNCAPPED.

    Feeds the 版本發佈資訊 history browser (the 6-version cap applies only to the ✦ panel);
    the router pages through this list via ``offset/limit``.
    """
    current_key = _version_key(current)
    versions = {f.version for f in CATALOG if _version_key(f.version) <= current_key}
    return sorted(versions, key=_version_key, reverse=True)


def known_feature_keys(current: str) -> set[str]:
    """Per-feature keys (``"<version>:<id>"``) for features in the ✦ panel window.

    The router validates POSTed seen keys against this set: a key outside the visible
    window (never rendered in the panel) can never be marked seen.
    """
    visible = set(visible_versions(current))
    return {f"{f.version}:{f.id}" for f in CATALOG if f.version in visible}


# --- per-feature seen-state (config_store create-always / seed-once) --------

# Legacy single-row table (round 1/2): kept in place for the migration source + so a boot
# on an old checkout never crashes on a missing table. No longer written after round 3.
_DDL = (
    "CREATE TABLE IF NOT EXISTS whatsnew_config "
    "(id INTEGER PRIMARY KEY CHECK (id = 1), seen_version TEXT NOT NULL, "
    "updated_at TEXT NOT NULL)"
)
# Round-3 per-feature seen table: one row per acknowledged "<version>:<id>" key.
_SEEN_DDL = (
    "CREATE TABLE IF NOT EXISTS whatsnew_seen "
    "(feature_key TEXT PRIMARY KEY, seen_at TEXT NOT NULL)"
)


def _create(conn: sqlite3.Connection) -> None:
    # Both tables, every boot (config_store's create runs on every startup, so existing
    # installs pick up whatsnew_seen safely without a bespoke migration path).
    conn.execute(_DDL)
    conn.execute(_SEEN_DDL)


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO whatsnew_config (id, seen_version, updated_at) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO NOTHING",
        (_SEED_SEEN_VERSION, _MIGRATION_AT),
    )


def _migrate_legacy_seen(conn: sqlite3.Connection) -> None:
    """Fold a legacy version-level ack into per-feature seen rows (idempotent).

    If the legacy ``whatsnew_config.seen_version`` is a valid version > "0", mark every
    catalog feature at or below it as seen (INSERT OR IGNORE). Effectively one-time: seen
    rows only ever grow, so re-running is a no-op. A fresh install (seen_version "0") and
    a hand-corrupted non-version row both short-circuit here.
    """
    row = conn.execute(
        "SELECT seen_version FROM whatsnew_config WHERE id = 1"
    ).fetchone()
    if row is None:
        return
    raw = str(row["seen_version"])
    if not is_valid_version(raw) or _version_key(raw) <= _version_key(_SEED_SEEN_VERSION):
        return
    legacy_key = _version_key(raw)
    keys = [f"{f.version}:{f.id}" for f in CATALOG if _version_key(f.version) <= legacy_key]
    if not keys:
        return
    conn.executemany(
        "INSERT OR IGNORE INTO whatsnew_seen (feature_key, seen_at) VALUES (?, ?)",
        [(k, _MIGRATION_AT) for k in keys],
    )
    conn.commit()


def ensure_whatsnew_seeded(conn: sqlite3.Connection) -> None:
    """Create both tables (always), seed the legacy row (once), then migrate legacy acks."""
    config_store.ensure_seeded(conn, _CATEGORY, create=_create, seed=_seed)
    _migrate_legacy_seen(conn)


def get_seen_keys(conn: sqlite3.Connection) -> set[str]:
    """Return the set of acknowledged ``"<version>:<id>"`` feature keys."""
    ensure_whatsnew_seeded(conn)
    rows = conn.execute("SELECT feature_key FROM whatsnew_seen").fetchall()
    return {str(r["feature_key"]) for r in rows}


def mark_seen(conn: sqlite3.Connection, keys: list[str], *, now: datetime) -> set[str]:
    """Mark each key in *keys* as seen (INSERT OR IGNORE); return the full seen set.

    Idempotent — an already-seen key is left untouched (its original ``seen_at`` stands).
    Caller validates the keys against :func:`known_feature_keys`.
    """
    ensure_whatsnew_seeded(conn)
    if keys:
        conn.executemany(
            "INSERT OR IGNORE INTO whatsnew_seen (feature_key, seen_at) VALUES (?, ?)",
            [(k, now.isoformat()) for k in keys],
        )
        conn.commit()
    return get_seen_keys(conn)
