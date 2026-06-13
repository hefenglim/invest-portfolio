# Spec 06 — 數據變數系統、提示詞預覽/測試、外部資料快照（P1）

> 對應前端：`vars.js`（變數 registry mock）、`settings-prompts.js`（插入變數/變數總表/預覽提示詞/測試送出）。
> 原則：**LLM 不自行計算** — 變數值全部由計算核心即時組裝；外部抓取資料一律快照入庫，回測可重現當時輸入。

## 6.1 變數 registry

```
GET /api/prompt-vars
→ [{ "token": "holdings_json", "name": "持倉明細", "category": "部位與績效",
     "scope": "portfolio" | "per_symbol", "desc": "...", "available": true,
     "sample": "<截斷預覽值>" }]
```

變數清單（前端 `vars.js` 為準，共 8 類 24 個）：

| 類別 | 變數 | 來源 |
|---|---|---|
| 部位與績效 | holdings_json, allocation_json, kpis_json, returns_by_ccy_json, realized_json, symbol_detail_json | **已具備**（build_dashboard / spec 01） |
| 價格與技術 | price_history_json, ma_signals_json, volatility_json, price_vs_cost_json | 日線已具備；**ma/vol 為新計算函式**（pure，由日線推） |
| 股利 | dividends_json, ex_dividend_calendar_json, dividend_projection_json | 已具備 / spec 05 |
| 匯率 | fx_json, fx_rates_json | 已具備 |
| 籌碼與基本面 | institutional_json, margin_json, monthly_revenue_json, valuation_json, financials_json | **新增 FinMind ingest（6.3）** |
| 市場情緒 | market_sentiment_json, index_quotes_json | **新增來源（6.3）** |
| AI 自身 | backtest_json, calibration_gap_json | spec 04 evaluations 匯總 |
| 系統狀態 | freshness_json, as_of | 已具備 |

渲染引擎：`render_prompt(text, scope_ctx) -> str`，將 `{{token}}` 替換為 JSON 字串。
**SR 澄清（2026-06-13）— 未知/越權 token 的兩種路徑**：
- `POST /api/prompts/preview`（診斷用途）：**永遠 200**，問題列在 `unknown_tokens[]` /
  `scope_violations[]` 欄位，前端標紅提示。
- 正式執行路徑（`/api/prompts/test`、insight 排程、preflight 後的實跑）：未知 token → **422**
  （列出未知清單）；`per_symbol` 變數出現在 portfolio 範圍 → 同樣 422（= spec 04 R1）。

## 6.2 提示詞預覽與測試送出

```
POST /api/prompts/preview
  { "body": "...", "scope": "portfolio", "symbol": null }
  → { "system_prompt": "...", "rendered": "...",
      "tokens_used": ["holdings_json", ...], "unknown_tokens": [],
      "est_tokens": 1842 }                  -- 完整代入後的實際送出樣貌

POST /api/prompts/test
  { "body": "...", "scope": "portfolio", "symbol": null }
  → { "reply": "...", "model": "claude-sonnet", "via": "litellm",
      "tokens_in": 1842, "tokens_out": 96, "cost_usd": "0.0070",
      "quota_remaining": "0.83" }
```

test 規則：走既有 `shared/llm`（LiteLLM）、role=default、**成本照記 llm_usage（agent=prompt_test）並扣額度**；
不寫入洞察卡；額度歸零回 **402 `budget_exceeded`**（統一映射見 spec 08 §8.0）。preview 不呼叫 LLM、零成本。

## 6.3 外部資料快照 ingest（新排程 jobs）

> 你的硬性要求：「系統抓到的即時數據以及各種有價值的資訊必須妥善保存在資料庫」。

```
external_snapshots
  id, source TEXT,            -- 'finmind' | 'sentiment' | 'index'
  dataset TEXT,               -- 'institutional' | 'margin' | 'monthly_revenue' | 'valuation' |
                              -- 'financials' | 'vix_fng' | 'index_quotes'
  symbol TEXT NULL,           -- 標的級資料才有
  as_of DATE, payload JSON,   -- 原始回應（append-only，回測重現用）
  fetched_at
```

| job | 週期 | 內容 |
|---|---|---|
| `finmind_chips_daily` | 交易日收盤後 | 持倉+觀察清單台股的法人買賣超、融資券 |
| `finmind_fundamentals_monthly` | 每月 12 日 | 月營收；季報季加抓 financials |
| `finmind_valuation_daily` | 交易日 | PER/PBR |
| `sentiment_daily` | 每日 | VIX（yfinance ^VIX）、Fear & Greed（CNN API）|
| `index_quotes_daily` | 交易日 | TAIEX/SPX/KLCI 收盤 |

變數值 = 最新快照的衍生計算（如 consecutive_buy_days 由近 20 日快照算出）。
快照缺漏時變數回 `{"unavailable": true, "last_as_of": "..."}` — LLM 看得懂、freshness 一致。
FinMind 免費額度有限（研究筆記已驗證 600 req/hr）：jobs 依持倉數分批、加 backoff。

## 6.4 失敗模式
- FinMind 斷線 → 變數標 unavailable，洞察照常產生（提示詞已要求「資料未提供時明說」）。
- 任何 ingest job 失敗記 job_runs（既有），連續 3 次失敗發 warn 預警。

## 前端接線
- `vars.js` registry → GET /api/prompt-vars（available 欄位驅動「需後端新增」標記消失）。
- `settings-prompts.js` previewPrompt() → POST /api/prompts/preview（取代前端 V.render）；
  testSend() → POST /api/prompts/test（取代 setTimeout mock）。
