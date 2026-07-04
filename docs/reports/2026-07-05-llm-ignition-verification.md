# LLM 點火全面驗證報告(P0 執行記錄)

> 驗證日:2026-07-05 · 分支 `fix/llm-structured-output-live`(部署於測試站)
> 範圍:LLM 五個表面全部真打(批次洞察 Loop 1、快取、Loop 2–4 空轉、AI 對帳單文字解析、
> 自由文字 prompt test)+ 降級路徑 + 記帳核對 + 瀏覽器雙版本
> 方法:兩環境迴圈 —— 修復在 DEV 過重閘門(pytest 全套/mypy --strict/ruff)→ 部署分支
> 到測試站 → 站外行為驗證;共三輪部署迭代。金鑰全程由使用者持有,未入 git/對話。

---

## 結論

**LLM 支柱從「已建成未點火」推進到「點火且真站全綠」。** 首輪真打即暴露 7 個
hermetic 測試(mock LLM)原理上看不見的缺陷 —— 其中 2 個屬「沒修就等於功能不存在」級
(結構化輸出在真供應商上 100% 失敗、同日快取永遠失效)。全部修復、補上回歸測試
(單元+契約)、逐輪重新部署驗證。最終狀態:**24 項站外檢查全 PASS**,測試站留下乾淨的
10 張真卡(1 週報+9 健檢)供使用者實際操作。

## 修復清單(七項,全部含回歸測試)

| # | 缺陷(真站症狀) | 根因 | 修復 |
| --- | --- | --- | --- |
| F1 | 每次批次:4 個 LLM 呼叫、$0.036、**零卡片**(主備援全滅) | LiteLLM 能力表對所有 `openrouter/*` id 回 False → `response_format` 從未送出;組裝提示詞又無任何 JSON 指令 → 模型回中文長文,解析必敗 | `shared/llm.py`:結構化呼叫**一律**在提示尾附 schema 化 JSON-only 契約(`<output_format>`);解析容忍圍欄/前後綴文字(剝欄+切片)再判失敗 |
| F2 | 上述失敗被報成「**額度耗盡**」($4.96 還在) | `except LLMError` 一律寫死 `budget_exhausted_mid_run` | reason 帶 `exc.kind`(`llm_unavailable_mid_run` / `ai_not_activated_mid_run` / budget);例外訊息進 `detail` |
| F3 | 執行中 run 的 status 是空白;`started_at` +08:00 但 `finished_at` UTC | insert 沒寫 status;finalize 用 UTC | `start_job_run`/`start_insight_run` 寫 `'running'`;finalize 用呼叫者時區(insight 與 generic 兩路徑) |
| F4 | preflight 傳 `{}` → 健康任務被誤報 R3「策略段全空」 | 空物件被解析成全預設草稿,蓋掉已存組合 | 空草稿(`model_fields_set` 為空)視同「查已存任務」 |
| F5 | **同日重跑再扣一次錢+卡片重複**(快取從未命中) | 指紋雜湊「渲染後提示詞」,而 `{{as_of}}/{{now}}` 帶秒級時鐘 → 指紋永不相同(單元測試用固定 NOW 看不見) | 指紋改雜湊**日錨定第二渲染**(now/as_of 壓到當日):同日同資料命中、盤中資料變動仍正確失效、LLM 看到的提示詞保留真時間。R4 異常卡快照同步降為日粒度(同日重跑不再重複產異常卡) |
| F6 | generic 排程 job 同樣的時區病 | `finish_job_run` UTC 收尾 | 傳入呼叫者 `now` 的時區 |
| F7 | AI 文字解析:模型猜出不存在的 `charles_schwab` → 全部「unknown account」 | 提示詞說「用系統認得的帳戶 id」**卻沒列出清單**,一次性範例只有 tw_broker | 提示詞注入真實帳戶目錄(`id=名稱 (幣別)`)+ 禁止自創 id |

觀察但**不改**(記錄供決策):G1「未排程→verdict blocked」為 spec §7.2 明文+測試釘住
(手動流程下顯示偏嚴,可考慮軟化為 warn);刪除洞察任務不級聯刪卡(append-only 歷史,
孤兒卡會留在洞察頁)。

## 站外驗證證據(最終輪,測試站)

```
PASS  IT1 週報 run ok / 產 1 張全組合卡(繁中,body 2,390 字)/ 純敘事無預測
PASS  IT1 同日重跑:$0、零重複卡(指紋快取命中)
PASS  IT2 健檢 run ok / 9 檔各 1 張 / 9/9 帶預測+信心值
PASS  IT2 批次重跑:$0、零新卡(9 目標全命中快取)
PASS  額度遞減 == 用量總和(分毫核對:-0.1338320 vs +0.1338320)
PASS  R4 未知代號 → 1 張「資料異常」定額卡(model=(none)、$0)、同日重跑不重複
PASS  解綁 default 角色 → partial + ai_not_activated_mid_run(訊息明確)→ 還原成功
PASS  runs 列表 status 不再空白('running'/'ok')、時間戳同為 +08:00
PASS  Loop 2 evaluate_insights 手動觸發 → ok「evaluate pass complete」(day-0 無到期
      預測,不硬判);/api/ai-score 健康(n=0)
PASS  Loop 3 generate_calibrations → ok(min_samples 閘門下安全空轉)
PASS  alert_scan → ok(掃出 4 則警示,無 on_alert 任務故 0 派發)
PASS  prompts/test 自由文字 → 真回覆(繁中、引用真 KPI 數字)
PASS  AI 文字解析三案:嘉信→schwab(ok)/ Moomoo美股→moomoo_my_us(正確賣超軟警告)/
      台股券商→tw_broker(ok);單次成本 ~$0.0011
```

## 測試站現況(交付使用者實際操作)

- 分支 `fix/llm-structured-output-live` @ `3ba2bdc`;prod 不受影響(仍 tag v0.1.9)。
- 三模型註冊+六角色綁定(使用者 7/4 親設;sonnet-5 timeout 已調 120s,金鑰未動)。
- 系統提示詞 = 方向性判讀版(§3 修訂稿);兩個洞察任務:持倉週報(portfolio,純敘事)、
  個股健檢(per_symbol,self_correct on,horizon 14 天)。
- 洞察頁 = 乾淨 10 張真卡(驗證期間的重複/孤兒卡已清,合成資料庫內部整理)。
- 額度:$5 儲值,驗證全程共花 **~$0.35**(其中一半是修復前的失敗呼叫)。
- 尚未掛排程(手動觸發模式)—— 依 runbook,排程在使用者驗收後、promote 前再掛。

## 交付後的下一步(使用者)

1. 測試站實際操作:洞察頁看卡、pipeline hub 手動觸發、AI 輸入試幾句真實說法。
2. 綠燈後回報 → merge main + `/ship-version`(CHANGELOG 收整 F1–F7)→ tag 部署 prod
   → prod 填金鑰/儲值/建任務(同 runbook §6)→ 掛週排程。
3. 隔日(或預測到期後)看 Loop 2 首批真實評分。

---
*Human-facing verification archive (Traditional Chinese per the 2026-07-04/05 report
precedent); code artifacts remain English. No credentials appear in this file.*
