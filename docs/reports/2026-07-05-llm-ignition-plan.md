# LLM 點火方案(P0)— 設定級執行計畫

> 定案日:2026-07-05 · 依據:`2026-07-04-llm-theme-assessment.md` §5 P0 + 本日方向討論
> 執行環境:**測試站先行**(合成資料),綠燈後 prod(v0.1.9,純設定,不需出版)
> 金鑰:OpenRouter(使用者持有,執行時親自填入測試站;**絕不入 git、絕不入對話記錄**)

---

## 0. 決策記錄(2026-07-05,human sign-off)

| 決策 | 結論 |
| --- | --- |
| 供應商 | OpenRouter(一把金鑰跨供應商;金鑰由使用者於測試時填入) |
| 首發洞察類型 | 第一波:持倉週報+個股健檢(含籌碼大戶解讀);第二波:風險警示解讀+股利現金流前瞻;第三波(P1 新聞接線後):個股新聞事件解讀 |
| 可驗證預測 | 開,少量:個股健檢卡帶方向+信心值;週報純敘事 |
| 建議邊界 | 系統提示詞第 5 條由「不提供買賣建議」改為「**方向性判讀**」:允許偏多/偏空/觀望+條件式情境,禁止具體買賣指令與部位大小 |
| 新聞接線(P1) | 點火穩定後立刻排(NewsAPI,資料源目錄 pending) |

---

## 1. 模型指派表(三個註冊列 → 六個角色)

單價為 2026-07-05 OpenRouter 現查行情(USD / 百萬 tokens)。
`model_name` 填 OpenRouter 完整路徑;LiteLLM 字串由系統組成 `openrouter/<model_name>`。

| 註冊列 id | provider | model_name | vision | 輸入價 | 輸出價 | context | max_out | timeout | retries |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `haiku-4.5` | openrouter | `anthropic/claude-haiku-4.5` | 1 | `1` | `5` | 200000 | 4096 | 60 | 2 |
| `sonnet-5` | openrouter | `anthropic/claude-sonnet-5` | 1 | `2` | `10` | 1000000 | 8192 | 120 | 1 |
| `gemini-2.5-flash` | openrouter | `google/gemini-2.5-flash` | 1 | `0.30` | `2.50` | 1048576 | 4096 | 60 | 2 |

⚠️ Sonnet 5 的 $2/$10 是 **2026-08-31 前的導入價**,9/1 起 $3/$15 —— 屆時要回設定頁更新單價(用量記帳靠它,填錯=預算閘門失真)。

| 角色 | 綁定 | 理由 |
| --- | --- | --- |
| default(產卡主力,量最大) | `haiku-4.5` | 結構化 JSON 穩、繁中品質好、便宜 |
| default_fallback | `gemini-2.5-flash` | 跨供應商、穩定版(非 preview)、更便宜 |
| vision(對帳單截圖→交易草稿) | `sonnet-5` | 量極小、數字辨識準確度優先(草稿仍經人工確認) |
| vision_fallback | `gemini-2.5-flash` | 具視覺、跨供應商 |
| master(評分/校準/驗證) | `sonnet-5` | 高推理;只在預測到期與週校準時呼叫,量小 |
| master_fallback | `haiku-4.5` | 迴路不中斷;校準品質略降可接受 |

## 2. 預算與成本試算

- 首儲 **$5**(累加式 top-up,無月重置;用完再儲)。警戒線用預設(剩餘 <$1 告警)。
- 週節奏成本(13 檔健檢+1 張週報,haiku 級;master 評分/校準 sonnet 級):
  約 **$0.12/週 ≈ $0.5/月**,加 master 增量估 **<$1/月** → $5 約可跑半年。
- 保護機制皆已內建:逐 call 記帳(`llm_usage`)、額度中斷(R6,批次中途耗盡→partial 保留已產卡)、快取指紋命中不扣費。

## 3. 系統提示詞修訂稿(設定 › 提示詞,全域單一)

第 4 條放寬(原「2–3 句」對週報過緊)、第 5 條改為方向性判讀、新增第 6 條新鮮度紀律:

```
你是資深投資組合分析師，服務一位同時持有台股、美股、馬股的個人投資者。

原則：
1. 一律使用繁體中文（台灣用語）回答。
2. 金額必須標注幣別；不同幣別不可加總。
3. 損益語意採台灣慣例：紅漲綠跌。
4. 每則洞察精簡扼要，必須引用輸入資料中的具體數字。
5. 可給方向性判讀（偏多／偏空／觀望）與條件式情境（例：「跌破 60 日均線宜重新評估」），
   並說明所依據的數據；不給具體買賣指令、不建議部位大小、不代替使用者決策。
6. 輸入資料缺漏、過期或標記不新鮮時，如實說明；絕不以猜測或外部記憶填補數字。
```

防越權仍是雙層:此邊界只有人能改;Loop 3 自動校準有 denylist+master 驗證,不能自行變激進。

## 4. 第一波策略提示詞草稿(×2)

JSON 輸出格式由管線強制(`InsightCard` schema),提示詞**只寫內容指令**。
`{{token}}` 於執行時代入實值;composer 預覽會驗 token 與範圍。

### 4.1 持倉週報(scope=portfolio,純敘事)

```
請以「本週持倉週報」為題產出一張綜合洞察卡，依序涵蓋五節：

一、組合總覽 — 引用總市值、總報酬、XIRR、已實現/未實現拆分，總評本週組合狀態。
{{kpis_json}}

二、配置觀察 — 由產業配置與持倉權重點出集中度最高的部位及其風險意涵。
{{allocation_json}}
{{holdings_json}}

三、幣別與匯率 — 各幣別報酬分列評述（不可加總）；以換匯損益歸因說明匯率對組合的影響方向。
{{returns_by_ccy_json}}
{{fx_json}}
{{fx_rates_json}}

四、股利現金流 — 未來除息事件與年度已宣告股利，指出下一筆現金流的時點與金額。
{{ex_dividend_calendar_json}}
{{dividend_projection_json}}

五、市場環境 — 以情緒指標與三地大盤 20 日動能，一句話定調當前環境。
{{market_sentiment_json}}
{{index_quotes_json}}

守則：資料時間 {{as_of}}；依 {{freshness_json}} 檢查新鮮度，缺價或過期的標的必須點名
並排除於結論之外。本卡為純敘事回顧，不附預測（prediction 留空）。
```

### 4.2 個股健檢(scope=per_symbol,帶預測)

```
請對下列標的做一次持股健檢，產出一張洞察卡：
{{symbol_detail_json}}

一、部位現況 — 現價相對原始/調整均價的位置與未實現損益（引用具體數字）。
{{price_vs_cost_json}}

二、技術面 — 均線位置與乖離、30 日波動與回撤，並以近期日線走勢一句話總結型態。
{{ma_signals_json}}
{{volatility_json}}
{{price_history_json}}

三、籌碼與基本面（僅台股有值；變數為空時整節跳過，不得虛構）—
法人買賣超與連買賣天數、融資融券變化、月營收動能、估值位階（PER/PBR 歷史百分位）、
近四季財報摘要。點出籌碼大戶動向與基本面是否相互印證。
{{institutional_json}}
{{margin_json}}
{{monthly_revenue_json}}
{{valuation_json}}
{{financials_json}}

四、環境對照 — 相對所屬大盤的強弱與當前市場情緒。
{{index_quotes_json}}
{{market_sentiment_json}}

五、方向性判讀 — 綜合以上給出偏多／偏空／觀望之一，附條件式情境與所依據的數據；
並以 prediction 欄位給出未來兩週的方向預測與信心值（confidence）。
判讀只到方向與條件，不給買賣指令或部位大小。

守則：資料時間 {{as_of}}；依 {{freshness_json}} 標記新鮮度；缺漏資料如實說明。
```

## 5. 洞察類型組裝參數

| 參數 | 持倉週報 | 個股健檢 |
| --- | --- | --- |
| scope | portfolio | per_symbol |
| 掛載策略 | §4.1(單一) | §4.2(單一) |
| use_system_prompt | ✓ | ✓ |
| self_correct(Loop 3) | ✗(純敘事無量化錨,校準無意義) | ✓(auto_promote 維持預設關,校準版本人工檢視) |
| horizon_days | —(無預測) | 14 |
| universe | — | 全持倉(非台股籌碼變數自然為空,提示詞已交代跳過) |
| 排程(prod 啟用時) | 每週六 09:00(美股週五收盤後) | 每週一 09:00(台股籌碼資料週末已齊) |

第二波備忘:風險警示解讀用 `on_alert` scope(訂閱警示規則,24h 防抖,預測窗強制 ≤3 交易日);股利現金流前瞻每月一次。

## 6. 測試站點火步驟(runbook)

前置:測試站資料為合成(`seed_demo`),點火驗證不經手真實持倉;排程停用,一切手動觸發。

1. **設定 › LLM 與額度**:照 §1 建三個模型列,填 OpenRouter 金鑰(建議測試站用可獨立撤銷的一把,或在 OpenRouter 端設消費上限)。
2. 綁定六角色(§1 下表)。
3. 儲值 $5;警戒線維持預設 $1。
4. **設定 › 提示詞**:貼上 §3 系統提示詞修訂稿。
5. 建立 §4 兩個策略提示詞+§5 兩個洞察類型;composer **預覽**確認 token 全綠(unknown/scope violation 會列出)。
6. **手動觸發**(pipeline hub):先跑週報(1 call,便宜),檢視卡片;再跑個股健檢(逐檔)。
7. 驗證清單(Loop 1):
   - [ ] 洞察頁渲染正常:繁中、幣別標注、引用數字與儀表板一致;
   - [ ] `llm_usage` 逐 call 記帳出現、剩餘額度遞減、金額與單價表吻合;
   - [ ] 立即重跑同任務 → 快取指紋命中,**不再扣費**;
   - [ ] 缺價標的 → 「資料異常」定額卡(零 LLM 成本);
   - [ ] 解綁 default 角色再觸發 → 誠實降級(AINotActivated),綁回後恢復。
8. **隔日**手動觸發 Loop 2 評分:確認 quant verdict(到期預測)、master 敘事分、資料未齊時 `pending_data` 不硬判。
9. 全綠後 **prod 點火**:同表組裝、另填金鑰與儲值、啟用 §5 排程。prod 停在 tag v0.1.9 —— 點火是純設定,不需出新版。
10. 下一輪(P1):新聞/qualitative 接線(NewsAPI)→ 第三波洞察類型;Vision 前端上傳位接線;`/api/ai-score` 準確度卡。

## 7. 單價出處(2026-07-05 查核)

- [Claude Haiku 4.5 — OpenRouter](https://openrouter.ai/anthropic/claude-haiku-4.5)($1/$5)
- [Claude Sonnet 5 — OpenRouter](https://openrouter.ai/anthropic/claude-sonnet-5)($2/$10 導入價至 2026-08-31,後 $3/$15)
- [Gemini 2.5 Flash — OpenRouter](https://openrouter.ai/google/gemini-2.5-flash)($0.30/$2.50)

---
*Note: this plan is intentionally in Traditional Chinese — it is a human-facing runbook the user
executes against the live settings UI (same precedent as the 2026-07-04 assessment archive);
code artifacts remain English per the bilingual protocol. No credentials appear in this file.*
