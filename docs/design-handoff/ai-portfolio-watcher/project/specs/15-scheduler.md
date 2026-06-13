# 15 — 排程管理（P1）

> 前端現況：`settings-scheduler.js window.SCHED_DATA` 全 mock。後端已有
> `scheduler/jobs.py`（`schedule_config`／`job_runs` 表、`JOBS` registry、`run_job`、
> `trigger_job`）與 `runtime.py`（APScheduler），但：**無路由、無動態 reschedule**
> （cron 修改後需重啟才生效）。spec 04/07 的 insight 排程掛載也依賴本 spec 的表與機制。

## 15.0 Schema 擴充（SR 2026-06-13 整併 — spec 04/07 的依賴一次補齊）

```sql
-- schedule_config 新增（_add_column_if_missing）：
kind TEXT NOT NULL DEFAULT 'system'   -- 'system'（JOBS registry）| 'insight'（spec 04 §4.2 動態掛載）
payload TEXT NULL                     -- kind=insight 時存 insight_type_id
-- job_runs 新增：
payload TEXT NULL                     -- insight run 存 insight_task_id（spec 07 §7.4 過濾鍵）
reason TEXT NULL                      -- status=skipped 時的枚舉（spec 07 §7.4）
cost_usd TEXT NULL                    -- insight 類 job 的 LLM 費用（Decimal string）
```
- `GET /api/scheduler/jobs` **必須同時列出** kind=system 與 kind=insight 的 job
  （insight job 的 `desc` = 組合名稱，由 insight_types join）。
- `kind=insight` 的 job 不可從本頁刪除（只能停用）；刪除走 spec 04 §4.1 的組合封存連動。

## 15.1 GET /api/scheduler/jobs

### Endpoint & Method
`GET /api/scheduler/jobs`

### Description
排程工作總表：設定值（cron/tz/enabled）＋最近一次執行結果＋下次執行時間。

### Request Structure
無參數。

### Response Structure
**200**：
```jsonc
{ "jobs": [
    { "id": "quotes_tw", "desc": "台股收盤報價＋匯率",
      "cron": "0 14 * * mon-fri", "tz": "Asia/Taipei", "enabled": true,
      "last": { "status": "ok", "at": "2026-06-11T14:00:04+08:00",
                "detail": "3 ok, 0 failed", "duration_s": 3.8 },     // 無紀錄 → null
      "next": "2026-06-12T14:00:00+08:00" } ] }                      // 停用 → null
```
（`human` 友善描述由前端 cron builder 自行推導，後端不回。）

### Python Backend Implementation Notes
- `schedule_config` JOIN 各 job 最新一筆 `job_runs`（`duration_s` = finished−started）；
  `desc` 來自 `JOBS` registry 的 `description`。
- `next`：從 APScheduler `get_job(job_id).next_run_time`；未掛載（disabled）回 null。

## 15.2 PUT /api/scheduler/jobs/{id}

### Endpoint & Method
`PUT /api/scheduler/jobs/{id}`

### Description
修改單一 job 的 cron／時區／啟用開關（前端簡易模式組出 cron，進階模式直填）。

### Request Structure
- Body（子集更新）：
  ```jsonc
  { "cron": "30 17 * * mon-fri", "tz": "Asia/Kuala_Lumpur", "enabled": true }
  ```

### Response Structure
**200** 回更新後該 job 完整列（同 15.1 元素，含重算的 `next`）。
**400**：`{ "error": { "code": "invalid_cron", "message": "cron 表達式無效：…", "field": "cron" } }`
（tz 無效同此 code 換 field）。**404**：未知 job id。

### Python Backend Implementation Notes
- 驗證：`apscheduler.triggers.cron.CronTrigger.from_crontab(cron, timezone=tz)`
  — 建構失敗即 400，**不寫 DB**。
- 寫 `schedule_config` 後立即 reschedule：`runtime.py` 需新增
  `reschedule(job_id, cron, tz, enabled)`（`scheduler.reschedule_job` / `pause_job` /
  `resume_job`）。App 持有 scheduler 單例（lifespan state）。

## 15.3 POST /api/scheduler/jobs/{id}/run

### Endpoint & Method
`POST /api/scheduler/jobs/{id}/run`

### Description
「立即執行」按鈕：手動觸發一次，不影響排程節奏。

### Request Structure
Body：無。

### Response Structure
**202**：`{ "run_id": 514, "job_id": "quotes_my" }`
**404**：未知 job id。**409**：該 job 正在執行中
`{ "error": { "code": "already_running", "message": "quotes_my 執行中" } }`。

### Python Backend Implementation Notes
- 背景 thread 跑 `jobs.run_job(conn, job_id, now=…)`（自帶 job_runs 記錄與例外吞噬）；
  「執行中」判定 = 該 job 最新 run 列 `finished_at IS NULL`。
- 前端以 15.4 輪詢 run 狀態。

## 15.4 GET /api/scheduler/runs

### Endpoint & Method
`GET /api/scheduler/runs`

### Description
執行歷史（設定頁歷史表＋spec 08 refresh-quotes／15.3 的完成輪詢都用這支）。

### Request Structure
- Query：
  | 參數 | 型別 | 預設 | 說明 |
  |---|---|---|---|
  | `job_id` | string | — | 過濾單一 job |
  | `limit` / `offset` | int | 50 / 0 | started_at 倒序 |

### Response Structure
**200**：
```jsonc
{ "rows": [
    { "id": 514, "job_id": "quotes_my", "started_at": "2026-06-10T17:30:06+08:00",
      "finished_at": "2026-06-10T17:30:36+08:00", "status": "error",
      "detail": "HTTP 502 from provider — 1155.KL 未更新",
      "duration_s": 30.0, "cost_usd": null } ],     // cost_usd 僅 insight 類 job 有值
  "total_count": 208 }
```
**400**：limit > 500。

### Python Backend Implementation Notes
- 直查 `job_runs`；`cost_usd` 讀 15.0 新欄位（system job 一律 null）；
  `reason` 於 status=skipped 時一併回傳（spec 07 枚舉）。
- `status` 為 null（執行中）時前端顯示 spinner — 保留 null 不要轉字串。
