# 12 — 輸入中心：手動交易／CSV 匯入／AI 輸入（P0）

> 前端現況：`input.js`（5 tabs：手動/CSV/AI/股利/換匯期初）全 mock，費稅以
> `input-mock-data.js fee_rules` 前端鏡像計算。後端核心**齊備但無路由**：
> `manual.enter_transaction`、`validate.validate_transaction`、`fees.compute_fees`、
> `csv_import / dividend_import / fx_import / opening_import` 的 `build_*_preview`、
> `preview.commit_preview`、`agents.ai_agents_input`。
> 鐵律：**preview 系列純計算絕不寫入；commit 才寫帳本（append-only）**。

## 12.1 GET /api/input/context

### Endpoint & Method
`GET /api/input/context`

### Description
輸入中心開頁一次撈：帳戶（含股利模式）、費率規則、標的清單、目前持股 —
取代 `window.INPUT_DATA`。費率規則仍下發前端做**即時打字回饋**；
權威數字以 12.2 preview 為準。

### Request Structure
無參數。

### Response Structure
**200**（shape = `input-mock-data.js`，金額/費率為 Decimal string）：
```jsonc
{
  "accounts": [ { "id": "tw_broker", "name": "台灣券商", "ccy": "TWD", "div_model": "tw" } ],
  "fee_rules": { "tw_broker": { "rate": "0.001425", "discount": "1.0", "min_fee": "20",
                  "round_int": true, "tax_sell": "0.003", "tax_sell_etf": "0.001",
                  "label": "0.1425%・最低 NT$20・賣出證交稅 0.3%（ETF 0.1%）" } },
  "instruments": [ { "symbol": "2330", "name": "台積電", "market": "TW", "ccy": "TWD", "etf": false } ],
  "holdings": { "tw_broker": { "2330": "1000" } }
}
```

### Python Backend Implementation Notes
- `store.list_accounts` ＋ `config_seed`（FeeRuleSet → 序列化含 `label` 組字）＋
  `store.list_instruments`（`etf` 由 sector=="ETF" 或新欄位推導 — 與 `fees.compute_fees`
  的 ETF 稅率判定用同一來源）＋ `holdings.current_shares` 批次聚合。

## 12.2 POST /api/input/manual/preview ・ POST /api/input/manual/commit

### Endpoint & Method
```
POST /api/input/manual/preview   → 純計算（債務：取代前端 calcFees 為權威）
POST /api/input/manual/commit    → 寫入交易帳本
```

### Description
手動單筆交易。preview 回費稅與 issues（含「賣超持有」軟警告）；
commit 在使用者確認後寫入，軟警告需明確帶 `ack_oversell:true` 才放行。

### Request Structure
- Body（兩 endpoint 同 shape；數值欄位收 string 或 number，後端一律轉 `Decimal`）：
  ```jsonc
  { "account_id": "tw_broker", "symbol": "2330", "side": "buy",   // "buy"|"sell"
    "date": "2026-06-11", "shares": "1000", "price": "612.5",
    "fee_override": null, "tax_override": null,    // 非 null = 使用者鉛筆覆寫
    "note": null,
    "ack_oversell": false }                        // 僅 commit 用
  ```

### Response Structure
`preview` **200**：
```jsonc
{ "fee": "873", "tax": "0", "gross": "612500", "total": "-613373",
  "fee_rule_label": "0.1425%・最低 NT$20・賣出證交稅 0.3%（ETF 0.1%）",
  "fee_overridden": false, "tax_overridden": false,
  "issues": [ { "sev": "warn", "code": "oversell",
                "text": "賣出股數 1,500 超過持有 1,000 — 輸入錯誤還是放空？",
                "field": "shares" } ] }
```
`commit` **201**：`{ "txn_id": 413, "total": "-613373" }`
**400**：硬錯誤（未知代號／shares≤0／price≤0）— 同 issues shape 放進 `error.issues`。
**422**：`{ "error": { "code": "oversell_unacknowledged", "message": "需確認賣超" } }`

### Python Backend Implementation Notes
- `validate.validate_transaction(conn, TxnInput(...))` → issues；
  `fees.compute_fees(...)` → fee/tax（覆寫值優先，但 issues 仍照算提示差異）。
- commit 走 `manual.enter_transaction`；捕捉 `OversellError` → 422。
- 寫入時組 `fee_snapshot`（spec 11）一併存。

## 12.3 POST /api/import/preview ・ POST /api/import/commit

### Endpoint & Method
```
POST /api/import/preview
POST /api/import/commit
```

### Description
CSV 匯入（交易/股利/換匯/期初四種）與 AI 草稿共用的兩段式流程：
preview 逐列驗證＋計費 → 前端表格顯示 ok/warn/error → commit 寫入非 error 列。

### Request Structure
- `preview` Body：
  ```jsonc
  { "kind": "transactions",          // "transactions"|"dividends"|"fx"|"openings"
    "csv_text": "date,account_id,side,symbol,shares,price\n2026-06-02,..." }
  ```
- `commit` Body：
  ```jsonc
  { "kind": "transactions",
    "rows": [ /* preview 回傳的 rows 原樣回傳（可剔除使用者取消勾選的列） */ ],
    "ack_warnings": true }
  ```

### Response Structure
`preview` **200**（= `preview.ImportPreview` 序列化）：
```jsonc
{ "rows": [
    { "n": 1, "status": "ok",   "reason": null,
      "data": { "date": "2026-06-02", "account_id": "tw_broker", "side": "buy",
                "symbol": "0056", "shares": "2000", "price": "38.60",
                "fee": "110", "tax": "0" } },
    { "n": 2, "status": "warn", "reason": "賣出股數 1,500 超過持有 1,000 — 可寫入，請確認是否放空", "data": {} },
    { "n": 3, "status": "error", "reason": "未知代號 23300 — 不在標的清單，已排除", "data": {} } ],
  "summary": { "total": 3, "ok": 1, "warn": 1, "error": 1 } }
```
`commit` **200**（= `ImportSummary`）：`{ "written": 2, "skipped": 1 }`
**400**：CSV 表頭缺必要欄位／kind 非法。
**422**：rows 含 warn 但 `ack_warnings:false`。

### Python Backend Implementation Notes
- kind → builder 映射：`build_transaction_preview / build_dividend_preview /
  build_fx_preview / build_opening_preview`；commit 用 `preview.commit_preview(conn, rows, writer)`，
  writer 對應 `write_transaction_row / write_dividend_row / write_fx_row / write_opening_row`。
- `PreviewRow` 是 pydantic — 直接 round-trip（commit 收回的 rows 重新 `PreviewRow.model_validate`，
  **status=error 列即使被送回也必須拒寫**）。
- commit 全部列包在單一 transaction：任一列寫入失敗 → rollback ＋ 500。

## 12.4 POST /api/input/ai/preview

### Endpoint & Method
`POST /api/input/ai/preview`

### Description
AI 自然語言/截圖輸入 → 結構化交易草稿（preview shape 同 12.3，之後走同一個
`/api/import/commit` 寫入）。

### Request Structure
- Body（文字）：`{ "text": "在元大買 10 股 2330 @ 600" }`
- Body（截圖，Phase 2）：multipart `file` 欄位（PNG/JPG ≤ 5MB）— 走 vision 角色。

### Response Structure
**200**：同 12.3 preview ＋ `"meta": { "model": "claude-sonnet", "via": "litellm", "cost_usd": "0.012" }`
LLM 降級錯誤碼（統一映射見 spec 08 §8.0）：**402** `budget_exceeded`、**409** `ai_not_activated`、
**503** `llm_unavailable`（對應 `shared/llm_config` 三個例外的 `kind`）。

### Python Backend Implementation Notes
- 文字版直接 `agents.ai_agents_input(conn, text)`（內部已走 validate＋計費同一管線）。
- 截圖版：`agents.py` 需新增 vision completer 入口（prompt 同 `_PROMPT`，圖片以
  base64 附件送 LiteLLM vision 角色）— 結果同樣餵 `txn_preview_row`。
- 費用記入 LLM 用量帳（agent=`ai_agents_input`，spec 16 usage.by_agent 讀同一帳）。
