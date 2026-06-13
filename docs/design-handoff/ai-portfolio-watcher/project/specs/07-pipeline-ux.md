# Spec 07 — 洞察管線中心 UX 重構：任務狀態 API、乾跑預檢、診斷器（P1）

> 對應前端：`AI Pipeline Hub.html`（pipeline-data.js / pipeline.js / pipeline-preflight.js / pipeline-wizard.js）。
> 本 spec **不改變任何既有功能與邏輯**（spec 03/04/06 全部沿用）；只新增三件事：
> 1. 把散落各頁的狀態推導收斂成一支「任務狀態」API（單一事實來源）。
> 2. 乾跑預檢（preflight）— 與正式執行共用同一套守門程式碼（spec 04 §4.9 R1–R8）。
> 3. 診斷器（diagnose）— 回答「為什麼沒跑」。

## 7.0 命名遷移（僅 UI 文案與 API 路由別名，資料表不改名）

| 原名 | 新名 | 後端動作 |
|---|---|---|
| 洞察類型 insight_types | 洞察任務 insight task | 路由新增別名 `/api/insight-tasks/*` → 內部同一資源；舊路由保留 |
| 策略提示詞 strategy_prompts | 分析模板 | 無（純文案） |
| 系統提示詞 | 全域守則 | 無 |
| 校正提示詞庫 | 校正版本鏈 | 無 |
| 排程工作表＋執行歷史 | 運行中心 | 無 |

## 7.1 任務狀態 API（健康列＋管線卡的單一事實來源）

```
GET /api/insight-tasks/status
```
```jsonc
{
  "as_of": "...",
  "health": {
    "master_ok": true,                  // settings-llm master_model 已設定且可用
    "quota_remaining": "0.83",          // shared/llm 額度帳
    "last_batch": { "at": "...", "cards": 8, "cost_usd": "0.094" }
  },
  "tasks": [{
    "id": "it-health", "name": "持倉健診", "scope": "per_symbol",
    "enabled": true, "level": "warn",   // ok|info|warn|fail|idle（聚合，規則見 7.1.1）
    "nodes": {                          // 五節點各自的 (lv, text, sub)
      "trigger":  { "lv": "ok",   "text": "每日 08:00", "sub": "下次 06-13 08:00" },
      "input":    { "lv": "warn", "text": "全部持倉 7 檔", "sub": "1155.KL 缺價" },
      "assemble": { "lv": "warn", "text": "1/2 模板啟用", "sub": "停用段跳過" },
      "exec":     { "lv": "warn", "text": "sonnet via LiteLLM", "sub": "額度餘 $0.83" },
      "output":   { "lv": "warn", "text": "7 卡（6 LLM＋1 資料異常）", "sub": "今日 08:00" }
    },
    "last_run": { "at": "...", "status": "partial", "summary": "...", "notes": ["..."] }
  }]
}
```

### 7.1.1 節點狀態推導規則（後端實作；前端 `PIPE.nodeStates()` 為鏡像，接線後刪除）
| 節點 | fail | warn | info | off/idle |
|---|---|---|---|---|
| 觸發 | — | kind=manual（未排程） | — | task disabled |
| 輸入 | universe 空（R2） | freshness 缺價/過期影響本任務標的（R4 來源） | R2 自動移除事件 7 日內 | — |
| 組裝 | 模板全停用/封存（R3） | 部分模板停用；R1 mismatch 既有資料 | 校正 v 存在未套用 | — |
| 執行 | 額度 = 0（R6） | 額度 < quota_low 門檻；master 未設且 self_correct=true | — | — |
| 產出 | last_run ∈ {skipped, error} | partial | — | 從未執行 |

聚合 `level` = 五節點最高嚴重度（fail > warn > info > ok）；task disabled 一律 `idle`。

## 7.2 乾跑預檢

```
POST /api/insight-tasks/{id}/preflight        // 也支援 body 直接帶草稿（精靈第 4 步，未建立先檢）
```
```jsonc
{
  "gates": [   // 順序固定；與 runtime 守門（spec 04 §4.9）共用同一程式碼路徑
    { "id": "G0", "name": "任務啟用",   "lv": "ok|info|warn|fail", "msg": "...",
      "fix": { "kind": "enable_task" } },              // 可一鍵修復時回傳 fix 描述
    { "id": "G1", "name": "觸發來源",   ... },          // manual=fail(不會自動跑)
    { "id": "R1", "name": "範圍相容",   ... },
    { "id": "R2", "name": "標的宇宙",   ... },
    { "id": "R3", "name": "模板啟用",   ... },
    { "id": "R4", "name": "價格資料",   ... },
    { "id": "R5", "name": "變數可用性", ... },
    { "id": "R6", "name": "LLM 額度",   "msg": "餘 $0.83・本次估 $0.07" },
    { "id": "G7", "name": "校正管線",   ... }           // master 未設→warn；v 未套用→info
  ],
  "verdict": "blocked|degraded|clean",
  "assembled_preview": {               // 重用 POST /api/prompts/preview（spec 06）逐層組裝
    "layers": [{ "kind": "system|template|calibration", "name": "...", "rendered": "..." }],
    "est_tokens": 1842, "est_cost_usd": "0.07"
  }
}
```
硬規則：**preflight 與正式執行必須呼叫同一個守門函式**（避免「預檢過、實跑掛」的雙重事實）；
preflight 絕不呼叫 LLM、不寫 job_runs、零成本。

`fix.kind` 枚舉：`enable_task` / `create_schedule` / `enable_template:{id}` / `edit_universe` / `edit_templates` / `set_active_calibration`。
前端把 fix 渲染成一鍵按鈕，動作走既有 PUT/POST endpoints。

## 7.3 診斷器（為什麼沒跑）

```
GET /api/insight-tasks/{id}/diagnose
```
回傳與 preflight 相同的 gates 結構＋`first_blocker`（第一道 fail 閘門 id）＋
`recent_skips`: 近 5 筆 job_runs 中 status=skipped 的 (at, reason)。
實作 = preflight 唯讀版＋job_runs 查詢，無新狀態。

## 7.4 任務視角運行記錄

```
GET /api/insight-tasks/{id}/runs?limit=20
```
job_runs 以 payload=insight_task_id 過濾；**skipped 必須帶 reason 枚舉**
（`R1_scope_mismatch | R2_universe_empty | R3_no_live_templates | R6_quota | master_missing | disabled`），
與 spec 04 §4.9「任何擋下都寫 job_runs」一致 — 此處只是查詢面。

## 7.5 前端接線點

| 前端 | mock | 替換 |
|---|---|---|
| `pipeline-data.js` PIPE.health/tasks/nodeStates | 全 mock | GET /api/insight-tasks/status |
| `pipeline-preflight.js` evalGates() | 前端鏡像 | POST …/preflight、GET …/diagnose |
| `pipeline.js` 運行記錄表 | PIPE.runs | GET …/{id}/runs |
| `pipeline-wizard.js` 建立 | push 進 PIPE.tasks | POST /api/insight-tasks（既有 spec 04 §4.7）＋草稿 preflight |
| 校正版本區段 | PIPE.chains | 既有 calibrations API（spec 04） |

## 7.6 驗收情境（前端已內建 3 個故障 demo，後端需可重現）
1. 任務停用＋未排程 → diagnose first_blocker=G0，fix=enable_task + create_schedule。（demo：股利展望）
2. 唯一模板停用 → 該次 job_runs 寫 skipped/R3_no_live_templates；preflight R3=fail 附 enable_template fix。（demo：動能週報）
3. 自選標的出清 → universe 自動移除＋info 預警；清單空 → enabled=false＋R2 fail。（demo：高息標的體檢）
