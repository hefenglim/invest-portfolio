"""What's-new feature catalog + per-install acknowledged-version state (WP-WN, 2026-07-13).

A small, hand-maintained changelog of *user-facing* features surfaced by the ✦ 新功能
panel, plus a single-row table tracking the highest version the user has acknowledged
(so the "NEW" badge disappears once the panel is opened).

Follows the ``config_store`` create-always/seed-once pattern (same shape as
``ui_prefs``: one row, id=1). Lives in ``shared/`` and imports nothing internal beyond
``config_store`` so any layer may read it; only the api router writes it. Strings and
counts only — no money anywhere here.

CATALOG maintenance rule: when a version ships, add/update its entry here (+
``VERSION_DATES``) so the panel stays current. See the ship-version checklist.
"""

import re
import sqlite3
from datetime import datetime

from pydantic import BaseModel

from portfolio_dash.shared import config_store

_CATEGORY = "whatsnew"
_MAX_VERSIONS = 6
_VERSION_RE = re.compile(r"^\d+(\.\d+)*$")
# Default acknowledged version: lower than every real version, so a fresh install sees
# every visible group as unseen. "0" -> key (0,) sorts below any (0, 1, N).
_SEED_SEEN_VERSION = "0"


class Feature(BaseModel):
    """One user-facing feature announcement, bound to the version that introduced it."""

    version: str
    id: str  # kebab-case, unique within its version
    title: str  # zh-TW
    desc: str  # zh-TW one-liner
    href: str | None  # e.g. "settings.html#alerts"; None -> no 前往 button
    area: str  # zh-TW breadcrumb, e.g. "系統設定 → 預警規則"


# Hand-maintained, newest version first. 2-4 features per version, phrased for the end
# user (what they can now do), not implementation detail. The v0.1.18 entry is the
# what's-new system itself; it stays hidden until __version__ is bumped at ship time
# (visible_versions filters it out while current == 0.1.17 — this is intentional).
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
    ),
    Feature(
        version="0.1.17",
        id="target-weights",
        title="目標配置比重",
        desc="為每檔持股設定目標權重，驅動再平衡漂移預警與再平衡試算",
        href="settings.html#alerts",
        area="系統設定 → 預警規則",
    ),
    Feature(
        version="0.1.16",
        id="channel-setup-guides",
        title="通知通道設定教學",
        desc="ntfy／Telegram／Email 各通道新增逐步設定說明，降低設定門檻",
        href="settings.html#alerts",
        area="系統設定 → 預警規則",
    ),
    Feature(
        version="0.1.16",
        id="dispatch-timing-note",
        title="推播時段說明",
        desc="面板標示實際預警發送時段（工作日收盤後約 15:00 台北）",
        href="settings.html#alerts",
        area="系統設定 → 預警規則",
    ),
    Feature(
        version="0.1.15",
        id="channel-toggle-persist",
        title="通道開關即時儲存",
        desc="通知通道與勿擾時段的開關改為點擊即存，不需再另按儲存",
        href="settings.html#alerts",
        area="系統設定 → 預警規則",
    ),
    Feature(
        version="0.1.15",
        id="test-send-error-reason",
        title="測試發送錯誤原因",
        desc="測試發送失敗時顯示供應商回報的具體原因，便於排除設定問題",
        href="settings.html#alerts",
        area="系統設定 → 預警規則",
    ),
    Feature(
        version="0.1.14",
        id="push-channels",
        title="多通道推播通知",
        desc="預警與訊號事件可推播到 ntfy／Telegram／Email，任一通道獨立運作",
        href="settings.html#alerts",
        area="系統設定 → 預警規則",
    ),
    Feature(
        version="0.1.14",
        id="quiet-hours",
        title="勿擾時段",
        desc="設定勿擾時段，期間內的通知會延後送出",
        href="settings.html#alerts",
        area="系統設定 → 預警規則",
    ),
    Feature(
        version="0.1.14",
        id="per-rule-subscriptions",
        title="逐規則通知訂閱",
        desc="可分別選擇要接收哪些預警與訊號事件的推播",
        href="settings.html#alerts",
        area="系統設定 → 預警規則",
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

    A catalog version newer than *current* stays hidden (e.g. the v0.1.18 entry while
    ``__version__`` is still 0.1.17). Deduplicated across the multi-feature catalog.
    """
    current_key = _version_key(current)
    versions = {f.version for f in CATALOG if _version_key(f.version) <= current_key}
    return sorted(versions, key=_version_key, reverse=True)[:_MAX_VERSIONS]


# --- acknowledged-version persistence (single-row config_store table) -------

_DDL = (
    "CREATE TABLE IF NOT EXISTS whatsnew_config "
    "(id INTEGER PRIMARY KEY CHECK (id = 1), seen_version TEXT NOT NULL, "
    "updated_at TEXT NOT NULL)"
)


def _create(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO whatsnew_config (id, seen_version, updated_at) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO NOTHING",
        (_SEED_SEEN_VERSION, datetime(2026, 7, 13).isoformat()),
    )


def ensure_whatsnew_seeded(conn: sqlite3.Connection) -> None:
    """Create the single-row table (always) and seed the default seen_version (once)."""
    config_store.ensure_seeded(conn, _CATEGORY, create=_create, seed=_seed)


def get_seen_version(conn: sqlite3.Connection) -> str:
    """Return the highest acknowledged version; falls back to the seed when absent.

    Defensive (same stance as ui_prefs): a legacy/hand-edited row holding a non-version
    string would make every ``_version_key`` comparison raise — treat it as never-seen
    instead of 500ing the panel.
    """
    ensure_whatsnew_seeded(conn)
    row = conn.execute("SELECT seen_version FROM whatsnew_config WHERE id = 1").fetchone()
    raw = str(row["seen_version"]) if row is not None else _SEED_SEEN_VERSION
    return raw if is_valid_version(raw) else _SEED_SEEN_VERSION


def set_seen_version(conn: sqlite3.Connection, version: str, *, now: datetime) -> str:
    """Persist the acknowledged *version*, MONOTONIC — never regress.

    Keeps the max of the stored value and *version* (by version_key), so an out-of-order
    or replayed lower version cannot un-acknowledge newer features. Caller validates the
    format (:func:`is_valid_version`). Returns the value actually stored.
    """
    ensure_whatsnew_seeded(conn)
    current = get_seen_version(conn)
    winner = version if _version_key(version) > _version_key(current) else current
    conn.execute(
        "INSERT INTO whatsnew_config (id, seen_version, updated_at) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET seen_version = excluded.seen_version, "
        "updated_at = excluded.updated_at",
        (winner, now.isoformat()),
    )
    conn.commit()
    return winner
