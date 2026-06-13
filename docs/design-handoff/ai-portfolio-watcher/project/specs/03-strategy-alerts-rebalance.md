# Spec 03 — strategy/ 模組：預警規則引擎、買賣/再平衡試算（P1）

## 目的
三個前端功能目前在瀏覽器端鏡像計算，需移到後端成為單一事實來源：
1. 風險預警（`alerts.js computeAlerts()`）
2. 買賣試算（`detail.js` simSection 的 feeTax 鏡像）
3. 再平衡試算（`rebalance.js`）

全部落在 **`strategy/` 新模組**（rules 已預留：pure functions over computed outputs，不寫帳本）。

## 3.1 預警規則引擎

```
GET /api/alerts
```
> **SR 澄清**：本 endpoint 與 dashboard payload 內嵌的 `alerts` **必須呼叫同一個計算函式**
> （strategy 模組 `compute_alerts(conn, now)`）。定位：非儀表板頁面鈴鐄的輕量刷新；
> 儀表板頁一律讀內嵌欄位，不另打這支。

```jsonc
{
  "as_of": "...",
  "alerts": [
    { "id": "single_weight:2330", "sev": "risk|warn|info",
      "rule": "single_weight", "title": "...", "detail": "...",
      "href": "/symbol/2330" }
  ]
}
```

規則（v1，與前端 alerts.js 一致）：
| rule id | 預設門檻 | sev |
|---|---|---|
| single_weight | 權重 > 30% | risk |
| sector_weight | 指定產業群權重 > 60% | risk |
| stale_price / missing_price | freshness 規則 | warn |
| fx_drift | 池均價 vs 現匯 ±3% | info |
| exdiv_upcoming | 持倉除息日 ≤ 14 天 | info |
| quota_low | LLM 額度 < 警示門檻 | warn（=0 升級 risk） |

> **SR 澄清**：`quota_low` 門檻**不寫死 $1.00**，讀 spec 16 `quota.alert_threshold_usd`
> （單一事實來源；預設值 1.00）。alert-rules 設定頁不重複出現此門檻。
| calib_gap | AI 校準誤差 > 15pp（資料源：spec 04 ai-score） | warn |
| calibration_regression | 校正轉正後成效轉負自動回退時通知（spec 04） | info |

### 門檻設定
```
GET /api/alert-rules          → [{ id, enabled, value, unit, min, max }]
PUT /api/alert-rules          → 整組覆寫（前端 設定›預警規則 頁）
```
存於 config 表（單用戶單列 JSON 即可）。**前端目前存 localStorage（`pd_alert_rules`），接線後改走此 API 並刪除 localStorage 邏輯。**

計算時機：dashboard payload 組裝時順帶計算（同一快照），不另開排程。

### 頁面間同步契約（體驗債已修，後端需知）
前端已實作 localStorage `pd_alerts_cache` + `storage` 事件：規則變更或重算後，
其他已開啟頁面的鈴鐺即時更新。後端接線後：`PUT /api/alert-rules` 的 response 直接回傳
重算後的 alerts 陣列，前端寫入同一 cache key 即可沿用現有同步機制，不需 websocket。

## 3.2 買賣試算（what-if）

```
POST /api/whatif
{ "symbol": "2330", "side": "buy|sell", "shares": "200", "price": "598.00",
  "account_id": "tw_broker" }   // SR 2026-06-13 補：費稅規則繫於帳戶，不可省略歸屬
```
`account_id` 規則（SR Q1 定案）：省略時後端預設為**該 symbol 持股最多的帳戶**，並在回應以
`"account_id": "..."` 回聲實際採用帳戶；再平衡賣出分配同規則（不跨帳戶拆單）。

```jsonc
{
  "amount": "...", "fee": "...", "tax": "...",
  "fee_rule_desc": "0.1425%・最低 NT$20・證交稅 0.3%",
  // side=sell:
  "proceeds_net": "...", "adjusted_cost_removed": "...", "realized": "...",
  "remaining_shares": "...", "new_weight": "0.31",
  "oversell": false,
  // side=buy:
  "total_cost": "...", "new_shares": "...", "new_original_avg": "...", "new_adjusted_avg": "..."
}
```

要點：直接重用 data_ingestion 的費稅引擎（與真實寫入同一套規則），但走試算路徑（compute, no write）。
`oversell=true` 時仍回傳完整數字（前端顯示軟性警告，與寫入時的攔截行為一致）。

## 3.3 再平衡試算

```
POST /api/rebalance/preview
{ "targets": { "2330": "0.30", "0056": "0.241", ... } }   // ratio of reporting-ccy MV
```

```jsonc
{
  "rows": [
    { "symbol": "2330", "current_weight": "0.378", "target_weight": "0.30",
      "side": "sell", "shares": "210",            // 整數股；MY 市場 100 股一手
      "amount": "125580.00", "ccy": "TWD",
      "fee": "...", "tax": "...", "new_weight": "0.302" }
  ],
  "summary": { "turnover_reporting": "...", "total_fees_reporting": "...",
               "cash_after": "...", "excluded": ["00919"] }   // 缺價標的不參與
}
```

換算匯率：與 dashboard 同一組 spot（freshness 標記一致）。

## 前端接線
- `alerts.js`：刪除前端規則計算，改讀 dashboard payload 內嵌的 `alerts`；快取機制（localStorage `pd_alerts_cache` 供非 dashboard 頁顯示鈴鐺）保留。
- `detail.js`：simSection 的 `feeTax()` 改 POST /api/whatif（300ms debounce）；保留前端鏡像作為網路失敗 fallback 並標注「估算值」。
- `rebalance.js`：compute() 改 POST /api/rebalance/preview（500ms debounce）。
