# 14 — 資料來源管理（P1）

> 前端現況：`settings-datasources.js window.DATASOURCES_DATA` 全 mock。
> 後端已有 provider 實作（twse/tpex/yfinance/finmind…）與 `pricing.registry.Registry`
> fallback 鏈，但：**無金鑰儲存表、無健康/延遲記錄表、fallback 鏈 hardcode 於
> `pricing/defaults.py`、FinMind token 走建構子參數**。本 spec 補齊持久層＋路由。

## 14.0 新增持久層（config_store category = "data_sources"）

```sql
CREATE TABLE IF NOT EXISTS data_sources (
  id TEXT PRIMARY KEY,            -- "twse"|"tpex"|"yfinance"|"alphavantage"|"klse"|"finmind"|...
  api_key TEXT,                   -- 明文僅存 DB（單機自用）；API 回應永遠只給遮罩
  enabled INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS data_source_health (
  source_id TEXT PRIMARY KEY,
  status TEXT NOT NULL,           -- "ok"|"error"|"off"|"unknown"
  last_test TEXT, latency_ms INTEGER, detail TEXT);
CREATE TABLE IF NOT EXISTS data_source_fallbacks (
  account_id TEXT PRIMARY KEY,
  chain TEXT NOT NULL);           -- JSON array of source ids, 順序即優先序
```
seed：依 `pricing/defaults.py` 現行鏈寫入預設值。來源的靜態描述
（name/type/markets/auth/note）放 Python 常數表（不落 DB）。

## 14.1 GET /api/datasources

### Endpoint & Method
`GET /api/datasources`

### Description
來源總表（分組：報價/股利/匯率/新聞）＋各帳戶 fallback 鏈 — 取代 `DATASOURCES_DATA`。

### Request Structure
無參數。

### Response Structure
**200**（shape = mock）：
```jsonc
{
  "sources": [
    { "id": "finmind", "name": "FinMind", "type": "dividend", "markets": ["TW"],
      "auth": "apikey", "token_masked": "fm-•••9b1",
      "status": "ok", "last_test": "2026-06-11T03:00:05+08:00",
      "latency_ms": 650, "note": "台股股利、除息行事曆・付費 API" } ],
  "account_fallbacks": { "tw_broker": ["twse", "tpex", "yfinance"] },
  "account_names": { "tw_broker": "台灣券商" }
}
```

### Python Backend Implementation Notes
- `token_masked`：`prefix(3) + "•••" + suffix(3)`，key 為 null → `null`，
  status 同步給 `"off"`。
- `status` 來源：`data_source_health` 最新列；排程 job 失敗時（spec 15 `run_job`）
  順手 upsert 對應來源的 health（detail = exception 摘要）。

## 14.2 PUT /api/datasources/{id}/key

### Endpoint & Method
`PUT /api/datasources/{id}/key`

### Description
設定/重設 API 金鑰（FinMind、Alpha Vantage、NewsAPI…）。

### Request Structure
- Body：`{ "api_key": "fm-xxxxxxxxx9b1" }`（空字串 = 清除金鑰）

### Response Structure
**200**：`{ "id": "finmind", "token_masked": "fm-•••9b1", "status": "unknown" }`
**404**：未知來源 id。**400**：對 `auth:"none"` 的來源設金鑰。

### Python Backend Implementation Notes
- 寫入後將 health 重設為 `unknown`（提示使用者按「測試」）。
- `FinMindProvider` 等改為**從 `data_sources` 表讀 token**（建構時注入 conn 或
  讀取函式），移除環境變數/建構子 hardcode 路徑。

## 14.3 POST /api/datasources/{id}/test

### Endpoint & Method
`POST /api/datasources/{id}/test`

### Description
連線測試：以一檔已註冊標的（或固定樣本，如 2330/AAPL/1155.KL）打一次最小請求，
記錄延遲與結果。

### Request Structure
Body：無。

### Response Structure
**200**：`{ "id": "klse", "status": "error", "latency_ms": null, "detail": "HTTP 502 from provider", "last_test": "2026-06-12T10:00:00+08:00" }`
（測試失敗也是 200 — 失敗是合法測試結果；404 僅在來源 id 不存在。）

### Python Backend Implementation Notes
- 每個 provider 指定一個 probe 呼叫（`fetch_quote_latest` 一檔／`fetch_fx` 一對／
  `fetch_dividends` 一檔），`time.monotonic()` 計延遲，逾時 10s。
- 結果 upsert `data_source_health` 並原樣回傳。在 thread pool 執行避免卡 event loop。

## 14.4 PUT /api/datasources/fallbacks

### Endpoint & Method
`PUT /api/datasources/fallbacks`

### Description
各帳戶報價來源 fallback 鏈（拖曳排序後整組覆寫）。

### Request Structure
```jsonc
{ "account_fallbacks": { "tw_broker": ["twse", "tpex", "yfinance"],
                          "moomoo_my_my": ["yfinance", "klse"] } }
```

### Response Structure
**200** 回寫入後完整 `account_fallbacks`。
**400**：含未知 source id／空鏈／帳戶 id 不存在。

### Python Backend Implementation Notes
- `pricing.registry.Registry._chain` 改為可由此設定建構（`defaults.default_registry()`
  讀 `data_source_fallbacks` 表，無列時用現行 hardcode 預設）。
- 變更立即生效於下次 refresh（registry 每次 job 建構，不需熱重載）。
