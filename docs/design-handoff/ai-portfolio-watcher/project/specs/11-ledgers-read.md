# 11 — 四帳本讀取（P0）

> 前端現況：`ledger.js window.LEDGER_DATA` 全 mock（trades.html / ledger.html 共用）。
> 後端 `data_ingestion/store.py` 已有 `list_transactions / list_dividends /
> list_fx_conversions / list_opening`，但無路由；且交易列**缺 `fee_snapshot`**
> （寫入當下的費率規則快照 — 前端逐列顯示，spec 02 對帳匯出也需要）。

## 11.1 GET /api/ledgers/{kind}

### Endpoint & Method
```
GET /api/ledgers/transactions
GET /api/ledgers/dividends
GET /api/ledgers/fx
GET /api/ledgers/openings
```

### Description
四本 append-only 帳本的唯讀清單，供帳本頁表格＋共用過濾列（帳戶 chips、代號搜尋、日期區間）。
**無任何寫入路由** — 寫入只能走輸入中心（spec 12）。

### Request Structure
- Query（四個 endpoint 共用）：
  | 參數 | 型別 | 預設 | 說明 |
  |---|---|---|---|
  | `account_id` | string | — | 帳戶過濾（`tw_broker` 等 id，非顯示名） |
  | `symbol` | string | — | 代號過濾（fx 帳本忽略此參數） |
  | `from` / `to` | date (ISO) | — | 日期區間（含端點） |
  | `limit` / `offset` | int | 200 / 0 | 分頁（依日期＋rowid 倒序） |

### Response Structure
`transactions` **200**：
```jsonc
{ "rows": [
    { "id": 412, "date": "2026-06-09", "account_id": "tw_broker", "account": "台灣券商",
      "symbol": "0056", "name": "元大高股息", "side": "buy",
      "shares": "2000", "price": "38.60", "fee": "110", "tax": "0",
      "total": "-77310",                       // 買=負（現金流向），賣=正淨收款
      "ccy": "TWD",
      "fee_snapshot": { "rate": "0.001425", "discount": "1.0", "min_fee": "20",
                        "round_int": true, "tax_sell": "0.003",
                        "label": "0.1425%・最低 NT$20・賣出證交稅 0.3%" },  // 可為 null（舊資料）
      "note": null } ],
  "total_count": 152 }
```
`dividends` **200**（`type`: `"cash" | "stock" | "drip" | "net"`；前端映射 現金/配股/DRIP/淨額）：
```jsonc
{ "rows": [
    { "id": 88, "date": "2026-05-20", "account_id": "schwab", "account": "嘉信 Schwab",
      "symbol": "AAPL", "type": "drip", "gross": "7.50", "withhold": "2.25", "net": "5.25",
      "reinvest_shares": "0.0248", "reinvest_price": "211.40", "ccy": "USD" } ],
  "total_count": 31 }
```
`fx` **200**：
```jsonc
{ "rows": [
    { "id": 7, "date": "2026-05-26", "account_id": "schwab", "account": "嘉信 Schwab",
      "from_ccy": "TWD", "from_amt": "32000", "to_ccy": "USD", "to_amt": "1000.00",
      "implied_rate": "32.0000" } ],
  "total_count": 9 }
```
`openings` **200**：
```jsonc
{ "rows": [
    { "id": 1, "date": "2026-01-02", "account_id": "tw_broker", "account": "台灣券商",
      "symbol": "2330", "shares": "500", "avg": "480.00", "total": "240000", "ccy": "TWD" } ],
  "total_count": 4 }
```
**400**：`from > to` 或非法日期 →
`{ "error": { "code": "validation_error", "message": "日期區間無效", "field": "from" } }`

### Python Backend Implementation Notes
- 直接包裝 `store.list_*`；`account`（顯示名）由 `store.list_accounts` join。
  帳戶過濾 chips 由前端改讀 `GET /api/accounts`（spec 13）建構，不再 hardcode 顯示名。
- **SR 定案 — `fee_snapshot` 存 raw 值非顯示字串**：欄位 = 計費當下的 `FeeRuleSet` raw Decimal
  字串 ＋ `label`（人讀一行）。前端 ledger.js 接線時改為只渲染 `label`（舊 mock 逐 key
  顯示字串的作法廢棄）；spec 02 對帳匯出用同一份 raw 值，兩者永不分岐。
- **Enum 線上格式**：`side`/`type` 一律小寫（spec 08 §8.0）；**`DividendType` 需擴充 `NET`**
  （後端現只有 CASH/STOCK/DRIP，馬股淨額入帳前端已使用 — `shared/models/enums.py` 加值，
  `dividend_model.apply_dividend_model` 同步支援）。
- `implied_rate` 用 `StoredFxConversion.implied_rate` property（4 位小數字串）。
- **Schema migration**：`transactions` 表新增 `fee_snapshot TEXT NULL`（JSON 字串）。
  寫入路徑（spec 12 manual/csv/ai 與既有 `store.insert_transaction` 呼叫端）在計費當下
  以 `fees.compute_fees` 用到的規則組 snapshot dict 存入。舊資料回 `null`，前端顯示「—」。
- `total` 符號約定與前端一致：buy = `-(gross+fee+tax)`、sell = `+(gross-fee-tax)`，
  由後端計算回傳，前端不再自行運算。
