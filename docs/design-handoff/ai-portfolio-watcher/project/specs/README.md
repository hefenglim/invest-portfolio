# Backend Spec — 前端已實作、後端需補足的功能總覽

> 交付對象：後端 Claude Code。本資料夾每份 spec 對應一組前端已完成的 UI（mock 運作中），
> 後端補齊後將 mock 替換為真實 endpoint。前端檔案內以 `設計預覽`/`mock` 註記的位置即為接線點。
>
> 共同原則（沿用 repo rules，不重複贅述）：
> - 金額一律 `Decimal`，序列化為字串；前端只負責格式化顯示。
> - 不可跨幣別加總；報告幣別合併只在 `portfolio/` 計算層發生。
> - LLM 只做敘事合成，所有數字由計算核心提供（llm-insight.md 硬規則）。
> - 帳本 append-only；所有「試算」類 endpoint 為純計算、絕不寫入。

> **2026-06-13 全量 Gap Analysis 補齊（specs 08–16）**：後端目前為純 Python 計算庫、
> **不存在任何 HTTP 層** — spec 08 建立 FastAPI 骨架，為所有其他 spec 的前置。
> 實作順序建議：**08＋17（骨架與測試地基同步）→ 10/11/12（核心資料流）→ 01/02/03 → 09/13/14/15/16 → 04/05/06/07**；
> 每完成一個 spec 以 `make all` 全綠為完成定義（spec 17 §17.6）。
>
> **SR 審查（2026-06-13）**：全量對齊完成，8 項衝突已修入各 spec（標 `SR` 註記），
> 詳見 `SR-2026-06-13.md`；**含 Q1 在內全數定案關閉，無任何阻塞項，可直接交付 Claude Code。**

| Spec | 對應前端 | 優先級 |
|---|---|---|
| `08-app-shell-dashboard.md` | FastAPI 骨架、GET /api/dashboard（mock-data.js 即契約）、頂欄更新報價/重算（shell.js） | **P0（前置）** |
| `10-instruments.md` | 觀察清單（instruments.js）、板別探測、註冊、全域搜尋 registry（shell.js SYMBOLS） | P0 |
| `11-ledgers-read.md` | 四帳本唯讀清單（ledger.js / trades.html） | P0 |
| `12-input-center.md` | 輸入中心 5 tabs：手動/CSV/AI/股利/換匯期初（input.js） | P0 |
| `01-symbol-detail.md` | 個股詳情抽屜（detail.js / history-mock.js） | P0 |
| `02-export-endpoints.md` | 匯出中心 + 對帳級 CSV（settings-alerts.js E7 區、export.js） | P0 |
| `03-strategy-alerts-rebalance.md` | 風險預警鈴鐺（alerts.js）、預警規則設定（E1）、再平衡/買賣試算（rebalance.js、detail.js simSection） | P1 |
| `04-ai-self-evolution.md` | 策略=純設計物件、組合器多策略+排程掛載+自我校正開關、1:1 校正版本管理器、AI 大師模型（master role）、回測評分管線 | P1–P2 |
| `05-dividend-projection.md` | 除息日曆「年內股利預估」chips（F5） | P2 |
| `06-data-variables.md` | 數據變數系統、提示詞預覽/測試送出（LiteLLM）、FinMind/情緒外部資料快照 ingest | P1 |
| `09-auth-users.md` | 登入/鎖定/登出、授權用戶 CRUD（login.html、shell.js pdAuth、settings-users.js） | P1 |
| `13-accounts-fees.md` | 帳戶與費率唯讀（settings-accounts.html） | P1 |
| `14-datasources.md` | 資料來源金鑰/測試/fallback 鏈（settings-datasources.js） | P1 |
| `15-scheduler.md` | 排程管理：jobs/cron/立即執行/執行歷史（settings-scheduler.js） | P1 |
| `16-llm-settings.md` | LLM 模型/角色/額度/用量（settings-llm.js） | P1 |
| `17-testing-regression.md` | 全端自動測試關魪修復閉環、黃金資料集、E2E、回歸機制 | **P0（與 08 同步）** |
| `18-calculation-correctness.md` | 核心計算 bug-free：費率真值表、手算對照、會計恆等式、性質測試、Decimal 紀律 | **P0（計算層驗收憲法）** |
| `19-frontend-wiring-ops.md` | 統一 api.js 接線層、啟動佈局、SQLite 備份/還原、日誌 | **P0（接線開工前必讀）** |

交付方式見 `HANDOFF.md`（含後端 repo 的 CLAUDE.md 範本）。

## 前端 mock 接線點索引

| 前端檔案 | mock 內容 | 替換方式 |
|---|---|---|
| `history-mock.js` | 個股日線（隨機種子）、配息/交易事件 | GET /api/symbol/{symbol}/detail（spec 01） |
| `detail.js` `feeTax()` | 四帳戶費稅規則前端鏡像 | POST /api/whatif（spec 03）；前端鏡像僅留作離線 fallback |
| `alerts.js` `computeAlerts()` | 規則引擎前端版 | GET /api/alerts（spec 03） |
| `alerts.js` `PD_QUOTA` | AI 額度 hardcode $0.84 | 既有 shared/llm 額度帳 → dashboard payload |
| `rebalance.js` | 再平衡試算（前端計算） | POST /api/rebalance/preview（spec 03） |
| `settings-alerts.js` | 規則門檻存 localStorage | strategy/ config 表（spec 03） |
| `insights.html` AI 戰績 | 預測明細、命中率、校準 bins 全為假資料 | spec 04 self-backtest |
| `settings-prompts.js` COMPOSER_DATA / CALIB_CHAINS | 洞察組合、校正版本鏈假資料 | spec 04 |
| `settings-prompts.js` previewPrompt()/testSend() | 前端變數代入與模擬回覆 | POST /api/prompts/preview、/api/prompts/test（spec 06） |
| `vars.js` | 變數 registry + mock 預覽值 | GET /api/prompt-vars（spec 06） |
| `settings-llm.js` roles.master_model | AI 大師角色 mock | shared/llm 角色表擴充（spec 04 §4.3） |
| `settings-prompts.js` pd_evolution_cfg（localStorage） | 自我進化設定 | GET/PUT /api/evolution-config（spec 04） |
| `alerts.js` `PD_AI_SCORE` | 校準誤差 mock（F4 規則） | spec 04 ai-score |
| `app.js` F5 年內股利預估 chips | 前端稅前估算 | spec 05 dividend_projection |
| `mock-data.js` `returns.by_currency` | 各幣別報酬拆分 | 既有 `returns.by_currency` 已實作 — 確認 dashboard payload 已含此欄位即可 |
| `mock-data.js` 整份 | 儀表板 payload | GET /api/dashboard（spec 08） |
| `shell.js` refresh menu | toast 假動作 | POST /api/actions/refresh-quotes、/api/actions/recompute（spec 08） |
| `shell.js` `pdAuth`（localStorage pd_users/pd_session）、login.html | 帳密與守門全 mock | spec 09 auth API；接線後刪 localStorage 帳密邏輯 |
| `shell.js` `SYMBOLS` hardcode | 全域搜尋 registry | GET /api/instruments（spec 10） |
| `instruments.js` `INSTRUMENTS_DATA`、probe 流程 | 全 mock | spec 10；需 schema migration（target_low、board_status） |
| `ledger.js` `LEDGER_DATA` | 四帳本假資料 | GET /api/ledgers/{kind}（spec 11）；需 migration（transactions.fee_snapshot） |
| `input.js` `calcFees()`、`input-mock-data.js` | 費稅前端鏡像、假 CSV/AI 草稿 | spec 12：preview/commit 兩段式；前端鏡像僅留即時打字回饋 |
| `settings-accounts.html` 帳戶卡静態 HTML | 帳戶/費率寫死 | GET /api/accounts（spec 13） |
| `settings-datasources.js` `DATASOURCES_DATA` | 來源/金鑰/健康全 mock | spec 14；需新增 data_sources 三表 |
| `settings-scheduler.js` `SCHED_DATA` | jobs/歷史全 mock | spec 15；PUT 需動態 reschedule |
| `settings-llm.js` `LLM_DATA` | 模型/角色/額度/用量全 mock | spec 16（master 角色欄位見 spec 04 §4.3） |
