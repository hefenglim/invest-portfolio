# 16 — LLM 模型／角色／額度設定（P1）

> 前端現況：`settings-llm.js window.LLM_DATA` 全 mock。後端 `shared/llm_config.py`
> **表結構齊備**（llm_models／roles／budget ledger／usage，含三個降級例外），但無路由。
> spec 04 §4.3 的 master 角色擴充疊加在本 spec 之上。

## 16.1 GET /api/llm/config

### Endpoint & Method
`GET /api/llm/config`

### Description
設定 › LLM 整頁資料：模型清單、角色指派、額度、用量統計 — 取代 `LLM_DATA`。

### Request Structure
無參數。

### Response Structure
**200**（shape = mock；金額 Decimal string）：
```jsonc
{
  "models": [
    { "alias": "claude-sonnet", "provider": "anthropic", "model_name": "claude-sonnet-4-5",
      "api_base": null, "api_key_masked": "sk-•••••••a2f",
      "vision": true, "price_in": "3.00", "price_out": "15.00",   // USD / Mtok
      "context_window": 200000, "max_output_tokens": 8192,
      "timeout_seconds": 60, "max_retries": 2, "enabled": true, "notes": null,
      "health": "ok", "last_called": "2026-06-11T08:00:12+08:00" } ],
  "roles": { "default_model": "claude-sonnet", "default_fallback": "gpt-4o-mini",
             "vision_model": "claude-sonnet", "vision_fallback": null,
             "master_model": null, "master_fallback": null },     // master = spec 04
  "quota": { "remaining_usd": "3.84", "alert_threshold_usd": "1.00",
             "topups": [ { "at": "2026-06-01T09:00:00+08:00", "amount_usd": "10.00", "note": "六月加值" } ] },
  "usage": {
    "by_model": [ { "alias": "claude-sonnet", "calls": 42, "tokens_in": 512400,
                    "tokens_out": 96100, "cost_usd": "4.83" } ],
    "by_agent": [ { "agent": "ai_agents_input", "cost_usd": "1.92" } ],
    "daily": {                                       // SR 定案：逐模型成本序列（近 30 日）
      "dates": ["06-10", "06-11"],
      "series": [ { "alias": "claude-sonnet", "costs": ["0.29", "0.26"] },
                  { "alias": "gpt-4o-mini",   "costs": ["0.06", "0.05"] } ]
    }
  }
}
```

### Python Backend Implementation Notes
- 全部來自 `llm_config` 既有四表；`api_key_masked` 遮罩規則同 spec 14。
- `health`：最近一次呼叫成功/失敗（usage ledger 最新列 status），無紀錄 → `"unknown"`。
- AI 狀態 chip 邏輯（關閉/額度歸零/偏低）由前端依 roles＋quota 推導，後端不重複給。
- **SR 修正 — `usage.daily` 為逐模型序列**：前端 `settings-llm.js` 現以 mock 寫死兩條
  positional 線（d[1]=claude-sonnet、d[2]=gpt-4o-mini），接線時改為依 `series[]` 動態建線
  （模型數可變）。`quota.alert_threshold_usd` 同時是 spec 03 `quota_low` 規則的門檻來源。

## 16.2 POST /api/llm/models ・ PUT /api/llm/models/{alias} ・ DELETE /api/llm/models/{alias}

### Endpoint & Method
```
POST   /api/llm/models
PUT    /api/llm/models/{alias}
DELETE /api/llm/models/{alias}
```

### Description
模型 registry CRUD。`api_key` 為 write-only：請求可帶、回應永遠只給遮罩。

### Request Structure
- `POST`/`PUT` Body（PUT 為子集更新；alias 不可改）：
  ```jsonc
  { "alias": "qwen-vl", "provider": "openrouter", "model_name": "qwen/qwen2.5-vl-72b",
    "api_base": "https://openrouter.ai/api/v1", "api_key": "sk-…",
    "vision": true, "price_in": "0.40", "price_out": "0.40",
    "context_window": 32000, "max_output_tokens": 2048,
    "timeout_seconds": 90, "max_retries": 1, "enabled": false, "notes": "測試中" }
  ```

### Response Structure
**201/200** 回該模型完整列（16.1 shape）。
**409**：alias 重複。**404**：PUT/DELETE 不存在。
**422**（DELETE）：`{ "error": { "code": "model_in_use", "message": "qwen-vl 仍被 vision_model 角色指派" } }`

### Python Backend Implementation Notes
- 對應 `llm_models` 表（`llm_config.py` 內補 CRUD 函式，路由不得直寫 SQL）。
- DELETE 前檢查角色表引用；被引用一律 422，前端先解除指派。

## 16.3 PUT /api/llm/roles

### Endpoint & Method
`PUT /api/llm/roles`

### Description
角色指派整組覆寫（default／vision／master ＋各自 fallback）。任一角色設 null = 關閉該能力；
全部 null = AI 全關（`AINotActivated` 狀態）。

### Request Structure
```jsonc
{ "default_model": "claude-sonnet", "default_fallback": "gpt-4o-mini",
  "vision_model": "claude-sonnet", "vision_fallback": null,
  "master_model": null, "master_fallback": null }
```

### Response Structure
**200** 回寫入後 roles。**400**：指向不存在或 `enabled:false` 的 alias；
vision 角色指向 `vision:false` 的模型。

### Python Backend Implementation Notes
- `LLMRole` enum 需擴充 `MASTER`/`MASTER_FALLBACK`（spec 04 §4.3）。
- fallback 與主模型相同 alias → 400（無意義設定）。

## 16.4 POST /api/llm/models/{alias}/test ・ POST /api/llm/quota/topup ・ PUT /api/llm/quota

### Endpoint & Method
```
POST /api/llm/models/{alias}/test    → 連線測試（最小 ping）
POST /api/llm/quota/topup            → 加值
PUT  /api/llm/quota                  → 警示閾值
```

### Request Structure
- `test` Body：無。
- `topup` Body：`{ "amount_usd": "10.00", "note": "六月加值" }`（amount > 0）
- `quota` Body：`{ "alert_threshold_usd": "1.00" }`

### Response Structure
- `test` **200**：`{ "ok": true, "latency_ms": 820, "reply_snippet": "pong" }`；
  失敗也是 200 ＋ `{ "ok": false, "error_detail": "401 invalid api key" }`。
- `topup` **200**：`{ "remaining_usd": "13.84" }`；**400** amount ≤ 0。
- `quota` **200** 回新閾值。

### Python Backend Implementation Notes
- test：經 LiteLLM 以該模型發 max_tokens=8 的固定 prompt；**測試費用照記用量帳**
  （誠實計帳），逾時 = timeout_seconds。
- topup 寫 budget ledger（append-only，與 mock 的 `topups` 列表同源）；
  `remaining_usd` = topups 總和 − usage 總成本，由 ledger 推導、不存快照。
