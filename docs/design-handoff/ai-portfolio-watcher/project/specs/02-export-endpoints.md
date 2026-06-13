# Spec 02 — 對帳級匯出端點（P0）

## 目的
前端「匯出中心」（設定 › 匯出中心，`settings-alerts.js`）需要後端以 **raw Decimal 精度**產生對帳級檔案。
頁面各表格的「⬇ 匯出 CSV」為前端顯示值匯出（已可用，不需後端）；本 spec 只涵蓋對帳級匯出。

## Endpoints

```
POST /api/export/holdings        → holdings_snapshot_{as_of}.csv
POST /api/export/ledgers         → ledgers_{as_of}.zip
POST /api/export/llm-usage       → llm_usage_{from}_{to}.csv
POST /api/export/job-runs        → job_runs_{from}_{to}.csv
POST /api/export/tax-package     → tax_package_{year}.zip      (body: {"year": 2026})
```

全部回 `Content-Disposition: attachment`。金額欄輸出 raw Decimal 字串（不千分位、不四捨五入超過帳本精度）、UTF-8 with BOM、CRLF。

## 各檔案內容

### holdings snapshot
欄：symbol, name, market, board, account_id, quote_ccy, shares, original_avg, adjusted_avg,
original_cost_total, adjusted_cost_total, market_price, price_as_of, price_stale,
market_value, unrealized_pnl, capital_gain, dividend_portion, payback_ratio, weight,
reporting_ccy_value
＋檔尾註解列：`# as_of=..., fx_rates={USD:..., MYR:...}, generated=...`

### ledgers zip
四帳本各一 CSV（欄位 = 資料表欄位原樣）＋ `fee_rules_snapshot.json`（產出當下費率規則）＋ `manifest.json`（列數、as_of、schema version）。

### tax package zip（年度報稅包）
- `realized_gains_{year}.csv` — 該年度每筆已實現損益（原幣 + 報告幣別換算 + 使用之匯率）
- `dividends_{year}.csv` — 股利收入明細：gross、withholding、net、type、幣別；**各幣別分列，絕不合計**
- `fx_realized_{year}.csv` — 已實現匯損益歸因明細
- `summary.md` — 各幣別小計（人讀）

## 實作要點
1. 匯出為同步產生（資料量個人級，<10MB）；若超過 3s 改為 job + 輪詢，但 v1 不需要。
2. `tax-package` 的年度切割以**交易日**（賣出日/配息發放日/換匯日）為準。
3. 所有匯出寫一筆 `job_runs`（kind=export）供稽核。

## 前端接線
- `settings-alerts.js` 匯出中心卡片的「產生並下載」按鈕 → 對應 endpoint，成功後直接觸發下載。
- 年度下拉值傳入 tax-package body。
