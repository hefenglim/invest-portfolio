# 10 — 標的註冊與觀察清單（P0）

> 前端現況：`instruments.js window.INSTRUMENTS_DATA` 全 mock；`shell.js` 的全域搜尋
> （Cmd+K）用 hardcode `SYMBOLS` 陣列。後端已有 `data_ingestion/register.py`、
> `data_ingestion/store.py`（list/get/upsert_instrument）、`pricing/board.py`（probe_tw_board），
> 但無路由；且 `instruments` 表**缺 `target_low` 欄位**（目標價提醒）。

## 10.1 GET /api/instruments

### Endpoint & Method
`GET /api/instruments`

### Description
觀察清單整頁資料：所有已註冊標的＋現價/漲跌＋是否持有＋目標價。
同一 payload 供 `shell.js` 全域搜尋使用（取代 hardcode SYMBOLS）。

### Request Structure
- Query：
  | 參數 | 型別 | 預設 | 說明 |
  |---|---|---|---|
  | `q` | string | — | 代號/名稱模糊過濾（後端做也可前端做；v1 可省略） |

### Response Structure
**200**（shape = `INSTRUMENTS_DATA`）：
```jsonc
{
  "as_of": "2026-06-12T10:00:00+08:00",
  "list": [
    { "symbol": "2330", "name": "台積電", "market": "TW", "board": "TWSE",
      "sector": "半導體", "ccy": "TWD", "held": true,
      "last": "612.5", "chg_pct": "0.012",        // null = 缺價（前端顯示「缺價」badge）
      "target_low": null },                        // Decimal string | null
    { "symbol": "8069", "name": "元太", "market": "TW", "board": null,   // null = 板別未解析
      "sector": "光電", "ccy": "TWD", "held": false,
      "last": null, "chg_pct": null, "target_low": "220" }
  ]
}
```
**500**：共同錯誤格式。

### Python Backend Implementation Notes
- `store.list_instruments(conn)` ＋ `pricing.store.get_latest_price`（批撈）；
  `chg_pct` = 最近兩個 close 的變動率（`get_price_history` 取 2 筆）。
- `held`：由帳本推（`data_ingestion.holdings.current_shares > 0` 任一帳戶）—
  一次 SQL 聚合，勿逐檔呼叫。
- `board` 序列化規則：DB 空字串且 market=US → `""`；TW 且未解析 → `null`
  （需新增 `board_resolved INTEGER` 欄位或以 sentinel 值區分「預設 TWSE」與「已確認 TWSE」—
  採後者：新增欄位 `board_status TEXT CHECK(board_status IN ('resolved','unresolved')) DEFAULT 'resolved'`）。

---

## 10.2 POST /api/instruments/probe

### Endpoint & Method
`POST /api/instruments/probe`

### Description
台股代號板別探測（TWSE vs TPEx）：註冊流程第一步，回傳判定供使用者確認。

### Request Structure
- Body：`{ "symbol": "2330" }`

### Response Structure
**200**：
```jsonc
{ "symbol": "2330", "name": "台積電", "board": "TWSE",
  "board_label": "TWSE 上市" }
// 探測失敗（兩板皆無資料）：
{ "symbol": "8069", "name": null, "board": null, "board_label": "未解析" }
```
**400**：symbol 空白。**503**：行情來源全數失敗
`{ "error": { "code": "provider_unavailable", "message": "TWSE/TPEx 來源皆無回應" } }`。

### Python Backend Implementation Notes
- `pricing.board.probe_tw_board(...)`（注入 `TwseProvider`/`TpexProvider`）。
- 探測失敗 ≠ 錯誤：回 200 + `board:null`，前端顯示「以預設 TWSE 抓報價、板別待確認」banner。

---

## 10.3 POST /api/instruments ・ PUT /api/instruments/{symbol}

### Endpoint & Method
```
POST /api/instruments            → 註冊新標的
PUT  /api/instruments/{symbol}   → 更新（板別改判、sector、name、target_low）
```

### Description
註冊（probe 確認後送出明細表單）與後續修正。註冊成功後標的立即進入排程抓價工作清單
（`scheduler.jobs.build_worklist` 讀同一張表，無需額外動作）。

### Request Structure
- `POST` Body：
  ```jsonc
  { "symbol": "6488", "market": "TW",            // "TW"|"US"|"MY" 必填
    "name": "環球晶", "sector": "半導體",
    "board": "TPEx",                              // TW 限定；null = 未解析暫存
    "quote_ccy": "TWD",                           // 可省略：TW→TWD, US→USD, MY→MYR
    "target_low": "450" }                         // 選填 Decimal string
  ```
- `PUT` Body：以上欄位的任意子集（symbol/market 不可改）。

### Response Structure
**201 / 200** 回完整 instrument 列（同 10.1 list 元素 shape）。
**409**：`{ "error": { "code": "duplicate_symbol", "message": "2330 已註冊" } }`
**404**（PUT）：symbol 不存在。**400**：market 與 board 組合非法（US/MY 不可帶 TW 板別）。

### Python Backend Implementation Notes
- `register.register_instrument(conn, InstrumentDraft(...))`；更新走 `store.upsert_instrument`。
- **Schema migration**：`instruments` 表新增
  `target_low TEXT NULL`、`board_status TEXT NOT NULL DEFAULT 'resolved'`、
  **`is_etf INTEGER NOT NULL DEFAULT 0`**（SR 定案：ETF 判定的唯一來源，
  註冊表單勾選；`fees.compute_fees` 的 `tax_sell_etf` 與 spec 12 `instruments[].etf`
  皆讀此欄，禁止用 sector=="ETF" 推導）—
  用既有 `data_ingestion.schema._add_column_if_missing`，並把欄位補進
  `shared/models/assets.py Instrument`（pydantic，`target_low: Decimal | None = None`、`is_etf: bool = False`）。
- `target_low` 同時是 spec 03 預警規則 `target_price` 的資料來源 — 寫入後預警即生效。
