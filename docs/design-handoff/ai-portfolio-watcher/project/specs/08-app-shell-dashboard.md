# 08 — API 基礎層與儀表板 payload（P0 — 所有其他 spec 的前置）

> **背景**：後端 `portfolio_dash` 目前是純 Python 計算庫，**不存在任何 HTTP 層**。
> 本 spec 建立 FastAPI 應用骨架與第一批核心 endpoints。specs 01–07 與 09–16 的所有
> 路由都掛在這個應用上 — 請最先實作本 spec。

## 8.0 應用骨架（非 endpoint，但為硬性要求）

- 新增 `portfolio_dash/api/` 套件：`app.py`（`create_app()` factory）＋ 每個領域一個 router 檔
  （`dashboard.py`、`auth.py`、`instruments.py`、`ledgers.py`、`ingest.py`、`settings_*.py`…）。
- lifespan 啟動時：`shared.db.session()` 取得連線 → `bootstrap.bootstrap_db(conn)` →
  `scheduler.jobs.ensure_scheduler_seeded(conn)` → `scheduler.runtime.start()`；關閉時 `shutdown()`。
- SQLite 連線：**per-request**（FastAPI dependency 包 `session()`），勿跨執行緒共用單一連線。
- 前端為純靜態 HTML/JS：以 `StaticFiles` 掛載 repo 前端目錄於 `/`，API 一律掛 `/api/*`。
- **金額序列化**：所有 `Decimal` 欄位序列化為**字串**（pydantic `model_dump(mode="json")` 即可；
  自訂 dict 組裝處用 `str(value)`）。前端 `fmt` 已同時容忍 number/string。
- **共同錯誤格式**（所有 spec 共用）：
  ```jsonc
  { "error": { "code": "validation_error", "message": "shares 必須大於 0", "field": "shares" } }
  ```
  - 400 = 請求格式/參數錯誤、422 = 業務規則拒絕、401/403 = auth（spec 09）、
    404 = 資源不存在、503 = 外部依賴（LLM/行情來源）不可用、500 = 未預期例外（log + 通用訊息）。
- **LLM 三例外 → HTTP 碼統一映射**（SR 2026-06-13 定案，所有 LLM 類 endpoint 一體適用）：
  | 例外（shared/llm_config） | HTTP | code |
  |---|---|---|
  | `LLMBudgetExceeded` | **402** | `budget_exceeded` |
  | `AINotActivated` | **409** | `ai_not_activated` |
  | `LLMUnavailable` | **503** | `llm_unavailable` |
- **Enum 線上格式（wire format）一律小寫**：後端 `Side.BUY` ↔ API `"buy"`、
  `DividendType.CASH` ↔ `"cash"`…序列化/反序列化在 API 層統一轉換（pydantic validator），
  核心層 enum 不改。**注意：`DividendType` 需擴充 `NET`**（馬股淨額入帳，前端 ledger 已用）。
- **時間格式**：DB 存 UTC isoformat（現狀）；API 回應一律轉換為 **ISO-8601 含時區偏移，
  应用時區固定 `Asia/Taipei`**（與 reporting_currency=TWD 同級的系統常數）。
- **分頁慣例**：清單類 GET 一律 `limit`/`offset` query ＋回應 `total_count`；limit 上限 500。

---

## 8.1 GET /api/dashboard

### Endpoint & Method
`GET /api/dashboard`

### Description
單一請求回傳儀表板整頁所需資料。`mock-data.js` 的 `window.DASHBOARD_DATA`
**就是欄位契約**（欄位名一字不差），對應後端 `portfolio.dashboard_models.DashboardData`。

### Request Structure
- Header：`Cookie: pd_session=…`（spec 09；訪客模式免帶）
- Query：
  | 參數 | 型別 | 預設 | 說明 |
  |---|---|---|---|
  | `trend_days` | int | 90 | 趨勢線回看天數 |

### Response Structure
**200**（節錄 — 完整 shape 以 `mock-data.js` 為準）：
```jsonc
{
  "as_of": "2026-06-11T14:30:00+08:00",
  "reporting_currency": "TWD",
  "kpis": { "total_market_value": "1618682.54", "total_return": "308529.66",
            "total_return_rate": "0.2147", "realized_total": "34931.12",
            "unrealized_total": "273598.54", "xirr": "0.1832",
            "fx_realized": "1250.00", "fx_unrealized": "14154.12",
            "reporting_currency": "TWD" },
  "holdings": [ { "account_id": "tw_broker", "symbol": "2330", "...": "…",
                  "spark_30d": ["612.5", "..."] } ],   // spark_30d 為 spec 01 要求的附帶欄位
  "realized": { "rows": [], "by_currency": {} },
  "returns":  { "by_currency": {}, "reporting_total_return": "…", "xirr": null },
  "allocation": {}, "currency_view": {}, "fx": {}, "dividends": {},
  "trend": {}, "freshness": {}, "insights": [],
  "alerts": [],                  // spec 03：組 payload 時順帶計算
  "dividend_projection": {},     // spec 05
  "llm_quota": { "remaining_usd": "3.84" }  // 取代 alerts.js PD_QUOTA hardcode
}
```
**500**：
```jsonc
{ "error": { "code": "internal_error", "message": "dashboard 組裝失敗，詳見 server log" } }
```

### Python Backend Implementation Notes
- 核心：`portfolio.dashboard.build_dashboard(conn, now=…)` 已回傳 `DashboardData`，
  router 只做「呼叫＋附加欄位（alerts／dividend_projection／llm_quota／spark_30d）＋序列化」。
- `spark_30d`：用 `pricing.store.get_price_history(conn, symbol, …)` 取最近 22 個交易日 close，
  一次 SQL 批撈全部持倉代號，避免 N+1。
- `llm_quota`：讀 `shared/llm_config` 預算帳（remaining）。
- 計算皆為 pure-read；**本 endpoint 絕不寫入**。

---

## 8.2 POST /api/actions/refresh-quotes

### Endpoint & Method
`POST /api/actions/refresh-quotes`

### Description
頂欄「⟳ 重新整理 → 更新報價」（`shell.js` refresh menu）：立即抓最新報價＋匯率，寫入價格庫。

### Request Structure
- Body：
  ```jsonc
  { "markets": ["TW", "US", "MY"] }   // 省略或 null = 全部市場
  ```

### Response Structure
**202**：
```jsonc
{ "run_ids": [101, 102, 103], "jobs": ["quotes_tw", "quotes_us", "quotes_my"] }
```
**400**：`{ "error": { "code": "validation_error", "message": "未知市場代碼 XX" } }`

### Python Backend Implementation Notes
- 對每個市場呼叫 `scheduler.jobs.run_job(conn, "quotes_{tw|us|my}", now=…)`，
  以背景 thread（`fastapi.BackgroundTasks`）執行，立即回 202＋`job_runs` 的 run id。
- 前端完成提示：輪詢 `GET /api/scheduler/runs?limit=…`（spec 15）看 run 狀態即可，不需 websocket。

---

## 8.3 POST /api/actions/recompute

### Endpoint & Method
`POST /api/actions/recompute`

### Description
頂欄「重算（重建統計）」：由期初／交易／股利／換匯四帳本完整重建統計。
**帳本 append-only，本動作絕不修改帳本**。

### Request Structure
- Body：無（空 JSON `{}`）

### Response Structure
**200**：
```jsonc
{ "as_of": "2026-06-12T10:00:00+08:00", "rebuilt": true }
```
**422**（帳本不一致，例如超賣導致成本簿無法建立）：
```jsonc
{ "error": { "code": "oversell", "message": "2330@tw_broker 於 2026-05-15 賣出超過持有" } }
```

### Python Backend Implementation Notes
- 現行架構統計皆「讀時計算」（`build_dashboard` 每次重建），故 v1 行為＝
  以 `portfolio.cost_basis.build_book` 完整跑一遍驗證帳本一致性、捕捉 `OversellError` 映射 422，
  並清除任何將來引入的快取層。回傳新 `as_of`，前端隨後重新 `GET /api/dashboard`。
