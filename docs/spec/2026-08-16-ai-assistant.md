# AI 投資助手 —— 規格（2026-08-16）

**狀態：** owner 裁示完成（§0）。W1（`f0ab8e7`）、W2（`c609c57`）、W3 已出貨；W4–W8 依序實作。
**基準：** branch `feat/corporate-actions`，未合併，`__version__` 0.1.28。
**上游：** `docs/reports/2026-07-06-ai-assistant-development-blueprint.md`（願景與誠實目標函數）；
`C:\Users\hefeng\.claude\plans\validated-riding-mitten.md`（核准的實施計畫，含每案 3–5 選項與比較表）。

> 盤點結論：**機器已經蓋好了，缺的是四條腿裡的一條、驗證，和一個落點。** 四個閉合 LLM 迴路、
> 34 變數／10 類、四法則技術引擎（rules-v1 + TechScore）、13 條警示規則、11 個登錄提示詞、
> 逐筆成本記帳＋預算硬閘門、R1-R8 閘門——全在。本計畫幾乎不造新機器：它接線、補腿、修錯。

owner 的「AI 助手」＝ **AI 建議**：用**倉位持股資訊 × 市場新聞 × 基本面 × 技術面**的各種 AI
分析洞察，給出**建議與提點**。不是一個新的互動介面。

---

## §0 決策表

| # | 決策 | 裁示 | 理由 |
| --- | --- | --- | --- |
| **AI-D1** | 互動形態 | **純批次卡片** | owner 裁示。自由對話會一次拆掉三個機制：指紋快取（問題不固定→失效）、Loop-2 評分（沒有固定預測就無法計分）、以及 `stack.md` 明列的唯一重開 SPA 條件（「串流聊天成為主要介面」）。在 1–2 人、月費 ~$3–6 的規模下，「任意提問」的價值低於它拆掉的東西 |
| **AI-D2** | 技術分析加深 | **修「均線交叉」重名 → 訊號歷史＋事件研究回測；不加指標** | owner 裁示。`technicals.ma_cross` 用 20/60、`strategy/rules/ma_cross` 用 50/200，且**同時**經 `technical_signals_json` 與 `rule_signals_json` 餵進同一份提示詞——助手會引用互相矛盾的交叉。`signal_states` PK=symbol（一列一標的）→ 回測不可能 → 而藍圖 D3a 說指標擴充「等回測數據說話」→ **死結**。回測是鑰匙 |
| **AI-D3** | AI 門抽取 schema | **判別式聯集：交易＋股利＋資金，一份提示詞** | owner 裁示。真實對帳單本來就是混的（同一頁有買賣、股利、利息、券商費用）；四道獨立門會逼 owner 貼四次、付四次 vision 費，並誘發「跳過麻煩那幾列」——那正是帳本失真的來源。公司行動留在 CSV／表單（需 ratio 代數兩欄＋批次層驗證，形狀不同）。⚠ **資金列的方向存在 `kind` 裡**（amount 一律無號）：錯標一個 `BROKER_FEE`→`DEPOSIT`，資金池反向錯 **2×金額**且無人報錯，並經 F3 放大到整個外幣曝險 |
| **AI-D4** | 基本面來源 | **三家全做成 provider（yfinance／Finnhub／Alpha Vantage），金鑰門控、日後註冊即串入；已啟用者資訊一起納入；逐筆標記來源** | owner 裁示。`chips` 類 5 個變數全是 FinMind = **TW only**（`variables.py:211-225`）；US／MY 只有 `consensus_json`（分析師**意見**，不是基本面）。真實部位是美股為主 → **台股卡結構性地比美股卡強**。⚠ 與 `pricing/defaults.py` 的先成功者勝 fallback 鏈**不同**：這是聯集。已同步記入 `.claude/rules/data-and-pricing.md`。**兩家不一致時：兩值都留、標來源、標記 `disagreement:true`，絕不取平均**——平均是憑空造一個沒有來源支持的數字 |
| **AI-D5** | 提點（主動提醒） | **納入：官方 `on_alert` 建議卡，預設啟用** | owner 裁示。這條路已蓋好但預設關著（on_alert 任務建立時 disabled，`insights.py:315-318`）；`alerts_bridge` 的 24h 去抖、R7 訂閱閘門全部已內建。推播（ntfy）本次不開 |
| **AI-D6** | 整體路線 | **雛形先上 demo，燃料與驗證並進** | owner 裁示。prod 帳本自 2026-07-22 起是空的（0 筆交易），但 demo 有合成部位可點；owner 的既定審閱方式是「點得到的 demo，不是寫出來的規格」。prod 只在計分板補完後才看到助手 |
| **AI-D7** | 計分板 | 接基準重放（`relative`）＋實現波動（`volatility`） | 建議、owner 未推翻。`insight_service.py:396-445` 兩者回 None → 永遠 `pending_data`→`undetermined`，被排除出所有彙總——戰績頁「乾淨」是因為打不了分的被丟掉了。盲點的方向對助手有利，不可接受。兩條都是接線，不是新演算法 |
| **AI-D8** | 產品立場 | 方向性判讀＋條件情境；**不做部位大小、不做下單** | 建議、owner 未推翻。實查全庫無 beta／相關性／Sharpe——給部位大小沒有風險模型可靠（藍圖 §9.3 第 2 條） |
| **AI-D9** | 模型角色 | **不新增角色** | 建議（AI-D1 的直接後果）：沒有獨立功能，第 7 個角色只是空欄位 |
| **AI-D10** | 建議卡的預測由誰決定 | **自由決定（維持卡 schema 現況）** | owner 裁示。預測選填、帶預測則必須帶信心（`cards.py:53-58` 既有）。能真心說「資料不足」比「每張卡都被評分但可能硬給數字」重要 |
| **AI-D11** | 提點卡訂閱哪些警示 | **風險警示六條**：`target_cross`／`single_weight`／`fx_drift`／`drawdown_from_peak`／`vol_spike`／`consensus_change` | owner 裁示。⚠ `signal_*` 轉換事件**刻意不含**在 `'all'` 通配符裡（`gating.py:89-101`，原本就是設計如此）；資料健康類（`missing_price`／`quota_low`）LLM 沒有內容可詮釋 |
| **AI-D12** | 建議 preset 預設啟用範圍 | **建啟用、demo 先行** | owner 裁示。依既有慣例建為 enabled（每個官方 preset 都是；on_alert 過去是唯一例外）；merge/main/prod 保持不動直到 owner 明確同意 |
| **AI-D13** | W3 掛法（抓取層架構） | **混合式**：`DataType.FUNDAMENTALS` 進 enum，三家 provider 用 `supports()` 宣告能力（金鑰門控沿用 `default_registry` 已接好的 DB token_getter 佈線），**不進** `DEFAULT_PROVIDER_ORDER` fallback 鏈；抓取與解析放新的 `pricing/fundamentals_source.py`（比照 `consensus_source.py` 的純函式＋I/O 縫） | owner 裁示（W3-1）。聯集語義下鏈的「順序」毫無意義，但 `supports()`/`capable_ids()` 是現成的能力＋金鑰閘。owner 附問：多來源會不會讓線圖混亂——**不會**：`prices` 主鍵一檔一天一列、先成功者勝、重跑同鍵 upsert，圖表永遠只讀到一個收盤價；聯集只發生在 `external_snapshots`，而圖表從不讀它 |
| **AI-D14** | W3 合併形狀 | **不合併，每家一塊**：`fundamentals_json = {source: block}`；欄名在各塊內正規化為同一組 canonical 名（改名不是合併）；「不取平均」紅線由提示詞執行——不同來源不同值必須並陳並標來源，不得平均、不得調和出任何一家都沒報過的數字 | owner 裁示（W3-2），**修訂 AI-D4 的機制**（原為合併層逐欄 `{value, source}`＋`disagreement` 旗標；兩值都留、各自標來源的精神不變——塊鍵本身就是來源）。已同步修訂 `.claude/rules/data-and-pricing.md` 的 AI-D4 節 |
| **AI-D15** | W3 欄位集 | **固定 canonical 交集 8 欄**：`pe_ratio`／`pb_ratio`／`eps_ttm`／`market_cap`／`dividend_yield_pct`／`beta`／`roe_pct`／`revenue_growth_yoy_pct`；缺欄＝缺席不虛構；TW 由既有 FinMind valuation 快照映射出 `finmind` 塊（不重複抓取） | 建議、owner 採納（W3-3）。yfinance 避開 `Ticker.info`（`consensus_source.py:4` 的明文紀律），改由財報端點在抓取縫**本地推導**（比照 consensus 縫算 rating_score 的先例）；單位在縫上統一（市值＝報價幣別原始單位，殖利率／ROE／成長率＝百分比） |
| **AI-D16** | W3 排程與額度 | `fundamentals_daily` 日跑 yfinance＋Finnhub（全宇宙）；`fundamentals_av_weekly` 週六跑 Alpha Vantage、**僅持有標的**（registered-runner 模式：持有集由 api 層算好注入） | 建議、owner 採納（W3-4）。AV 免費額度 25 calls/day，全宇宙一輪即爆；持有集是 `portfolio/` 的重放結果，`pricing/` 算不出——沿用 `signal_scan`／`alert_compute` 的 runner 註冊縫。實作為**兩個** job（run 歷史與健康歸屬各自乾淨），裁示文字「同 job」的意圖不變。探針先行：yfinance 腿即時可跑，finnhub／AV 腿待金鑰進場後跑，頻率依探針結果修 |
| **AI-D17** | W4 schema 形狀 | **判別式聯集＋kind 鑑別欄**：`AiDraftList.rows: list[TxnDraft\|DivDraft\|CashDraft]`（Pydantic `Field(discriminator="kind")`）＋ `unparsed: list[{text, reason}]` | owner 裁納（W4-1，2026-08-18）。缺欄在解析邊界就擋＋重試一次，不進預覽才爆；保留對帳單原始列序。`unparsed` 讓模型坦白「這幾列分不出來」（換匯／公司行動／選擇權）——**不靜默丟棄**，那正是 AI-D3 要消滅的失真來源。已查明 `complete_structured` 是提示詞＋解析制，聯集無 provider 端 oneOf 風險 |
| **AI-D18** | W4 預覽／commit 路徑 | **三類各走既有門**：drafts 依 kind 分組 → 各渲染一份 canonical CSV → 各叫既有整檔 builder（cash 由 router 注入 `cash_pool_fn`，`ai_agents_input` 增 required kwarg）→ 前端逐類打既有 `/api/import/commit` | owner 裁納（W4-2）。零新端點／writer，undo 顆粒度不變，C7 列↔行不變式在每類內成立，驗證與 CSV 門零漂移。否決：後端混合端點（多一個端點＋三 batch 協調，部分失敗處理照樣要寫）、dispatching writer 單 batch（undo 一按撤三類、provenance kind 語意撐彎） |
| **AI-D19** | W4 提示詞 v6 | **三段顯式＋兩旗標進場**：共用規則＋三類各一段＋各一 one-shot；kind 詞表模組層 join 自 `cash_kinds.py`（GICS `_GICS_SECTOR_LIST` 前例）；`daytrade` 補「僅使用者明示當沖才設」（W1 刻意欠的）；`short_sale` 同規格進場（僅明示 放空/融券/short） | owner 裁納（W4-3）。`agents.py` 的欄位註解明寫 short_sale「與提示詞規則一起進」——這波就是那波；真實 Schwab 對帳單有 declared short，不進則混抽時那幾列卡賣超 |
| **AI-D20** | W4 語料與閘門 | **合成文字語料＋live 報表**：`tests/golden/ai_extraction/` 30–50 例（三類×三市場×邊界）；`scripts/ai_extraction_eval.py` 產欄位級命中率報表，**cash `kind`／`daytrade`／`short_sale` 錯標率獨立列出**；手動跑，門檻隨第一輪基線校準；一條確定性 pytest 只驗語料檔本身防腐爛；截圖案例走人工複核（只落 `docs/human_noted/`） | owner 裁納（W4-4）。live runner 不進 CI：非確定＋花 token＋pytest-socket 禁網。否決卡帶回放——它量的是管線（已有測試）不是抽取品質，而語料要解決的是「改提示詞不再盲改」 |
| **AI-D21** | W4 預覽呈現 | **三個區段**：交易／股利／資金各一表格；資金列顯示**中文 kind＋明示 ±**（＋入金／−券商費用——AI-D3 硬性要求的落點，中文詞表留 server 單源：payload 附 `kind_label`＋`sign`）；股利區顯示類型中文＋毛/扣/淨；空區段不渲染；`unparsed` 列在頂部提示條 | owner 裁納（W4-5）。否決單表（欄位聯集空格多）與分頁（藏兩類易漏看） |

> **命名慣例**：本企劃的裁示用 `AI-D<n>`，**永不**與公司行動 spec 的 `D<n>` 編號空間混用。
> 先例教訓：上一輪藍圖把自己的裁示寫在它自己的 §10（D1–D9），產生了「那是藍圖內部裁示
> 還是全域裁示」的真實歧義。

---

## §1 現況盤點（實查，檔案：行號為證）

### 1.1 已經有的（不要重蓋）

| 層 | 資產 | 位置 |
| --- | --- | --- |
| 提示詞制度 | `PROMPT_REGISTRY` 11 條，新增 LLM 呼叫點若未登錄則測試失敗 | `llm_insight/official_templates.py:522` |
| 變數 | 34 個 `{{token}}`／10 類，scope 驗證、市場切片、tier 門控 | `llm_insight/variables.py:79` |
| 技術法則 | 趨勢濾網 MA200／50-200 交叉＋量能確認／12-1 動能／RSI-52週；TechScore＋evaluation_context | `strategy/rules/`，`params.py:16` `rules-v1` |
| 指標 | SMA/RSI(Wilder)/年化波動/最大回撤/52週位置/趨勢結構/量能，全部 Decimal | `portfolio/technicals.py` |
| 評分 | quant hit + master 敘事 0-100 + 信心校準分箱 | `llm_insight/scoring.py`、`evaluations_store.py` |
| 閘門 | R1-R8 一份純函式，執行與 dry-run 共用（不可能分歧） | `llm_insight/gating.py:114` |
| 成本 | 6 角色 × 模型註冊表、逐筆 `llm_usage`、Σtopup−Σusage 硬上限、`quota_low` 警示 | `shared/llm_config.py` |
| 落點 | `insights.html` 三分頁；個股抽屜已有技術訊號區 | `web/detail.js:549` |

### 1.2 缺的（誠實清單）

| 缺口 | 證據 | 後果 |
| --- | --- | --- |
| prod 沒有部位 | 帳本 0 筆（≥2026-07-22） | 助手今天上線，會對著空組合說話 |
| 2/3 的預測打不了分 | `insight_service.py:440` / `:443` | `relative`／`volatility` 永遠 `undetermined`；戰績頁乾淨是因為打不了分的被排除 |
| 沒有訊號歷史 | `signal_states` PK=symbol | 「TechScore 怎麼走的」無法回答；回測不可能 |
| 「均線交叉」有兩個定義 | `technicals` 20/60 vs `rules` 50/200 | 兩者同時進同一份提示詞 |
| OHLC 不可用 | `open/high/low` 有存但從不做分割還原、全庫無讀者 | 第一根 K 線就會畫錯（`pricing/store.py:148-154` 自陳） |
| 抽取品質從未被量測 | `tests/` 只測管線，無準確度語料 | 改提示詞是盲改 |
| 無日內、無 beta／相關性／Sharpe、基本面僅 TW | 實查 grep 全庫 | 部位大小建議沒有風險模型可靠 |

---

## §2 ★ AI 門三個實查缺陷（W1，**已出貨 `f0ab8e7`**）

盤點時實查，修正前各寫一條反證測試看過紅：

| # | 缺陷 | 實測 |
| --- | --- | --- |
| **AI-1（高）** | `daytrade` 進得了預覽、進不了寫入 | 預覽算 0.15%（**900**），commit 從 CSV 重推導後寫入 0.3%（**1800**）。費稅是 `original_total` 的一部分，成本基礎跟著錯 |
| **AI-2（中）** | `_drafts_to_csv` 不做 CSV 跳脫 | ⚠ 比預期嚴重：不是欄位偏移，是**崩潰**。多餘欄位落在 DictReader 的 `None` 鍵，`AttributeError` 在 try/except **之外**拋出——never-500 一族，觸發它的是提示詞主動要求模型寫的備註裡的一個逗號 |
| **AI-3（低-中）** | JPEG/WebP 被標成 `data:image/png` | 門用 magic byte 正確嗅出型別，傳輸層卻一律標 PNG——自己認出型別後又標錯 |

**修正帶出的新責任**：`daytrade` 現在有牙齒了，所以同一次改動加上預覽列的琥珀「當沖」標記
（含 e2e；`daytrade` 在 wire 上是字串 `"0"`，JS 裡 truthy，用真值判斷會讓每列都掛標記）。
**刻意沒做**：提示詞從未教過模型何時設 `daytrade`（只在 `<schema>` 出現）——沒有準確度語料前
改提示詞是盲改，留給 W4 跟語料一起走。

---

## §3 波次

| 波次 | 內容 | 決策 | 規模 | 狀態 |
| --- | --- | --- | --- | --- |
| **W1** | AI 門三缺陷（AI-1/2/3）＋ 資金列正負號預覽 | E 必修 | S | ✅ **出貨 `f0ab8e7`** |
| **W2** | 修「均線交叉」重名 ＋ **助手雛形**：抽屜內建議卡、`on_alert` 提點卡（只用現有變數） | AI-D2 · AI-D1 · AI-D5 | S-M | ✅ **出貨 `c609c57`** |
| **W3** | 基本面三家 provider（聯集並存、每家一塊、塊內欄名正規化）→ `fundamentals_json` 變數 | AI-D4 · AI-D13–D16 | L | ✅ 本波提交（探針證據見 §4） |
| **W4** | AI 門判別式聯集擴充（交易＋股利＋資金）＋ 準確度語料與閘門 | AI-D3 · AI-D17–D21 | M | 🔧 裁示完成（2026-08-18），實作中 |
| **W5** | 計分板補洞：基準重放（`relative`）＋ 實現波動（`volatility`） | AI-D7 | M | ⬜ |
| **W6** | 訊號歷史（時序表）＋ 事件研究回測 → 點亮 `backtest_json`／`calibration_gap_json` | AI-D2 | L | ⬜ |
| **W7** | 助手完全體：組合層、引用回測數字、戰績頁升級為決策品質儀表 | AI-D1 · AI-D9 | M | ⬜ |
| **W8** | 選配：OHLC `*_raw`＋factor 地基、週線視角 | — | M | ⬜ 可延後 |

> W3 與 W4 互相獨立，可對調或並行。

### 關鍵檔案

| 波次 | 檔案 |
| --- | --- |
| W1 | `data_ingestion/agents.py` · `shared/llm.py:226-236` · `shared/image_types.py`（新） |
| W2 | `portfolio/technicals.py` ↔ `strategy/rules/params.py` · `llm_insight/official_templates.py`（模板＋preset＋`PROMPT_REGISTRY`）· `api/routers/insights.py`（on_alert 預設啟用）· `web/detail.js` |
| W3 | `pricing/enums.py`（+FUNDAMENTALS）· `pricing/providers/{yfinance,finnhub,alphavantage}_provider.py`（supports 宣告）· 新 `pricing/fundamentals_source.py`（比照 `consensus_source.py`）· `pricing/ingest.py`（union 迴圈）· `scheduler/jobs.py`（兩 job＋runner 縫）· 新 `api/fundamentals_service.py`（AV 週跑 runner）· `llm_insight/variables.py`（新變數）· `api/routers/prompts.py::_external_vars`（每家一塊組裝＋TW finmind 映射）· `llm_insight/official_templates.py`（advice 模板 v2：基本面段＋不平均紅線） |
| W4 | `data_ingestion/agents.py` · `cash_import.py`／`dividend_import.py`（復用）· `api/routers/input_center.py`（`_BUILDERS`/`_WRITERS`）· `data_ingestion/import_templates.py:35-47`（七／八個註冊點）· `web/input.js` · `tests/golden/ai_extraction/`（新）· `scripts/ai_extraction_eval.py`（新，live runner） |
| W5 | `api/insight_service.py:396-445` · `pricing/benchmarks.py` · `portfolio/twr.py` |
| W6 | `strategy/signal_states.py`（新歷史表）· 新 `portfolio/backtest.py` · `llm_insight/variables.py:264-275` |
| W7 | `llm_insight/official_templates.py` · `web/insights.html` |
| W8 | `pricing/schema.py` · `pricing/store.py` |

---

## §4 驗證

- 每波：`pytest` 全套（**不接管線**，exit code 才是真的）· `mypy --strict` **裸跑** · `ruff` ·
  `/stress-audit --phase 1` `fail=0`（**基準 = `ops=126 pass=4780`**，確定性，別的數字就是真訊號）。
- **W1 反證**（已過）：`daytrade` 那條在修正前是紅的。
- **W3**：三家各一組 fixture（含**無金鑰降級**與**兩家並存**兩種情境）；斷言
  `sources` 下**每家一塊、兩值皆在**（AI-D14——不設合併層，也就沒有「平均」可斷言）。
  **探針證據（2026-08-18，`scripts/probe_fundamentals.py`）**：yfinance 腿對 US／TW／MY
  （含 sub-RM1 小板）8 欄得 7（`beta` 為設計性缺席——不碰 `Ticker.info`）；finnhub／AV
  腿待金鑰進場後補跑。探針順帶抓到 `fast_info` 的鍵名在這版 yfinance 是 camelCase
  （文件與舊版是 snake_case）——見 `LESSONS_LEARNED.md`。
- **W4**：欄位級命中率報表；**資金列 `kind` 錯標率與 `daytrade` 錯標率單獨列出**——
  它們是唯二會動錢而不報錯的欄位。
- **W6**：回測附最小樣本門檻與重疊註記；樣本不足時輸出「不足以判斷」而非數字。
- 每波在 **demo 站**行為驗證：`scripts/verify_live.py` + 真實瀏覽器走完新流程。
- ⚠ **分支紀律不變：不 merge、不 tag、`__version__` 維持 0.1.28**，直到 owner 同意。

---

## §5 明確不在範圍

| 項 | 為什麼 |
| --- | --- |
| 自由對話／串流聊天 | AI-D1；且是 `stack.md` 唯一重開 SPA 的條件 |
| 第 7 個 LLM 角色 | AI-D9：AI-D1 之下沒有獨立功能，只是空欄位 |
| 部位大小建議、自動下單 | AI-D8：無 beta／相關性／Sharpe，沒有風險模型可靠 |
| MACD／布林／ATR 指標包 | AI-D2：D3a 說等回測數據，回測要先有 W6 |
| 基本面取平均／挑一家為準 | AI-D4：兩值都留、標來源、標不一致——平均是憑空造數字 |
| 推播（ntfy） | AI-D5：本次只做站內提點卡，推播是另一個開關 |
| 日內資料 | `prices` PK 是 `(instrument, as_of_date)`，一天一列——結構限制不是設定 |
| 公司行動進 AI 門 | AI-D3：需 ratio 代數（兩欄，E6a 拒絕預除）＋批次層驗證，形狀不同 |
| 真實交易資料進 repo | 隱私鐵則；人工複核結果只落 `docs/human_noted/` |
