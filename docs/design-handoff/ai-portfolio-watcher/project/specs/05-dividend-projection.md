# Spec 05 — 年度股利現金流預估（F5，P2）

## 目的
除息日曆面板的「年內股利預估」chips（`app.js renderExDivCalendar`）目前由前端以
「已宣告事件 × 持有股數」即時計算。後端化的理由：稅後淨額需要各帳戶股利模式
（TW 全額 / Schwab 預扣 30% / Moomoo US 0.99 平台費 / MY 淨額），這些規則在費稅引擎裡。

## Dashboard payload 新增欄位

```jsonc
"dividend_projection": {
  "year": 2026,
  "by_currency": {                       // 各幣別分列，絕不加總
    "TWD": { "declared_gross": "21400", "declared_net": "21400", "events": 3 },
    "USD": { "declared_gross": "7.50",  "declared_net": "5.25",  "events": 1 }
  },
  "basis": "declared_only"               // v1: 只算已宣告（ex_dividend_calendar）
}
```

## 計算規則
1. **v1（本 spec）**：只統計 `ex_dividend_calendar` 中、持倉中標的、`ex_date` 落在本年度剩餘期間的已宣告事件：`shares × cash_amount`。
2. 淨額 = 套用該持倉帳戶的股利模式（與入帳邏輯同一套規則，但不寫入）。
3. **v2（之後）**：`basis: "declared_plus_estimated"` — 對歷史配息頻率穩定的標的（近 12 月 ≥2 次同頻率），以 trailing 金額推估未宣告的剩餘期數，標 `estimated: true` 分開列示。前端會以虛線/淡色區分。

## 前端接線
- `app.js` 的 F5 chips 改讀 `D.dividend_projection.by_currency`，顯示 net（title 註明 gross）。
- 月曆視圖（E3）不變。
