# 19 — 前端接線層(api.js)與運行保全(P0 — 接線開工前必讀)

## 19.1 統一 fetch 包裝 `api.js`(新前端檔,所有頁面共用)

> 17 份 spec 的「前端接線」全部經由本層,**禁止任何頁面直接呼叫 `fetch`**。

```js
window.pdApi = {
  get(path, params)        // → Promise<json>
  post(path, body)         // → Promise<json>
  put(path, body), del(path)
  download(path, body)     // 匯出類:觸發瀏覽器下載(spec 02)
};
```

行為契約:
1. **錯誤處理**:非 2xx → 解析 spec 08 §8.0 錯誤格式,throw `PdApiError{status, code, message, field, issues}`;
   呼叫端 catch 後以 `window.toast(message, 'fail', code)` 顯示 — 文案直接用後端 message,前端不再造句。
2. **401 全域攔截**:保護模式下任何 401 → `window.location.replace('login.html')`(一處實作,各頁免重複)。
3. **402/409/503(LLM 降級)**:不導頁,回拋給呼叫端 — AI 功能區塊自行顯示降級狀態
   (額度歸零 chip、未啟用提示),與 settings-llm.js 既有狀態邏輯一致。
4. **金額欄位**:回應的 Decimal 字串**原樣傳遞給 `fmt`**(fmt 已相容 string/number),
   前端絕不 `parseFloat` 後再運算 — 運算一律在後端。
5. **debounce 約定**:試算類輸入(whatif 300ms、rebalance 500ms,spec 03)在呼叫端做;
   `api.js` 提供 `pdApi.abortable(key)` 讓新請求自動取消同 key 舊請求(AbortController),
   防止過期回應覆蓋新結果。
6. **mock 退場規則**:每支 endpoint 接線後,對應 mock(`window.*_DATA`/mock 檔)**整段刪除**,
   不留 fallback 分支(唯二例外:detail.js feeTax 離線鏡像、alerts 快取 — spec 03 明文保留)。

## 19.2 啟動與檔案佈局

```
portfolio-dash/
  portfolio_dash/          # 既有後端套件(含新 api/)
  web/                     # 前端靜態檔(本專案全部 html/js/css 移入)
  data/portfolio.db        # SQLite(唯一狀態;路徑由 PD_DB_PATH 環境變數覆寫)
  tests/  specs/  Makefile
```
- 啟動:`make run` = `uvicorn portfolio_dash.api.app:create_app --factory --port 8400`;
  StaticFiles 掛 `web/` 於 `/`,API 於 `/api/*`(spec 08 §8.0)。
- 相依鎖定:`pyproject.toml` 完整宣告(fastapi/uvicorn/apscheduler/pydantic/litellm/freezegun/
  pytest 系列…),版本上鎖(`~=`)。

## 19.3 資料保全(投資帳本 = 不可遺失資料)

1. **每日自動備份 job**(`backup_daily`,進 spec 15 registry,預設每日 01:30):
   `sqlite3 .backup` API(非檔案複製 — 避免寫入中拷貝損毀)→
   `data/backups/portfolio_{YYYY-MM-DD}.db.gz`,保留 30 份,輪轉刪除。
2. **寫入前自動快照**:CSV/AI commit(spec 12)與任何 schema migration 前,
   先做一次同機制備份(檔名加 `pre_import_`/`pre_migrate_` 前綴)— 匯錯整批可一鍵回退。
3. **還原**:`make restore FILE=...`(停 scheduler → 換檔 → 重啟);
   設定頁顯示最近備份時間(掛在 freshness 區,`GET /api/dashboard` freshness 加
   `last_backup_at` 欄位)。備份連續 3 日失敗 → warn 預警(走 spec 03 規則引擎)。
4. **完整性自檢**:備份 job 內含 `PRAGMA integrity_check`,fail 即 error run + 預警。

## 19.4 日誌

- 結構化 logging(stdlib,JSON lines)→ `data/logs/app.log`(rotate 10MB×5);
  API 5xx 一律含 traceback;LLM 呼叫記 alias/tokens/cost(與 usage 帳對得上)。
