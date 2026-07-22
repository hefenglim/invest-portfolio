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


# Hand-maintained, newest version first. Features are phrased for the end user (what they
# can now do), not implementation detail. Entries for a version that has not shipped yet
# stay hidden until __version__ is bumped to that version at ship time (visible_versions
# filters out any version newer than the running __version__ — this is intentional).
#
# v0.1.0 -> v0.1.11 were backfilled from CHANGELOG.md (round 3) so the history browser has
# the full release story; those older entries carry href=None (the ✦ panel caps at the
# newest 6 versions, so they surface only in 版本發佈資訊, which renders title/desc/area).
CATALOG: list[Feature] = [
    # --- v0.1.22 (Batch B: the Moomoo account merge) ------------------------------------
    Feature(
        version="0.1.22",
        id="moomoo-account-merge",
        title="Moomoo 帳戶合併為單一雙市場帳戶",
        desc="兩個 Moomoo 帳戶合併為一個「Moomoo MY」:買美股扣 USD、買馬股扣 MYR,"
        "馬幣同一池(餘額改為合計);首次啟動自動遷移並先建備份快照;"
        "此帳戶的換匯表單開機即可用",
        href="cash.html",
        area="資金管理",
        target="#cm-account",
    ),
    Feature(
        version="0.1.22",
        id="per-market-dividends",
        title="股利依標的市場自動分流",
        desc="同一帳戶下,美股股利走 DRIP(30% 預扣)、馬股走現金入帳——"
        "表單模式與寫入型別跟隨輸入代號的市場;CSV 匯入型別不符該市場模型時會要求確認",
        href="trades.html",
        area="交易輸入 → 股利",
        target="#d-symbol",
    ),
    Feature(
        version="0.1.22",
        id="merged-input-hardening",
        title="多市場帳戶輸入強化",
        desc="未註冊代號需明確選擇市場(絕不猜測);草稿預覽的幣別與價格精度跟隨標的"
        "(馬股顯示 MYR、3 位小數);帶舊 Moomoo 帳戶代號的 CSV 會自動轉換並提示",
        href="trades.html",
        area="交易輸入",
        target="#m-symbol",
    ),
    # --- v0.1.21 (round-7 batch A, FU-D55..D60) -----------------------------------------
    Feature(
        version="0.1.21",
        id="my-resolve-hardening",
        title="馬股標的判讀大幅強化",
        desc="內建 Bursa 全市場 1,079 檔代號名錄：輸入 inari、maybank 等名稱即判讀出正確代號"
        "（含 0 開頭代號），冷門股不再因報價源缺漏而判讀失敗",
        href="instruments.html",
        area="觀察清單 → 快速註冊",
        target="#quick-add-btn",
    ),
    Feature(
        version="0.1.21",
        id="news-fetch-hardening",
        title="新聞內文抓取強化",
        desc="抓取器補上瀏覽器身分與 cookie、放寬讀取上限並新增多段內文擷取後備；"
        "空內文會記錄原因並自動重抓，不再永久留白",
        href="news.html",
        area="新聞庫",
        target="#nw-run",
    ),
    Feature(
        version="0.1.21",
        id="draft-preview-oldnew",
        title="草稿預覽：舊→新對比＋扣款後現金",
        desc="買賣試算改為「舊 → 新」成對顯示（持股／原始均價／調整均價），"
        "並在該帳戶現金下方新增扣款後現金預估（幣別跟隨標的）",
        href="trades.html",
        area="交易輸入 → 草稿預覽",
        target="#m-pc-rows",
    ),
    Feature(
        version="0.1.21",
        id="whatif-drawer-backend",
        title="個股試算抽屜改由後端計算",
        desc="試算全數改由後端試算引擎供數並以舊→新對比呈現，新增賣出剩餘市值；"
        "前端不再自行計算任何金額",
        href="index.html",
        area="儀表板 → 個股詳情 → 試算",
        target="#holdings-body",
    ),
    Feature(
        version="0.1.21",
        id="input-clear-on-success",
        title="寫入成功自動清空＋勾選精準寫入",
        desc="AI 智能輸入的勾選框現在真正生效（只寫入勾選列）；AI／CSV 全數寫入成功後"
        "自動清空輸入避免誤觸重複寫入，部分成功則只保留未寫入列供檢查",
        href="trades.html",
        area="交易輸入 → AI 智能輸入",
        target="#ai-result",
    ),
    Feature(
        version="0.1.21",
        id="opening-simplified",
        title="期初庫存輸入簡化",
        desc="只需填股數與原始總成本（均價改為即時試算顯示），代號欄可直接挑選既有標的；"
        "舊 CSV 仍相容，均價與總成本不一致時會提醒確認",
        href="trades.html",
        area="交易輸入 → 期初庫存",
        target="#o-total",
    ),
    # --- v0.1.20 (v0.1.19 follow-up rounds r1-r6, FU-D1..D54) ---------------------------
    Feature(
        version="0.1.20",
        id="symbol-exact-resolve",
        title="代號解析安全化",
        desc="代號不再被相似碼自動改寫（如 2303 誤成 2330）；未註冊標的一律引導快速註冊，杜絕錯帳",
        href="trades.html",
        area="交易輸入 → 手動輸入",
        target="#m-symbol",
    ),
    Feature(
        version="0.1.20",
        id="ai-instrument-resolve",
        title="新增標的 AI 智能判讀",
        desc="輸入名稱或錯誤代號即自動判讀當地交易所代號＋名稱＋GICS 產業，"
        "經真實報價查核後帶入；不確定時列候選供點選",
        href="instruments.html",
        area="觀察清單 → 快速註冊",
        target="#quick-add-btn",
    ),
    Feature(
        version="0.1.20",
        id="gics-sectors",
        title="產業分類統一為 GICS",
        desc="三市場統一採 GICS 11 大類（中英對照），既有標的自動遷移；"
        "新增選填「產業細分」欄位，產業配置圖與集中度警示同步分組",
        href="index.html",
        area="儀表板 → 產業配置",
        target="#sector-chart",
    ),
    Feature(
        version="0.1.20",
        id="draft-preview-pnl",
        title="草稿預覽盈虧試算",
        desc="交易草稿即時顯示已實現損益、調整成本移除、剩餘股數（買入則為新均價），並列出該帳戶現金供參考",
        href="trades.html",
        area="交易輸入 → 草稿預覽",
        target="#m-pc-rows",
    ),
    Feature(
        version="0.1.20",
        id="sell-hints-live-ledger",
        title="賣出提示與帳本即時刷新",
        desc="賣出時提示可賣股數與持有均價（點擊帶入）；寫入後下方帳本原地更新，免重新整理",
        href="trades.html",
        area="交易輸入 → 手動",
        target="#pane-manual",
    ),
    Feature(
        version="0.1.20",
        id="fx-center",
        title="換匯中心",
        desc="顯示可用餘額（點擊帶入全額）、以最新匯率試算買入額、下方列出換匯記錄；"
        "透支硬擋，切帳戶自動清空金額，買賣幣別永不相同，單幣別帳戶不提供換匯",
        href="cash.html#fx",
        area="資金管理 → 換匯中心",
        target="#cfx-confirm",
    ),
    Feature(
        version="0.1.20",
        id="cash-guards",
        title="出金入金防護",
        desc="出金顯示賬戶現金並硬性阻擋超額（含回溯日期）；點擊餘額數字即可帶入全額",
        href="cash.html",
        area="資金管理 → 出金入金",
        target="#cm-confirm",
    ),
    Feature(
        version="0.1.20",
        id="dividend-overview",
        title="股利總覽",
        desc="新舊股利區整併為單一區段：近 12 個月實收、歷年分佈、"
        "年度預估（僅供參考）、除息日曆與回本進度",
        href="index.html",
        area="儀表板 → 股利總覽",
        target="#dividend-income-card",
    ),
    Feature(
        version="0.1.20",
        id="dividend-picker",
        title="股利輸入選擇器",
        desc="股利標的改為持有中清單點選（可切換顯示已清倉），配股入帳修正",
        href="trades.html",
        area="交易輸入 → 股利",
        target="#pane-div",
    ),
    Feature(
        version="0.1.20",
        id="ai-input-suite",
        title="AI 輸入強化",
        desc="支援對帳單圖片辨識與模型選擇；判讀一律輸出當地交易所代號；未註冊標的可於列內立即註冊後自動續匯入",
        href="trades.html",
        area="交易輸入 → AI 輸入",
        target="#pane-ai",
    ),
    Feature(
        version="0.1.20",
        id="csv-import-suite",
        title="CSV 匯入套件",
        desc="各資料類型提供範本下載；日期格式不明確時由您選擇，絕不猜測",
        href="trades.html",
        area="交易輸入 → CSV",
        target="#csv-dropzone",
    ),
    Feature(
        version="0.1.20",
        id="watchlist-lifecycle",
        title="觀察清單累積式管理",
        desc="移除改為封存（資料保留，重新加入自動補抓行情）；永久刪除僅限從未交易的純觀察標的並需輸入代號確認",
        href="instruments.html",
        area="觀察清單",
        target="#inst-body",
    ),
    Feature(
        version="0.1.20",
        id="target-price-alerts",
        title="目標價警示",
        desc="觀察清單可設目標價上下緣，穿越時觸發警示與推播（開關於 設定→預警規則）",
        href="instruments.html",
        area="觀察清單 → 目標價",
        target="#inst-body",
    ),
    Feature(
        version="0.1.20",
        id="scheduler-live-status",
        title="排程即時狀態與結果詳情",
        desc="立即執行顯示 排入→執行中（含處理進度）→成功／失敗；"
        "點狀態晶片開啟詳情（耗時、結果、Token 費用）並可直達資料頁",
        href="settings.html#scheduler",
        area="系統設定 → 排程中心",
        target="#view-scheduler",
    ),
    Feature(
        version="0.1.20",
        id="benchmark-twr",
        title="績效比較基準疊加",
        desc="趨勢圖新增 TWR 績效比較模式，可疊加 元大台灣50／S&P 500 基準（1／3年／全部）",
        href="index.html",
        area="儀表板 → 趨勢圖",
        target="#trend-mode",
    ),
    Feature(
        version="0.1.20",
        id="net-worth-trend",
        title="總資產（含現金）趨勢",
        desc="趨勢圖加入含現金的總資產線；各帳戶現金卡同步呈現",
        href="index.html",
        area="儀表板 → 趨勢圖",
        target="#trend-chart",
    ),
    Feature(
        version="0.1.20",
        id="fee-rules-center",
        title="費率規則中心",
        desc="費率明細改為即時資料驅動並可線上調整（含重設）；歷史交易以入帳當時快照為準",
        href="settings.html#accounts",
        area="系統設定 → 帳戶與費率",
        target="#fee-rules-wrap",
    ),
    Feature(
        version="0.1.20",
        id="alert-deep-links",
        title="通知直達連結",
        desc="推播與警示點擊直達對應頁面與區塊",
        href="settings.html#notify",
        area="系統設定 → 通知",
        target="#view-notify",
    ),
    Feature(
        version="0.1.20",
        id="data-center-page",
        title="資料中心",
        desc="新增資料中心頁：資料庫檔案統計與明細一覽",
        href="data-center.html",
        area="資料中心",
        target="#dc-summary",
    ),
    # --- v0.1.19 (P3 batch 3 · Wave 1 digests) — HIDDEN until __version__ is bumped ------
    Feature(
        version="0.1.19",
        id="daily-digest",
        title="每日收盤摘要",
        desc="收盤後自動彙整當日漲跌、警示與訊號，儀表板新增「今日摘要」卡",
        href="index.html",
        area="儀表板 → 今日摘要",
        target="#digest-daily-panel",
    ),
    Feature(
        version="0.1.19",
        id="weekly-action-list",
        title="每週行動清單",
        desc="每週日彙整再平衡漂移、警示回顧與即將除息等待辦，儀表板新增「週行動清單」",
        href="index.html",
        area="儀表板 → 週行動清單",
        target="#digest-weekly-panel",
    ),
    Feature(
        version="0.1.19",
        id="digest-settings",
        title="摘要與週報設定",
        desc="可開關每日／每週摘要、調整發送時間，並選用 AI 一句話總結",
        href="settings.html#scheduler",
        area="系統設定 → 排程中心 → 摘要與週報",
        target="#digest-settings-card",
    ),
    # --- v0.1.19 (P3 batch 3 · Wave 2 ledger + cash hardening) --------------------------
    Feature(
        version="0.1.19",
        id="cash-statement",
        title="現金收支明細",
        desc="點各帳戶現金池即可展開該幣別的收支明細，含每筆異動與伺服器計算的滾動餘額",
        href="cash.html",
        area="資金管理 → 現金收支明細",
        target="#cash-statement",
    ),
    Feature(
        version="0.1.19",
        id="ledger-input-hardening",
        title="交易輸入更嚴謹",
        desc="市場與帳戶不符、負費用、未來日期、重複與超大數值等會即時提示，並保留更正稽核",
        href="trades.html",
        area="交易帳本 → 交易輸入",
        target="#input-section",
    ),
    # --- v0.1.19 (fee-engine v2 + 折讓款 inbox) -----------------------------------------
    Feature(
        version="0.1.19",
        id="fee-engine-v2",
        title="費用引擎升級 v2",
        desc="改依各券商真實費率表計算手續費與稅：台股無條件捨去至整數、馬股印花稅與 SST、"
             "美股 SEC／TAF，交易預覽即時反映",
        href="trades.html",
        area="交易帳本 → 交易輸入",
        target="#input-section",
    ),
    Feature(
        version="0.1.19",
        id="rebate-inbox",
        title="折讓款預告與確認",
        desc="台股先收後退的次月手續費折讓自動預估並列入收件匣，實際入帳時一鍵確認記入現金池"
             "（僅供參考，不計入成本／損益）",
        href="dividend-inbox.html",
        area="收件匣 → 待確認退款（折讓款）",
        target="#rebate-section",
    ),
    # --- v0.1.19 (P3 batch 3 · Wave 3 fix pack + UX) ------------------------------------
    Feature(
        version="0.1.19",
        id="news-manual-fetch",
        title="手動抓取新聞",
        desc="新聞庫可選定範圍（全部或單一標的）立即抓取並以 AI 整理，不必等每日排程",
        href="news.html",
        area="新聞庫",
        target="#nw-toolbar",
    ),
    Feature(
        version="0.1.19",
        id="ui-toolbar-polish",
        title="操作列樣式統一",
        desc="全站面板的操作按鈕統一尺寸與對齊，同一列的按鈕高度一致，視覺更整齊",
        href="index.html",
        area="全站 → 面板操作列",
        target=".panel-head",
    ),
    Feature(
        version="0.1.19",
        id="dividend-inbox-page",
        title="股利收件匣獨立頁",
        desc="配息／配股偵測移至獨立的「股利收件匣」頁，可取消忽略、確認後一鍵復原",
        href="dividend-inbox.html",
        area="股利收件匣",
        target="#inbox-section",
    ),
    Feature(
        version="0.1.19",
        id="alert-bell-readstate",
        title="預警鈴已讀狀態",
        desc="開啟預警面板後鈴鐺紅點清除，僅在有新的預警時再次亮起（跨分頁同步）",
        href=None,
        area="全站 → 頂列預警鈴",
    ),
    Feature(
        version="0.1.19",
        id="quota-gate-when-ai-off",
        title="未啟用 AI 不再誤報額度",
        desc="尚未設定任何 AI 模型時，額度標籤顯示「AI 未啟用」，不再誤報 LLM 額度偏低預警",
        href=None,
        area="全站 → AI 額度標籤",
    ),
    Feature(
        version="0.1.18",
        id="whats-new-panel",
        title="新功能通知",
        desc="每次改版的新功能一覽，可一鍵前往對應的設定或頁面",
        href=None,
        area="全站 → 頂列 ✦",
    ),
    Feature(
        version="0.1.18",
        id="version-history-browser",
        title="版本發佈資訊瀏覽",
        desc="系統設定「一般」新增版本發佈資訊按鈕，可翻閱每次改版的完整功能清單",
        href="settings.html#accounts",
        area="系統設定 → 一般",
        target="#gen-whatsnew",
    ),
    Feature(
        version="0.1.18",
        id="rebalance-combined",
        title="再平衡跨帳戶合併試算",
        desc="同一標的跨多帳戶合併為一列試算，並標示各帳戶持股，目標權重驅動合併部位",
        href="index.html",
        area="儀表板 → 持倉明細",
        target='section[data-screen-label="持倉明細"]',
    ),
    Feature(
        version="0.1.18",
        id="rebalance-report-export",
        title="再平衡執行報告匯出",
        desc="再平衡試算可匯出可列印的執行報告，依帳戶列出買賣股數與費稅清單",
        href="index.html",
        area="儀表板 → 持倉明細",
        target='section[data-screen-label="持倉明細"]',
    ),
    Feature(
        version="0.1.18",
        id="holdings-report-export",
        title="持倉報告匯出",
        desc="持倉明細可匯出可列印的持倉報告，含 KPI 摘要、持倉明細與產業／幣別配置",
        href="index.html",
        area="儀表板 → 持倉明細",
        target='section[data-screen-label="持倉明細"]',
    ),
    Feature(
        version="0.1.18",
        id="ledger-report-export",
        title="帳本報告匯出",
        desc="交易帳本可匯出可列印的帳本報告，涵蓋交易、股利、換匯與期初庫存，可依日期區間",
        href="trades.html",
        area="交易帳本 → 帳本記錄",
        target='section[data-screen-label="帳本記錄"]',
    ),
    Feature(
        version="0.1.18",
        id="reconciliation-csv",
        title="CSV 匯出全面對帳級",
        desc="所有匯出 CSV 改由後端計算核心直接產生，數字為對帳級全精度，不再取自畫面顯示值",
        href="index.html",
        area="儀表板 → 持倉明細",
        target='section[data-screen-label="持倉明細"]',
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
        href="settings.html#notify",
        area="系統設定 → 通知中心",
        target=".nt-cards",
    ),
    Feature(
        version="0.1.16",
        id="dispatch-timing-note",
        title="推播時段說明",
        desc="面板標示實際預警發送時段（工作日收盤後約 15:00 台北）",
        href="settings.html#notify",
        area="系統設定 → 通知中心",
        target=".nt-cards",
    ),
    Feature(
        version="0.1.15",
        id="channel-toggle-persist",
        title="通道開關即時儲存",
        desc="通知通道與勿擾時段的開關改為點擊即存，不需再另按儲存",
        href="settings.html#notify",
        area="系統設定 → 通知中心",
        target=".nt-cards",
    ),
    Feature(
        version="0.1.15",
        id="test-send-error-reason",
        title="測試發送錯誤原因",
        desc="測試發送失敗時顯示供應商回報的具體原因，便於排除設定問題",
        href="settings.html#notify",
        area="系統設定 → 通知中心",
        target=".nt-cards",
    ),
    Feature(
        version="0.1.14",
        id="push-channels",
        title="多通道推播通知",
        desc="預警與訊號事件可推播到 ntfy／Telegram／Email，任一通道獨立運作",
        href="settings.html#notify",
        area="系統設定 → 通知中心",
        target=".nt-cards",
    ),
    Feature(
        version="0.1.14",
        id="quiet-hours",
        title="勿擾時段",
        desc="設定勿擾時段，期間內的通知會延後送出",
        href="settings.html#notify",
        area="系統設定 → 通知中心",
        target="#nt-qh-enabled",
    ),
    Feature(
        version="0.1.14",
        id="per-rule-subscriptions",
        title="逐規則通知訂閱",
        desc="可分別選擇要接收哪些預警與訊號事件的推播",
        href="settings.html#notify",
        area="系統設定 → 通知中心",
        target="#nt-subs",
    ),
    Feature(
        version="0.1.13",
        id="rules-engine",
        title="技術規則訊號引擎",
        desc="持股與觀察標的計算 TechScore 與趨勢、交叉、動能、RSI 四項技術訊號",
        href="instruments.html",
        area="觀察清單",
        target='section[data-screen-label="標的清單"]',
    ),
    Feature(
        version="0.1.13",
        id="drawer-signal-chips",
        title="個股技術訊號卡",
        desc="點開任一標的抽屜即可看到 TechScore、各規則證據與判讀說明",
        href="instruments.html",
        area="觀察清單 → 個股抽屜",
        target='section[data-screen-label="標的清單"]',
    ),
    Feature(
        version="0.1.13",
        id="signal-transition-events",
        title="訊號轉折事件",
        desc="趨勢、交叉與動能轉折會自動進入預警與推播串流",
        href="index.html",
        area="儀表板 → 持倉明細",
        target='section[data-screen-label="持倉明細"]',
    ),
    Feature(
        version="0.1.12",
        id="trading-volume",
        title="成交量資料",
        desc="三大市場的成交量納入資料庫，供技術訊號與 AI 健檢引用",
        href="instruments.html",
        area="觀察清單",
        target='section[data-screen-label="標的清單"]',
    ),
    Feature(
        version="0.1.12",
        id="five-year-history",
        title="五年價格歷史",
        desc="價格歷史回補延長至五年，52 週位置與長期指標更完整",
        href="instruments.html",
        area="觀察清單",
        target='section[data-screen-label="標的清單"]',
    ),
    Feature(
        version="0.1.12",
        id="analyst-consensus",
        title="分析師共識變數",
        desc="新增分析師目標價與評等共識，作為 AI 洞察的判讀依據",
        href="insights.html",
        area="AI 洞察",
        target='section[data-screen-label="AI 洞察"]',
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
        desc="「資料中心」頁可檢視各資料表列數、最舊紀錄日期、每類小計與檔案大小",
        href="data-center.html",
        area="資料中心 → 資料庫統計",
        target="#dbstats-body",
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

# version -> ISO delivery date (from the CHANGELOG headings). A not-yet-shipped version's
# date is added here when it ships; a version missing from this map serializes as date: null.
VERSION_DATES: dict[str, str] = {
    "0.1.22": "2026-07-22",
    "0.1.21": "2026-07-21",
    "0.1.20": "2026-07-20",
    "0.1.19": "2026-07-15",
    "0.1.18": "2026-07-14",
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
