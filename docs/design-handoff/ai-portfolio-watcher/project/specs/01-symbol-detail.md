# Spec 01 — 個股詳情資料端點（P0）

## 目的
前端個股詳情抽屜（`detail.js`）需要一次取得：歷史日線、成本基準、配息史、交易事件、已實現記錄。
目前由 `history-mock.js` 以隨機種子模擬 — 本 spec 完成後刪除該檔。

## Endpoint

```
GET /api/symbol/{symbol}/detail?days=180
```

### Response（全部金額為 Decimal 字串）

```jsonc
{
  "symbol": "2330",
  "as_of": "2026-06-11",
  "price_history": {
    "available": true,
    "points": [{ "date": "2025-12-12", "close": "472.00" }],
    "last_date": "2026-06-11",          // 最後有效報價日
    "stale": false,                      // staleness 規則同 dashboard freshness
    "partial": false,                    // SR 補：回補逾時只回部分時 = true（見實作要點 1）
    "note": null                         // 例如「自 2026-05-30 起無報價」
  },
  "cost_basis": {                        // 來自既有 holdings 計算，原幣
    "account_id": "tw_broker",           // SR Q1 定案：預設＝持股最多帳戶，回聲實際採用值
    "original_avg": "500.00",
    "adjusted_avg": "495.00"
  },
  "dividend_events": [                   // 帳本 dividends，該 symbol 全部
    { "date": "2026-06-03", "type": "cash|stock|drip|net",
      "gross": "5000", "net": "5000",
      "reinvest_shares": null, "reinvest_price": null, "ccy": "TWD" }
  ],
  "trade_events": [                      // 帳本 transactions + opening_positions
    { "date": "2026-01-02", "side": "open|buy|sell", "shares": "500", "price": "480.00" }
  ],
  "realized_rows": [ /* 與 dashboard realized.rows 同 shape，filter by symbol */ ]
}
```

## 實作要點
1. **price_history** 走既有 `pricing.get_price_history`；缺資料時先觸發歷史回補（同步等待上限 ~3s，逾時回傳已有部分並標 `partial: true`）。
2. 觀察清單（非持倉）標的：`cost_basis`、`realized_rows` 為 `null`/空陣列，前端已處理此狀態。
3. 事件日期若落在價格序列之外（早於最早日線），照樣回傳 — 前端會略過繪圖但顯示在表格。
4. 快取：同一 symbol+day 可快取至當日收盤後下一次 quotes 排程。

## 前端接線
- `detail.js` 將 `H.series(symbol)` / `H.events(symbol)` 改為此 endpoint 的單次 fetch。
- 持倉表 sparkline（`app.js sparkline()`）重用同一資料 — 建議 dashboard payload 直接附帶
  `holdings[].spark_30d: ["612.5", ...]`（最近 22 個交易日 close），避免 N+1 請求。
