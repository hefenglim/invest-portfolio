# Spec 04 — llm_insight：洞察組合器、AI 戰績回測、1:1 專屬校正提示詞（P1–P2）

> **最終定案（2026-06-12 使用者拍板，取代本檔所有舊版）**
> - 策略提示詞 = 純設計物件：無排程、無校正掛載；搭配數據變數（spec 06）組裝，可預覽與測試送出。
> - 洞察類型（組合）= 排程與自我校正的唯一掛載點：系統提示詞(可選) ＋ 1..n 策略 ＋ 自我校正開關。
> - 校正提示詞 1:1 掛組合；版本鏈演進；**封存制（軟刪除）— 永不物理刪除**。
> - 回測評分與校正生成由新 LLM 角色 **AI 大師模型（master）** 執行。
> 對應前端：`insights.html`、`settings-prompts.js`（組合器/校正庫/進化設定）、`settings-llm.js`（master 角色）。

## 4.0 資料模型

```
strategy_prompts                       -- 純設計物件
  id, name, body TEXT,                 -- body 內含 {{var}} tokens（spec 06）
  enabled BOOL, archived BOOL,         -- archived = 軟刪除
  created_at, updated_at

insight_types                          -- 洞察組合（排程/校正掛載點）
  id, name, scope TEXT,                -- 'per_symbol' | 'portfolio' | 'on_alert'
  use_system_prompt BOOL DEFAULT true, -- 系統提示詞可選
  self_correct BOOL DEFAULT false,     -- 自我校正開關
  universe JSON NULL,                  -- per_symbol 專用：{"mode":"all"} 或 {"mode":"custom","symbols":[...]}
                                       --   custom 可含持倉與觀察清單標的
  alert_rules JSON NULL,               -- on_alert 專用：'all' 或 ["fx_drift",...]（與 alert-rules 設定互通）
  enabled BOOL, archived BOOL,
  job_id TEXT NULL                     -- 排程工作表的 job（共用 scheduler）

insight_type_strategies                -- 多策略 (ordered)
  insight_type_id FK, strategy_prompt_id FK, position INT

calibration_prompts                    -- 1:1 鏈：每組合一條，版本遞增
  id, insight_type_id FK, version INT,
  archived BOOL DEFAULT false,         -- 軟刪除：選擇器隱藏、歸因保留
  body TEXT, cause TEXT,
  created_at
  -- 「生效版」記在 insight_types.active_calibration_version INT NULL（手動版本選擇器）

insight_evaluations                    -- AI 大師模型回測評分（每版累計成績的來源）
  id, insight_id FK, insight_type_id FK,
  calibration_version INT NULL,        -- 當次套用版本（影子評估記影子版本＋ is_shadow=true）
  is_shadow BOOL DEFAULT false,
  quant_hit BOOL NULL,                 -- 量化預測：程式比對價格（客觀，無 LLM）
  narrative_score INT NULL,            -- 敘事準確度：大師模型評分 0–100
  miss BOOL,                           -- 綜合判定未命中
  notes TEXT, evaluated_at
```

組裝順序（硬規則）：`系統提示詞(若啟用) + 策略1 + 策略2 + … + 生效校正版本(若 self_correct 且有生效版)`。
校正只能附加、不得改寫上層；數字一律來自注入變數（spec 06）。

## 4.1 刪除/封存連動規則

| 動作 | 行為 |
|---|---|
| 刪除策略提示詞 | 先查 `insight_type_strategies` 引用：**被引用 → 409 拒絕**（回傳引用組合清單）；未引用但有歷史 → `archived=true`；從未使用 → 可物理刪除 |
| 刪除洞察組合 | `archived=true`＋**同步刪除排程工作表 job**；校正鏈整條封存；歷史洞察/評估保留 |
| 刪除校正版本 | 一律 `archived=true`（軟刪除）；若該版正生效 → `active_calibration_version=NULL` |

## 4.2 排程掛載（與 scheduler 共用）

```
POST /api/insight-types/{id}/schedule   { "cron": "0 8 * * *" }
  → 在排程工作表建立/更新 job（kind=insight, payload=insight_type_id），回傳 job_id
DELETE /api/insight-types/{id}/schedule → 移除 job
```
前端「啟動排程」彈窗只是 cron 的友善包裝；之後的週期變更走既有排程設定頁（同一 job 記錄）。
`scope=on_alert` 的組合不可排程，由預警事件觸發（spec 03）。

## 4.3 AI 大師模型（master role）

- `shared/llm` 角色表新增 `master_model` / `master_fallback`（前端已加選單）。
- 職責：(1) 洞察回測評分（narrative_score）(2) 未命中聚類分析 (3) 校正新版本生成。
- 未設定 master → 自我校正管線整體暫停（洞察照常產生）；degrade 行為同額度歸零。
- 全部呼叫記入 llm_usage（role=master），受同一額度治理。

## 4.4 回測評分管線（每日排程 job `evaluate_insights`）

1. 取到期洞察（依預測 horizon 或洞察聲明的時間點）。
2. **量化先行（免 LLM）**：price_change/volatility 類預測由程式比對實際價格 → quant_hit。
3. **敘事評分**：大師模型輸入「洞察原文＋當時輸入快照＋當下實際數據」→ narrative_score 0–100 ＋ miss 判定＋原因 note。
4. 寫入 insight_evaluations；每版累計成績（評估次數/均分/失誤率）由查詢匯總，前端版本管理器顯示。

## 4.5 校正版本生成（每週排程 job `generate_calibrations`）

觸發條件（任一，且該組合 resolved 樣本 ≥ `min_samples`）：
- 同組合連續 ≥3 次 miss
- 失誤率/校準誤差超過 `gap_alert_pp`
- 輸出規則違規（幣別混算、越權建議 — 由輸出驗證器記錄）

大師模型輸入：現行生效版 body、失誤樣本明細（evaluations + notes）、分桶命中率。
輸出：**完整新版本**（保留有效條款、修訂失效條款；個股失誤寫成「（個股）…」條款）→ 版本鏈 +1。

## 4.6 影子評估語義（定案）

- **生效版 = 使用者手動選定**（`active_calibration_version`）。
- **生效版 ≠ 最新版 → 最新版自動成為影子**：同批次並行產出（不展示），其 evaluations 標 `is_shadow=true` 照常累計成績。
- 生效版 = 最新版 → 無影子（成本歸零）。
- 影子累計 `shadow_batches` 次且成績不劣於生效版 → 「勝出」：`auto_promote=true` 自動切換生效，否則前端提示人工「設為生效」。
- 同時影子數上限 `max_shadows`，超過排隊。
- 生效版 rolling 成績轉差（n≥8）→ 發 info 預警（`calibration_regression`，spec 03）。

## 4.7 API

```
GET    /api/insight-types                      → 組合列表（含 strategies[], self_correct, schedule, calib summary）
POST   /api/insight-types                      → 新增
PUT    /api/insight-types/{id}                 → 改名/範圍/策略組/開關
DELETE /api/insight-types/{id}                 → 封存＋刪排程（4.1）
POST   /api/insight-types/{id}/schedule        → 掛排程（4.2）
DELETE /api/insight-types/{id}/schedule
PUT    /api/insight-types/{id}/active-calibration   { "version": 2 | null }   → 手動版本選擇器
GET    /api/calibrations?insight_type={id}&include_archived=true
POST   /api/calibrations/{id}/archive          → 軟刪除
GET    /api/calibrations/{id}/samples          → 驅動該版的失誤樣本
GET    /api/ai-score                           → 戰績總表（totals/by_combo/calibration_bins/rows）
GET/PUT /api/evolution-config                  → auto_promote/shadow_batches/min_samples/max_shadows/gap_alert_pp
```

## 4.8 安全邊界（全部可在前端調整）
1. 校正只能附加；生成文字過驗證器（禁越權語句，關鍵字＋一次 LLM 審查）。
2. 影子成本受 max_shadows 上限；額度歸零管線暫停。
3. 全部版本 append-only 封存制，任何時刻可回退、歸因鏈永不斷。
4. 聚類與勝出判定為確定性程式碼；LLM 只寫校正文字與敘事評分。

## 4.9 Runtime 守門規則（組合排列審查定案，2026-06-12）

執行一個洞察組合前，後端依序檢查；任何擋下都寫 job_runs（status=skipped, reason）：

| # | 情境 | 行為 |
|---|---|---|
| R1 | **範圍×變數範圍不符**：非 per_symbol 組合引用含 per_symbol 變數的策略 | 建立/更新時 422 拒絕（前端組合器已在勾選時禁用＋既有列標紅警示）；既有資料跑到時 skip＋warn 預警 |
| R2 | **標的宇宙生命週期**（per_symbol）：custom 清單中的標的出清或移出觀察清單 | 自動從 universe.symbols 移除＋info 預警；**清單空 → enabled=false（自動關閉）＋warn 預警**；mode=all 自動跟隨持倉 |
| R3 | **策略全部停用/封存**：組合的策略段全空 | 該次執行 skip＋warn 預警；不自動關閉（策略恢復即繼續） |
| R4 | **缺價標的**（per_symbol 迭代中） | 不呼叫 LLM，直接產確定性「資料異常」卡（零成本）；計入 freshness |
| R5 | **變數資料不可用**（如 FinMind 變數遇美股/馬股、外部源斷線） | 變數代入 `{"unavailable":true}`，照常執行（系統提示詞要求「資料未提供時明說」） |
| R6 | **額度耗盡（迭代中途）** | 中止剩餘標的，job_runs 標 partial；已產卡保留 |
| R7 | **on_alert 觸發過濾** | 僅 alert_rules 命中（'all' 或含該 rule id）且該規則 enabled 時觸發；同一規則同一標的 24h 內不重複觸發（防抖）。**多個 on_alert 組合並存是合法的**：一條規則命中時，每個監聽它的 enabled 組合各產一張解讀卡（各自計費）；防抖以（組合, 規則, 標的）為鍵獨立計算。新建的 on_alert 組合預設 enabled=false，啟用後才參與觸發 |
| R8 | **執行單位** | 一次組合執行 = 一張卡（per_symbol 為每標的一張）：多策略串接在**同一個** LLM 呼叫內，非每策略一張卡 |

前端對應（已實作）：策略卡「全組合/單一標的」範圍徽章（由變數自動推導）、新增組合時不相容策略禁用、組合列 mismatch 紅框、per_symbol 組合「標的」chip（全部持倉/自選含觀察清單＋生命週期說明）、on_alert 組合「觸發」chip（規則多選）、預警規則頁每條規則顯示「⚡ 觸發 AI 解讀：…」互通指示。

## 前端接線
- `settings-prompts.js`：COMPOSER_DATA（含 universe / alert_rules）→ insight-types API；CALIB_CHAINS → calibrations API；版本選擇器 → active-calibration API；進化設定 localStorage `pd_evolution_cfg` → evolution-config API。
- `settings-alerts.js` 每條規則的「觸發 AI 解讀」指示 ← 讀 insight-types 的 on_alert 組合。
- `settings-llm.js` roles → master_model/master_fallback 欄位。
- `insights.html` 戰績 tab → /api/ai-score。
