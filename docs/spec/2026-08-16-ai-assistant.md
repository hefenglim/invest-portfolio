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
| **AI-D22** | W5 基準對應 | **固定市場對應，MY 誠實缺**：{TW→`0050`, US→`sp500`}（`pricing/benchmarks.py::BENCHMARKS` 的 key，新 helper `benchmark_for_market` 收在其旁單源），依個股市場（`instruments` 表）推得；MY 無基準 → `benchmark_return_pct=None` → 誠實 pending_data | owner 裁納（W5-1，2026-08-19）。零 schema 變更、零提示詞變更，**基準選擇權在程式不在 LLM**。否決：Prediction 加 benchmark 欄位（LLM 選尺有漂移風險＋既有列遷移）、MY 用 S&P 500 代理（MYR 對 USD 指數，噪音蓋過訊號）。^KLSE 有 yfinance 後綴路由坑（`^KLSE.KL`），列為日後便宜追加 |
| **AI-D23** | W5 超額報酬幣別 | **雙腿皆本地幣**：個股報價幣 vs 基準自身幣——每個市場兩腿天然同幣（AAPL/USD vs ^GSPC/USD、2330/TWD vs 0050/TWD） | owner 裁納（W5-2）。否決雙腿換算報表幣：FX 對兩腿相同、近似完全抵銷，結論一樣卻多一層 FX 掉落點（缺匯率的日子整列掉落）。**`convert_closes` 因此刻意不用**——記錄在此，免得日後有人「修」它 |
| **AI-D24** | W5 波動視窗 | **固定 30 日窗，due vs create**：`vol_change_pct = vol_30d(due)/vol_30d(create) − 1`，整段序列先 `series_in(valued_on=due)` 再切窗（分割落窗內不重表達會把 −95% 裂變當波動飆升）；基線 vol=0 或任一端歷史不足 → 誠實 None | owner 裁納（W5-3）。與 `alert_inputs.py:158-159` 的 vol_30d/vol_90d 同一估計器；horizon=3 的 on_alert 卡不退化成 3 點噪聲。否決：horizon 窗 vs 等長前窗（horizon=3 → 純噪聲；horizon=120 → 需 240+ 收盤）、30d vs 90d 基線比（量「近期 vs 長期」而非「預測後 vs 預測前」，與預測語義不對齊） |
| **AI-D25** | W5 波動 flat 帶 | **波動專屬帶 ±5%**：`scoring.py` 新增 `_VOL_FLAT_BAND = Decimal("0.05")`，`_score_volatility` 傳 `band=`；`price_change`／`relative` 維持 ±0.5% 不動 | owner 裁納（W5-4）。30 日窗估計器在恒定序列上本身有數個百分點抖動，±0.5% 會讓 `direction=flat` 的波動預測幾乎必敗——變相懲罰整個方向類別，與「看起來在計分其實沒有」同類。否決 ±10%：±5% 已大致覆蓋估計器噪聲，更寬會吞掉真實的小幅升降 |
| **AI-D26** | W5 範圍 | **個股層兩指標＋順修戰績列狀態**：組合層（symbol=None）維持誠實 None（W7 決策品質儀表再議）；同波修 `web/insights.html::renderScoreRows` 不讀 `status` 的既有缺陷——pending_data／undetermined 列現在 miss=0 被畫成「✓ 命中」（`_row_wire` 本就帶 status，純渲染修復） | owner 裁納（W5-5）。否決連組合層 relative 也接（twr_index/build_overlay 現成套件可行，但要再接 trend points/net invested 流程，超出本波「接線不是新演算法」的定位）。官方模板提示詞**不**放行 relative/volatility——自訂模板已能發，提示詞變更是語料品質問題，另一波 |
| **AI-D27** | W6 歷史來源 | **回放回填＋掃描增量**：rules 引擎是純函式（`rules-v1` 版本戳），對價格史逐日重放 `evaluate_symbol` 確定性重建進新表 `signal_history`；之後 `signal_scan` 每日增量補尾（**missing-set 規則**：`price_dates − stored as_of`，涵蓋左緣洞／中間洞／中斷自續） | owner 裁納（W6-1，2026-08-20）。重建值＝當時實採值（同價同參數、**同一 `_read_series` 組裝路徑**，逐日重組裝），非捏造；公司行動重表達價格時連動刪列（連 `signal_states` 一起——見 W6 節）由次掃重建。否決：純前向累積（momentum 暖機 252 交易日＋+120 前瞻 → 黑暗期 12–18 個月，D3a 繼續鎖死）、渲染時即席回放（無持久資產、每次渲染重算） |
| **AI-D28** | W6 快照粒度 | **完整每日狀態向量**：4 法則 state+score、tech_score、evaluation_context、params_version，PK `(symbol, as_of)`——as_of＝**價格資料日**（評估實際描述的日期），非掃描日；假日重掃冪等覆寫同列 | owner 裁納（W6-2）。能畫走勢、能做法則＋composite 兩層研究；30 標的×5 年 ≈ 4 萬列，SQLite trivial。否決：只存轉換事件（與 `alert_events` 功能重疊且資訊最少——那裡還只有 3 法則、無 rsi）、只存 composite（法則層「哪條法則有效」恰好答不了） |
| **AI-D29** | W6 事件範圍 | **4 法則方向性轉換＋composite 狀態帶穿越**：法則事件＝score 符號轉換（hold 語義，對上一個非零符號；bullish：trend above_confirmed／ma_cross golden·fast_above／momentum positive／rsi oversold(+0.5)，bearish 镜像） | owner 裁納（W6-3）。法則層回答「哪條法則有用」，composite 層回答「高分後真的漲嗎」——後者正是助手最常引用的數字。否決每日觀測條件分布（同一段趨勢的 200 天＝200 個自相關假樣本，統計上誤導） |
| **AI-D30** | W6 基線與防護 | **同標的無條件基線＋全防護**：窗 +20/+60/+120 **交易日**（index-based）；基線＝該股全部有效交易日的同窗前向報酬分布；本地幣＋`series_in` 重表達（AI-D23／W6c 紀律） | owner 裁納（W6-4）。防護：事件 n<8 → 輸出「不足以判斷」不給數字（對齊 design mock 的 <8 門檻）；重疊事件註記計數（納入統計但亮明）；前瞻窗超出最新價格日的右刪失事件逐窗排除並報數；**不年化、不 Sharpe**。否決：基準指數基線（混大盤 beta、個股 drift 未控制，可日後並陳）、同市場 pooled 基線（跨標的混雜） |
| **AI-D31** | W6 變數落點 | **分流**：`backtest_json`／`calibration_gap_json` 按**宣告意義**點亮（AI 自我校準：bins＝`calibration_bins` 全域＋`overall_hit_rate`；gap＝rolling 最近 20 筆 scored、**actual−claimed** 帶號 fraction、窗 <8 誠實 unavailable）＋**新 per-symbol 變數 `signal_backtest_json`** 載事件研究 | owner 裁納（W6-5）。探查查明兩 stub 的 declared 形狀本就是 AI 自我校準（spec-04），design mock 拿它錨定信心上限——語意不能毀；事件研究的粒度（per-symbol×rule×window）與 stub 的 portfolio scope 不合。否決：兩 stub 改作回測用（毀文件化語意且 AI 校準無出口）、只點 stub 不開研究變數（W6 產出對助手無感）。34→36 變數全 live；官方模板提示詞**不動**（引用是 W7 的語料品質決策） |
| **AI-D32** | W6 composite 門檻值 | **65/35 對齊既有狀態帶**（`composite.py:29-30` `_BAND_HIGH/_BAND_LOW`）——事件＝「進入強勢／弱勢 context」，一套詞彙 | owner 裁納（W6-6）。否決 70/30＋文件區分（同一提示詞表面兩套門檻＝AI-D2「兩個 ma_cross」缺陷類）與兩套並陳（payload 翻倍、樣本更碎、n<8 更易觸發） |
| **AI-D33** | W7 引用方式 | **建議卡 v3＋健檢卡引用＋信心錨定**：v3 提示詞引用三個 W6 變數（照抄數字、事件 n<8 只說樣本不足不引用、引用必帶樣本數與同窗基線、overlap/censored 揭露）；錨定法＝信心 ≤ 對應區間 `actual_pct`+5，區間 n<8 時上限 70，`calibration_gap_json.gap` 為負時依幅度下修。個股健檢卡（v2.5→v2.6）同步引用 `signal_backtest_json`（信心法不動） | owner 裁納（W7-1，2026-08-21）。InsightCard schema 不動、**程式端不做信心夾取**——錨定是提示詞法，validator 默默改寫模型自陳的信心與「合併基本面」同類缺陷。否決：schema 加結構化引用欄（解析器／渲染／快取指紋全動，1–2 人規模過度工程）、只引用不錨定（校準變數接進提示詞卻不用，迴路只接一半） |
| **AI-D34** | W7 組合層 | **週報引用校準雙變數**：持倉週報策略 v2.1→v2.2 引用 `backtest_json`＋`calibration_gap_json`（portfolio scope、永遠可用）——週報開始敘述 AI 自己的戰績與校準 | owner 裁納（W7-2）。零新模板／preset／任務型／排程額度。否決新組合層建議模板（組合層建議的品質依賴 AI-D35 的計分先落地，且多一個任務型多一份額度） |
| **AI-D35** | W7 組合層計分 | **接 TWR 測 `price_change`**：`_measure_actual` 的 symbol=None 臂用既有 `portfolio/twr.py::twr_index`（鏈式日 TWR，流量調整——出入金不被當損益）測量 create→due 窗；relative／volatility 維持個股層 | owner 裁納（W7-3），**了結 AI-D26 留下的「W7 再議」**。量測幣別＝TWD（`evaluate_due` 加 `reporting=Currency.TWD` 預設，`run_for_id` 前例；卡片本就對著 TWD 儀表敘事）；`_FLAT_BAND ±0.5%` 同一條（AI-D25 的 vol 專屬帶不動）；`price_at_create` 無組合類比——on-or-after 腿是唯一誠實基線。否決：維持不可計分（組合層預測永不進戰績，W7-2 的敘事永不被驗證）、relative 一起接（三市場組合的混合基準是另一個裁示） |
| **AI-D36** | W7 戰績頁 | **缺口卡＋門檻＋信賴分級**：`/api/ai-score` 擴欄**不新增路由**——`rolling_gap`（與提示詞變數同一條定義，抽進 `evaluations_store` 單源）＋每 combo 的 `min_samples` 門檻顯示＋後端算好的**信賴分級章**；順修 `calibration_bins` 對齊 ROUND_HALF_UP（原 float/HALF_EVEN 路徑，同一提示詞表面兩種進位詞彙） | owner 裁納（W7-4）。分級門檻：`n < 8` → 樣本不足（錨定 `MIN_SAMPLE`）；success＝`quant_hit_rate`（`quant_n>0` 時——`combo_score` 對無量化列回 "0" 非真 0%）否則 `1 − miss_rate`；**可參考** ⟺ success ≥ 0.6 且 calib_error_pp ≤ 10（錨定 `gap_alert_pp` 預設 10）且不為 None；其餘 → 早期。web 只渲染 server 字串（web 永不計算）。否決：per-combo 可靠性圖（後端已支援，UI 複雜度對 1–2 人不划算，日後追加）。`_ratio_str`／`_avg_str` 的 HALF_EVEN **亮明不順修**（會移動 miss_rate 等更多表面） |
| **AI-D37** | W7 v3 推出 | **from-template 加 replace 模式**：既有 `POST /api/strategy-prompts/from-template` 加顯式 `mode`＋`strategy_id`（**無新路由**）；設定頁策略列在「名稱匹配官方模板且 body 不同」時顯示「同步官方 vX」，確認後覆寫 body（`strategy_prompts` 無 version 欄——以 `updated_at` 重蓋為戳） | owner 裁納（W7-5）。綁定語義：任務以 **strategy id** 綁定（`insight_type_strategies.strategy_prompt_id`）→ 覆寫即全部綁定任務下次 run 原地升版；名稱不符／已封存 → 409（伺服端重驗，防重放覆寫改名列）；R1 由 run 時閘門誠實接住（PUT 同慣例，不預檢）。否決：維持手動（W3 升 v2 時 demo 靠手動重建，每次升版重做）、pack 自動覆寫（會連使用者同名自訂一起蓋掉——skip-existing 的保護目的正是它） |
| **AI-D38** | W7.1 卡片輸出保真 | **生產端＋提示詞雙邊修**（demo 首跑實測後）：①`signal_backtest_json` 加 `units` 欄（mean/median/pct_positive 皆為**分數**，0.1336 = +13.36%）——單位原本只寫在變數登錄表 `desc`（UI 文件，模型看不到）；②提示詞明訂單位、明訂「insufficient 的格子不得引用任何數字，**且不得改用同窗 baseline 充當事件報酬**」、明訂「輸入裡沒有的數字一律不得出現」；③信心上限改由 `scoring.confidence_ceiling` **預先算好一個整數**放進 `backtest_json.confidence_ceiling`，提示詞只說「不得超過它」 | owner 裁納 2026-08-23。實測依據（demo @f1efd56，13 張建議卡＋13 張健檢卡）：信心錨定 **0/13 遵守**（40–70 vs 上限 22.14／5.00）；2/13 把同窗 baseline 當成事件報酬；1 張（NVDA）對一個 `insufficient` 格子**憑空造出 +0.0796%**（payload 全無 mean，且該值也不是基線）；同一批卡把同一個分數寫成 `0.1336%`／`0.0640 USD`／裸 `0.1053`／正確的 `+9.61%` 四種。生產端每次都是對的（sub-gate 不給數字、基線算術與獨立 oracle 12/12 相符、法則帶真數字進了提示詞）——所以修的是**語意的位置**：把單位與方向放到數字旁邊，把多步條件換成一個整數。**AI-D33 的紅線不變**：程式端仍不夾取模型自陳的信心，只是不再要求它臨場算上限。⚠ 一項**逾裁示範圍的延伸**（實作時發現後補報）：`calibration_gap_json` 加 `reading` 白話方向欄（「最近 20 筆平均高估自己 46.6 個百分點」）——週報 v2.2 首卡把 gap −0.466 讀成「低估自身表現」，而模板就在同一段寫明「正值＝我最近低估自己」；帶號分數只要讀反一次就斷言了相反的事實。⚠ **無下限是刻意的**：`confidence_ceiling` 在 demo 現況算出 **0**（39 − 46.6 penalty），代表「戰績不支持任何方向性信心」；提示詞用文字說明此情形，程式端不憑空發明樓地板——是否加下限／限制 gap 罰則上限是 owner 的下一個決策 |
| **AI-D39** | 投資邏輯審查 · 信心上限的重複扣減 | **三步修正**（修 AI-D33 的**條文**，不動其紅線）：①**刪掉 rolling-gap 扣減**——分桶 cap（`actual_pct + 5`）本身就已經是校準修正，再減一次全域缺口等於同一個誤差扣兩次（demo 實測：bins 說 39、缺口減 47、答案 0）；②**無樣本／低於門檻的區間改用 `overall_hit_rate + 5`**（該值已在同一份 payload 裡），取代常數 `CEILING_NO_DATA = 70`——70 讓「沒人量過」比「量到 40%」值錢，且與 20 筆滾動窗形成極限環；③**超標改成記錄違規率，不夾取**——`insight_evaluations` 加 `ceiling_at_create`（產卡時寫入），`/api/ai-score` 加 `ceiling_violation_rate`，戰績頁顯示 | owner 裁納 2026-08-24（四鏡頭投資邏輯審查 §3）。**AI-D33 的「程式端不夾取」紅線被強化而非放寬**：③ 是把違規變成可量測的迴路輸出，正是校準迴路該做的事；夾取仍然禁止。`overall_hit_rate` 為 None（尚無任何 scored）時才退回常數 70——那是真正的「零證據」，不是「這個區間零證據」。⚠ 這條裁示指出的是 **W7.1 條文本身**的缺陷，不是實作偏離：`scoring.confidence_ceiling` 忠實實作了 AI-D33 的三步條件 |
| **AI-D40** | 投資邏輯審查 · ETF 旗標的種子 | **不猜、標未知、人工確認**：`is_etf` 改三態（`True`／`False`／**`None`**）。`quick_register` 的預設由 `False` 改為 `None`，自動註冊（`input_center.py:697`）落 `None`；顯式註冊表單仍傳實 bool。費稅縫（`manual.py` / `csv_import.py`）遇 `is_etf is None` **且 TW SELL** 時以 False 計但發**軟性 issue** `etf_flag_unknown`「無法判定是否為 ETF，賣出稅率待確認」（BUY 無稅，不擾民） | owner 裁納 2026-08-24（審查 §1.1）。2026-07-15 壓測讓**註冊表當權威**，卻沒堵住**餵給註冊表的那個 `False`**：TW ETF 首次手動下單即以 現股 0.3% 而非 ETF 0.1% 課稅（11 萬賣單多課 NT$220），且 `tax_rate: 0.003` 被寫進 `fee_rule_snapshot` 當權威。⚠ **審查報告的「Fix: 一個參數」低估了修法**——`input_center.py` 全檔只有兩個 `is_etf` 命中（`:144` 讀取、`:327` 註解「is_etf is NOT taken from the body」），手動下單 body **根本沒有這個欄位**。否決：body 加欄＋前端 checkbox（每次新標的多一次點擊且會忘記）、純 provider 推斷（推斷失敗仍靜默落 False——同一個病）。**既有 0/1 列一律不動**，只有未來的自動註冊落 NULL；附唯讀稽核腳本列出可能已被汙染的 TW 註冊列供 owner 逐一確認——程式絕不回頭改 owner 的資料 |
| **AI-D41** | 投資邏輯審查 · 總報酬的本金匯率 | **並陳，不改既有定義**：`total_return` 的定義**維持不變**，KPI 改標「**資產損益（不含本金匯率）**」；新增「**含匯兌總損益**」＝趨勢的 `total_value − net_invested`（流量逐筆按交易日匯率）；兩者差額＝「**本金匯率效果**」。三者是 **A · B · B−A 一組分解，永不加總**。`docs/accounting-formula-manual.md` 的**不變式 I5「總報酬已內含 FX」修正為只對 XIRR 成立** | owner 裁納 2026-08-24（審查 §2.1）。現況 `returns.py:70` 是 `Σ_ccy (realized+unrealized)_原幣 × 今日匯率`——匯率只乘在**損益**上，抓不到**本金**上的；而 `timeseries.py:110-130` 的 `總市值 − 累計淨投入` 才是真正含匯兌的累計結果，卻被標成「浮動損益」。⚠ **把換匯損益卡「加」到總報酬會重複計算交叉項** `(市值−成本)×(spot−acq)`，違反 invariant 6——B−A 就是那張卡的內容，所以是分解不是加總。否決：直接把 `total_return` 改成 FX-complete（動 golden payload、stress oracle、所有匯出與既有截圖的歷史比對）、只加說明文字（KPI 區仍然沒有任何含匯兌的獲利金額） |
| **AI-D42** | 投資邏輯審查 · 現金帳進報酬 | **三類「交易與融資的成本」進 XIRR**：`REBATE`（退佣——已資本化進成本基礎的佣金之退還）、`INTEREST_EXPENSE`（融資部位的利息）、`BROKER_FEE`。`xirr_reporting` 新增**必填**參數 `cash_movements`，符號取自既有 `cash_kinds.CASH_KIND_TABLE.credit`，每筆按自身日期 FX 換算。**排除** `DEPOSIT`／`WITHDRAW`／`OPENING`（資本移動）**與 `INTEREST`**（閒置現金利息） | owner 裁納 2026-08-24，**知情後修改自己的前一裁示**。⚠ **本條取代 owner 裁示 `D1 = A`（2026-08-13）**，該裁示明訂「現金收支永遠不會進入 XIRR」並就三個新 kind 再次確認（`docs/accounting-formula-manual.md` §4.4.7 限制 2、§7.2）。取代的理由是**手冊自己已把它記為盲點**（「`BROKER_FEE` 也是一筆『真實發生、卻對每一個報酬指標不可見』的金額」），而 D45（2026-08-11）又撤銷了本來要涵蓋它的帳戶層 IRR——AI-D42 補的是手冊已點名的缺口，不是推翻一個好設計。⚠ **D12 的重組費不受影響**：它記為 `WITHDRAW`，在本條之下仍然不進 XIRR，那條常設限制原樣成立。**`INTEREST` 被排除的理由比「資本移動 vs 池內損益」更鋭**：閒置現金的本金從未進入 XIRR 的分母，把它孳生的利息計入分子是不對稱的；進來的三類全部是**交易與融資的成本**，它們對應的本金正是分母裡的那些錢。實測量級：FE-D1 的 77% 退佣（群益 charge-first）單趟差 **0.229% 資本**，而那是 owner 券商的常態計費。⚠ **本條會移動手冊與 stress oracle 中每一個已錨定的 XIRR 數字**，凡帳本存在這三類現金列者皆然——oracle 須先擴充、錨點須重測，然後 `/stress-audit --phase 1` 才有意義 |
| **AI-D43** | 投資邏輯審查 · 基準對照與 AI-D23 的關係 | **AI-D23 不適用於基準對照，`convert_closes` 在此**可以**使用**：AI-D23（W5-2）封存 `convert_closes` 的理由是**計分的雙腿天然同幣別**（AAPL/USD vs ^GSPC/USD），多一層 FX 只是多一個掉落點且結論不變。**基準對照是相反的情況**——「同一筆錢、同樣的日期、放進那個市場的指數」的流量已在**報表幣**（逐筆交易日匯率），而基準以**自身幣別**報價（MYR 出資的美股部位、對美股指數、用 TWD 報告），**FX 是主體不是雜訊**。兩者不衝突 | owner 裁納 2026-08-24（審查「最高價值的一件事」）。⚠ 落成文字的理由：`llm_insight`／`pricing` 兩處都留有「`convert_closes` 因此不用——記錄在此，免得日後有人『修』它」的註解，**不寫這一條，下一個讀者會正確地把基準對照讀成違規**（房規：程式裡有、規則裡沒有的邊，就是下一次的稽核發現）。基準對照是**記錄性數字**（純函式＋既有零件：`BENCHMARKS`／`series_in`／`build_overlay`／換匯池），LLM 只負責敘述 |
| **AI-D44** | 投資邏輯審查 · 事件研究窗對齊計分窗（⑪） | `portfolio/backtest.py::FORWARD_WINDOWS` 由 `(20, 60, 120)` 改為 **`(10, 20, 60, 120)`**，+10 交易日（≈14 日曆日）**領頭**。計分契約（`scoring.py`／`horizon_days`／`ActualMeasurement`）**完全不動** | 缺陷：建議卡引用 `signal_backtest_json` 為**方向性宣稱**背書，而該宣稱是用卡片自己的預測期（官方模板 **14 日曆日**）打分的；證據窗最短是 +20 **交易日**——助手拿一個問題的答案去支持另一個問題。+10 是唯一與計分同span 的窗。長窗全部保留（「然後呢？」是真實且不同的問題）。⚠ **右刪失對窗長單調**，所以對齊窗同時是**樣本最多**的窗——被打分的那個問題，正好是資料最常能回答的問題。⚠ 三份提示詞 body 逐字寫著舊窗集，不改就會讓**指令與 payload 描述不同的東西**，那正是 AI-D2 兩套 `ma_cross` 定義的同一缺陷類：`LIBRARY_VERSION` → `official-v18`，持倉建議 v3.2／個股健檢 v2.8／持倉週報 v2.4／市場週報 v1.2 |
| **AI-D45** | 投資邏輯審查 · 訊號 `as_of` 用資料日、`freshness_json` 涵蓋全部來源（⑬） | ①`/api/signals` 與 `signal_states` 的 `as_of` 改用**最後價格日**（`signal_history` 本來就如此），無價格時誠實 `null`；`evaluated_at` 維持牆上時鐘。掃描**不再有全域 as_of**，逐標的各記自己的最後收盤。②`freshness_json` 新增 `sources` 區塊（每個外部變數一列：`last_as_of`／`age_days`／`unavailable`），由 router **已建好的** payload 推出，零新查詢；`FreshnessReport` 本身不動 | 缺陷：週末或供應商未交付時，抽屜與 `rule_signals_json` 都宣告「資料基準＝今天」，而提示詞守則（「資料基準 {{as_of}} — 在卡首標注基準日」）忠實地把錯的日期抄上卡片。**金色治具本身就是一個實例**：`GOLDEN_NOW` 是 2026-06-11，最新收盤是 2026-06-09，而一條契約測試從寫下那天起就在釘死牆上時鐘的答案。⚠ **本條恢復 W7 第 0 步 (b) 以「死代碼」為由拿掉的 `_read_series` 第三回傳值**。當時它確實是死的；現在它是承重的，docstring 已寫明理由已反轉——否則下一次死代碼清理會再拿掉一次。改回傳具名 dataclass 而非裸元組。`to_wire` 的 `price_as_of` **無預設值**（注入規則：忘記＝mypy 錯誤＋TypeError，不是靜默退回牆上時鐘）。⚠ **刻意不發明每個來源的 stale 判定**：價格有市場行事曆可依據，基本面沒有；替它訂一個「幾天算過期」的門檻正是本專案處處禁止的猜測。只報基準日與天數，判斷留給提示詞 |
| **AI-D38c** | AI-D38 第三步落地 · 信心上限**違規率**（⑫c） | `insights` 加 `ceiling_at_create INTEGER`（可為 NULL，無預設），產卡時從**提示詞渲染的同一份** `backtest_json` 取值；`/api/ai-score` 加頂層 `ceiling_violations {n, violations, rate}`，戰績頁第六張卡。**母體＝所有非 shadow 且同時有 confidence 與已記錄上限的卡片，不限已評分**（owner 裁示 2026-08-26） | AI-D38 的 (a)(b) 修的是上限的算術，(c) 是問責的那一半——**在此之前沒有任何地方記錄模型有沒有遵守**，而沒有量測的規則只是建議（W7.1 首跑 0/13 遵守一條同類規則）。⚠ 欄位必須在**卡片列**而非評估列：評估列是一個 horizon 之後才寫的，而上限來自不斷移動的分桶，事後不可重建；`price_at_create`（M4）是逐點對應的前例。⚠ **沒有記錄上限的卡不算「遵守」而是不在母體**——舊卡與 `backtest_json` 不可用時產出的卡從未受此規則管轄，把它們算成遵守會用規則沒管過的列去美化比率。母體為 0 時 `rate: null`（0% 遵從與「還沒有可比對的卡」是兩件不同的事）。⚠ **AI-D33 的紅線寫成測試**：違規的卡照樣存**模型自陳**的信心。量測遵守不得滑向強制遵守——validator 默默改寫模型自陳的信心，與把兩家 PE 取平均成一個沒人報過的數字是同一類缺陷 |
| **AI-D46** | 投資邏輯審查 · 兩條組合層風險規則（R5） | 新增 `portfolio_drawdown`（整本帳的每日總市值自高點回撤，預設 0.20，warn 在一半）與 `currency_weight`（單一幣別權重，預設 0.70）。皆走既有 alerts-v2 機制：零新表、零新路由、門檻在 `rules_config`。`CombinedView` 新增 `by_currency_reporting` | 缺陷：`drawdown_from_peak` 是**逐標的**比各自 52 週高點，分散的組合（正常情況）可以整體跌兩成而沒有任何一檔觸發；而三幣別投資人最大的未分散賭注常常是幣別配置，`single_weight`／`sector_weight` 看不到那個軸。⚠ **`incomplete` 的趨勢日必須跳過**：持股當天缺報價時 `total_value` 會塌陷，計入就是把資料缺口當成「組合回撤 100%」發風險警示；測試用同一數列跑有／無旗標兩次證明差別確實來自這個跳過。⚠ **命名是實質**：設定頁、推播清單、規則註解若三處都寫「回撤」就是 AI-D2 同一類缺陷——定名「高點回撤」（逐標的）vs「組合整體回撤」（整本帳），且設定頁文案直接寫出差別。⚠ **`by_currency_value` 是原生幣別**，跨幣別排序或相加是沒有意義的算術（10,000 USD vs 300,000 TWD 原生比 31:300，實際各半）；新欄在**同一個迴圈**累加，三個數字不可能互相矛盾。⚠ 兩條都守非正數分母（淨空頭的總值可 ≤ 0，比率會翻號——audit-H1 陷阱）。⚠ 新策略規則必須同時進 `ops/notify.RULE_CATALOG`（有子集漂移守衛），否則是一條沒人能被通知的規則 |
| **AI-D47** | 投資邏輯審查 · 除息日（⑧／R6） | `dividends` 新增 `ex_date TEXT`（**NULLABLE、無預設**），`date` 語意釘死為**發放日**。新 property `Dividend.effective_date`：**只有 STOCK** 用 `ex_date or date`，DRIP 與 CASH/NET 維持 `date`。三個過濾點（`build_book` 排序、`LedgerBundle.through`、`before_action_on`）共用該 property。四個入口全部收 `ex_date` | 缺陷：台股配股的除息日與發放日相隔約一個月，帳本只有一個日期。股價在除息日就調整、股數在發放日才增加 → 重播拿舊股數對已調整股價撐一個月，10% 配股讀成約 −9% 虧損，每年一次。⚠ **既有列一律 NULL，行為逐位元組不變**——回歸釘點是本波最承重的測試，因為這動的是 money of record。⚠ **只有 STOCK 移動**，判準是「那一天你實際擁有什麼」：STOCK 是除息日就附著的權利（股價也這麼說）；DRIP 是發放日**買進**的，那些股票在此之前不存在；CASH/NET 的凹陷是**誠實的**——部位確實變便宜而你確實還沒拿到錢，且除息日你擁有的東西沒變。⚠ **`through()` 才是關鍵**：它用日期過濾 dividends，不改它的話事件在除息~發放期間根本不在 bundle 裡，只改排序等於沒改。⚠ **供應商門本來就握有答案**：`dividend_inbox` 的 `div_date = p.pay_date or p.ex_date` 兩個日期都有卻只留一個——四個 insert 現在都傳 `ex_date`，這是台股配股零操作生效的原因。⚠ **差點造出的缺陷**：`broker/convert.py` 表頭來自 `template_columns()` 而列是手寫的，加欄未補值會輸出 10 標題配 9 值、`reinvest_price` 之後全部錯位且靜默寫進帳本；兩處（另一處是 AI 門 `_div_csv`）皆修，並加寬度斷言讓下一次加欄爆炸而非錯位。券商轉換器自身供給**空值**（對帳單只有發放日），維持其「照抄不重算」紀律 |
| **AI-D48** | 投資邏輯審查 · **B 也收那三類現金帳** | `total_value − net_invested`（**B**，含匯兌總損益）的流量串改為**與 `xirr_reporting` 相同的三類**：`REBATE`／`INTEREST_EXPENSE`／`BROKER_FEE`，符號取自 `cash_kinds.CASH_KIND_TABLE.credit`，每筆按**自身日期**的匯率換算。`INTEREST`（閒置現金利息）與 `DEPOSIT`／`WITHDRAW`／`OPENING`（資本移動）**仍不進**，理由與 AI-D42 逐字相同 | owner 裁納 2026-08-27。AI-D42 只搬動了 XIRR，而 **A 與 B 是並排印在同一條 KPI 帶上的兩個數字**（AI-D41）：一個收下 77% 退佣與融資利息，另一個沒有，於是「差額＝本金匯率效果」這句話從那一刻起就不再成立——差額其實是「本金匯率效果 ＋ 三類現金帳」。R4 讓它更嚴重：基準對照的**超額報酬以 B 為基準**，所以一筆沒被 B 看見的券商費會被算成「贏過大盤」。⚠ 這會移動 owner 已經看過的 KPI 數字，凡帳本存在這三類現金列者皆然。⚠ **`build_reporting_flows` 是 B 與 R4 反事實的共用定義**（timeseries.py 的 docstring 已寫明「這是『什麼算投入的錢』的唯一定義」），所以這一條同時決定了兩個數字——因此需要 AI-D49 一併裁示反事實那一腿怎麼辦。否決：只收 `REBATE`（利息與券商費仍是黑洞）、四類含 `INTEREST`（B 與 XIRR 從此各有一套納入規則，正是 AI-D2 重名缺陷類） |
| **AI-D49** | 投資邏輯審查 · 基準對照如何對待那三類成本（R4 × AI-D48） | **成本只記在組合這一腿**：現金帳流量進 B，但**不買指數**——`ReportingFlow` 只在有 `market` 時才可下單，現金帳沒有市場。反事實的 `net_invested` 因此是**證券流量**的累計，而超額報酬 `B − 基準報酬` 於是把交易與融資成本算在組合頭上。卡片必須明說這一點 | owner 裁納 2026-08-27。與「同一筆錢、同樣的日期，買指數會是多少」的字面一致：被動投資人不會替你付融資利息與券商月費。否決：**兩腿都扣同樣成本**（比較「乾淨」，但憑空替被動投資人捏造一組他沒付過的費用——與「LLM 不得產生沒有來源支持的數字」同一條紅線）、**反事實維持純證券流並改與 B′ 比**（最無爭議，但站上會出現兩個長得極像的「含匯兌總損益」，AI-D2 重名缺陷類）。⚠ 實作上這不是一個 if：`build_reporting_flows` 回傳的每一筆流量都帶 `market`，現金帳沒有——所以型別上就必須表態，`counterfactual()` 不得把它們算進 `uncovered_ratio`（那個比率的語意是「有多少錢找不到基準」，不是「有多少錢本來就不該買指數」） |
| **AI-D50** | 投資邏輯審查 · 稅務包的落點與年度來源 | 稅務包按鈕落在**帳本頁**匯出排（`#ledger-export-slot`），年度為下拉；年度清單**依帳本最早一筆推出**（`GET /api/db-stats` 的 `oldest`），預設**去年**。同一份推導由 `web/export.js::fillTaxYearSelect` 單一定義，設定頁 匯出中心 共用 | owner 裁納 2026-08-27。否決：儀表板已實現損益面板（無年度概念，要另長一個選擇器）、設定頁匯出區（那一區是**系統記錄** llm-usage／job-runs，稅務包歸在那裡等於埋掉）。⚠ **裁示當下我給 owner 的前提是不完整的**：我說「稅務包是唯一沒有前端按鈕的匯出端點」。實查後是——設定頁 匯出中心 **有** 一張「年度報稅包」卡，帶 ⬇ 圖示與「產生並下載」按鈕，而那顆按鈕的 handler 只 `toast('已排入產生佇列', 'ok')` 然後**什麼都不做**（沒有佇列）。五張卡片全是這樣，五個後端端點卻早就存在。**一顆回報成功卻什麼都沒做的按鈕比沒有按鈕更糟**，而且它對「有沒有掛 listener」式的接線稽核完全隱形（v0.1.26 掃過 2,287 個控制項、0 dead）。五張卡片同批接真；年度下拉原本寫死 `['2026','2025','2024']`，一併改成推導 |
| **AI-D51** | 投資邏輯審查 · 壓測的**時點**斷言族 | `oracle.py` 新增 `facts_through` / `replay_through` / `net_invested_through`；`phase1.py` 新增 `trend.*` 族，在**事件日與其前一日**（股利貢獻 `effective` 與 `d` 兩個日期）＋`ASOF`＋期末取樣，比對 `trend.start`／`trend.contiguous_days`／`trend.net_invested`（精確、與價格無關）／`trend.incomplete`／`trend.total_value`（僅在 oracle 判定當日可估值時） | owner 裁納 2026-08-27。⚠ **這一族的第一個職責是守住 AI-D48**：`net_invested` 就是 B 的減項，所以 app 與 oracle 必須一起搬，中間任何一邊先動，這一族就會紅。⚠ **仍然沒有比對「某一天的逐標的股數」**，而那正是 R6 除息日的落點——因為**沒有任何 app 介面回答那個問題**：`trend.points` 只帶四個組合層數字，而賣出預覽的日期感知在 賣超 守衛裡、不在報表裡。要補齊需要情境攜帶**每日**價格序列（`total_value` 才會在全期可斷言），而那不是一行：把本情境的**行動後**價格往前補日期，會餵給讀取側一個錯誤的分割基準。此限制寫在 README 而不是在這裡近似掉 |
| **AI-D52** | 全站互動掃描（2026-08-27）· 修復範圍 | ★ **17 條發現全修，逐條反證先行**；**F-18 複查後判定不成立、未動**。兩處需要選項的地方都採建議案：**F-02 取 A**（維持 `allow_oversell=False` 的嚴格性，把阻擋原因說出來並縮小敘述範圍；否決 B「以 degraded book 只擋被查詢的那一檔」——賣超部位沒有誠實終值，權重分母會被污染）、**F-06 取 A**（種子走顯示精度，讓欄位與內部狀態同源，合計誠實顯示 100.1% 並觸發既有的超過-100% 警告；否決 B「多顯示一位小數」——四捨五入永遠可能差一點，那是治標） | owner 裁納 2026-08-27（「以上按照建議進行全修復」）。⚠ **F-01 是 2026-08-24 那條規則自己的釘樁失效**：該規則要求「預覽必須逐字鏡射重播的分支」，而它指定的釘樁寫成「pin `preview.realized` ≡ 落帳的已實現列」——於是旁邊的均價欄又多活了三天，繼續對三個分支印同一個交易前數字。**指名單一欄位的釘樁只保證那一個欄位**。`.claude/rules/domain-ledger.md` 該條已擴寫為「每一個投影欄位」，並補上兩條：**投影 totals、讀時才除**（重播搬的是 totals，一步投影出來的均價會在末位漂移），以及**無法判定分支的介面要說出分岔、不要挑一個**（抽屜沒有放空勾選可讀，因此均價與已實現一樣不給數字） |
| **AI-D53**〔記錄〕 | 全站互動掃描 · 「靜默控制項」這個類別 | 三條發現同屬一類，且**都對「數 listener」式的接線稽核隱形**（v0.1.26：2,287 個控制項、0 dead）：匯出中心五顆按鈕跳成功 toast 卻不下載（AI-D50）、CSV 勾選框沒有任何 listener 而按鈕標籤宣告「寫入勾選列」、`window.pdAfterLedgerChange` 全庫無定義而 `if (window.x)` 讓錯名字變成安靜的 no-op。**判定標準因此改為「按下去之後有沒有可觀察的效果」**：一個 `/api/*` 請求、一段 DOM 改變、一次導頁、一個真的落到磁碟的檔案。只出現 toast 的一律當異常 | **實作記錄，非 owner 裁示**（2026-08-27，owner 裁定歸屬）。owner 的「全修復」概括承受了**做什麼**，這一列寫的是我**怎麼歸類**，兩者不同，混在同一欄會讓日後回頭讀的人分不清哪些真的問過。三條各留一支**靜態**守衛而非 e2e：`test_web_globals_are_defined.py`（每個 `window.pd*()` 呼叫都要有對應賦值）、`test_export_endpoints_have_callers.py`（既有）、`test_degrade_panel_actions_are_wired.py`（降級卡的動作按鈕必須被某支 JS 引用）。理由：這些縫是**壞掉之後才被發現**的，逐縫寫 e2e 等於永遠慢一步；而靜態守衛在下一次打錯名字的當下就紅 |
| **AI-D54**〔記錄〕 | 全站互動掃描 · 閘門的可信度 | ★ **mypy 的增量快取會在真的有錯誤時回報乾淨的樹**。`9fcc832` 記錄的「665 檔 0 error」是**快取產物**：同一個 commit 清掉 `.mypy_cache` 重跑，會找出一條既存的型別錯誤（三項分解對 `Decimal \| None` 相加）。檔案數也不同（667 而非 665），那是當時沒注意到的可見徵兆。**出貨前的 mypy 閘門一律冷快取跑**，檔案數是結果的一部分 | **實作記錄，非 owner 裁示**（2026-08-27，owner 裁定歸屬）。⚠ 這是本企劃**第二次**把紅的閘門讀成綠的——第一次是數 pytest 結果字元（`F`／`E` 也是結果字元）。兩次形狀相同：一個為真的數字，加上一個由它推出來的不為真的主張；兩次都只有靠刻意重推才發現（一次是 `git stash push`，一次是清快取）。**要回報給 owner 的閘門結果，重推一次，不要從順手的產物讀。** 兩條都在 `LESSONS_LEARNED.md` |
| **AI-D55** | 二代健保補充保費 · 永久排除 | ★ **完全放棄，全數抽離，不留任何考量。** 二代健保**本來就沒有實作**（`dividend_model.py` 明文：owner decision 2026-07-26 out of scope；`FEE_RULES` 與股利模型裡沒有稅率、門檻或欄位），所以本裁示改變的是**狀態**而非行為：從「暫停，日後可能回來」變成「排除，不會回來」。動作＝把這個名詞從**活的程式碼與設計文件**移除，理由改以一般性事實表述（「本 app 不模型化的扣繳」），保住那段解釋的功能——它是股利守門**不**強制 `gross − withholding == net` 的理由，而那條錯誤規則曾被提案過一次（`LESSONS_LEARNED.md`） | owner 裁示 2026-08-27。⚠ **有日期的歷史紀錄不改**：`CHANGELOG.md` 的既有條目、`LESSONS_LEARNED.md` 的教訓、`docs/audit/*.html` 的 2026-07 報告都是「當時真的這樣決定過」的紀錄，改寫等於竄改歷史（同 VM 操作日誌的紀律：更正是新條目，不是重寫）。要一併清掉請明講，那是另一個決定 |
| **AI-D56** | 三項功能 · 永久排除（不是延後） | ★ **組合層 `relative`／`volatility` 計分**（需「混合基準怎麼定」的裁示，三市場的書沒有單一基準）與 **`_ratio_str`／`_avg_str` 的 HALF_EVEN 對齊**（同一張戰績表面上 `calibration_bins` 已對齊 HALF_UP，這兩支還是 HALF_EVEN）**永久排除**，從待辦清單移除。⚠ 這與「延後」不同：延後會在下一輪重新被提出來問，排除不會 | owner 裁示 2026-08-27（「其他沒選的全數抽離不考慮這些功能」）。組合層計分維持 `insight_service.py` 現況（symbol=None 只有 `price_change` 可測，AI-D35 不變）；進位不一致維持現況並在此記錄為**已接受**，不再標為「已亮明未修」 |
| **AI-D57**〔記錄〕 | 出貨 · 繼續留在分支 | owner 2026-08-27 裁定**暫不出貨**：不 merge、不 tag、不部署。⚠ 分支已領先 `main` 與 `v0.1.28` **76 個 commit**（親驗 `git rev-list --count v0.1.28..HEAD`），承載公司行動、AI 助手 W1–W7、投資邏輯審查 R0–R7 與全站互動掃描的全部成果。先前對話中我把這個數字講成 13／14／15——那只是本子計畫自己的 commit 數，不是分支的分歧量，已更正 | **實作記錄，非裁示內容的一部分**：裁示本身是「暫不出貨」，此列附帶記錄的是規模，因為它改變了日後一次性部署的風險量級 |
| **AI-D58** | W8 · OHLC 的 `*_raw` 欄位 | ★ **`open`／`high`／`low` 比照 `close`，各自擁有 `*_raw`，儲存值＝`cap_4dp(raw × split_basis)`。取代 D39b。** D39b 的反對理由正是「沒有自己的 raw，套上因子就再也無法重述或還原」，而它自己寫了逃生口：「or add its own raw columns」——W8 走的就是那個口。在此之前，同一列可以帶著**分割後的 close 與分割前的 high/low**；今天無害純粹因為全庫沒有讀者（2026-08-27 再驗一次：唯一提到這三欄的 SQL 就是 INSERT 本身），但第一根 K 線就會把兩種基準畫在一起。⚠ **`volume` 仍然永不相乘**，而且理由比舊的 OHLC 更強：它是**數量不是價格**，20 併 1 會讓股數變多、價格變少，套上價格因子是**錯的**，不只是無法還原 | owner 裁示 2026-08-27。⚠ 遷移把既有列的 `*_raw` 由自身值回填（基準本來就是 1），而不是留 NULL 給日後的 reconcile 去猜——與 `close_raw` 的回填同紀律。⚠ DDL 裡三個新欄位排在 `split_basis` **之後**，讀起來怪，但是必要的：`ALTER TABLE` 只能附加，而 `test_fresh_and_migrated_schemas_agree_column_for_column` 要求全新建表與遷移後的欄位順序逐欄一致 |
| **AI-D59** | 壓測 · 過去日期的逐標的股數 | ★ **補上每日「如實成交價」序列，讓 `trend.total_value` 在全期可斷言——在已知價格下，總市值就把股數釘死。** 阻礙從來不是斷言而是治具：`seed_all` 每個標的只種一筆 ASOF 收盤，所以 ASOF 之前每一天都無價、每一天都`incomplete`、`trend.total_value` 正好在缺陷所在之處被跳過。兩條性質讓治具誠實而不只是變綠：**(1)** 收盤價是**當日如實成交價**（`oracle.price_as_traded` 把 ASOF 報價依區間內的分割逐一乘回去，除法最後做，絕不用商數連乘＝trap #2a），符合 `data-and-pricing.md` 對該欄位的不變式；**(2)** 每列 `fetched_at` ＝**自身日期**，寫入窗 `(as_of, fetched_at]` 與讀取窗 `(priced_on, day]` 皆為空，所以 `upsert_prices` 與 `reconcile_prices` 都不會重述這些列——日後插入或刪除 SPLIT 都無法重漆這一族賴以斷言的歷史 | owner 裁示 2026-08-27，實作 2026-08-28。★ **偵測力是量出來的，不是宣稱的**：把 R6 缺陷刻意重新植入 oracle（`facts_through` 改用發放日切，即 R6 修正前的行為）——**舊版壓測 `pass=5663 fail=0`，完全靜默**（且恰好重現先前記錄的基準數字，證明探針忠實還原了舊狀態）；**新版 `fail=53`，全部是 `trend.total_value`，全部落在 2026-05-02…05-29（除息→發放窗），每日差 70,000 TWD＝該期間應存在的配股股數價值**。新基準 `ops=128 pass=6075 fail=0`；`ops` 未動且必須維持128——這是用治具資料與斷言補上的缺口，不是新路由。⚠ 仍屬**複核而非對帳**的部分：配股附著於除息日、現金股利不附著，這條規則兩邊都是對 `domain-ledger.md` 的解讀，兩個獨立實作犯同一個誤讀仍會一致——那一類只有 `tests/portfolio/test_review_r6_ex_date.py` 的手算逐日檢查抓得到 |

> **〔記錄〕標記**（owner 裁定 2026-08-27）：帶此標記的列**不是 owner 的裁示**，是實作過程的歸類或教訓，編號沿用同一序列只為可引用。owner 的概括同意（例如「按照建議進行全修復」）承受的是**做什麼**，不等於他逐條決定了**怎麼歸類**；兩者混在同一欄，日後回頭讀就分不清哪些真的問過。
>
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
| **W4** | AI 門判別式聯集擴充（交易＋股利＋資金）＋ 準確度語料與閘門 | AI-D3 · AI-D17–D21 | M | ✅ 本波提交 |
| **W5** | 計分板補洞：基準重放（`relative`）＋ 實現波動（`volatility`） | AI-D7 · AI-D22–D26 | M | ✅ 本波提交 |
| **W6** | 訊號歷史（時序表）＋ 事件研究回測 → 點亮 `backtest_json`／`calibration_gap_json`（＋新 `signal_backtest_json`） | AI-D2 · AI-D27–D32 | L | ✅ 本波提交 |
| **W7** | 助手完全體：組合層、引用回測數字、戰績頁升級為決策品質儀表 | AI-D2 · AI-D33–D37 | M | ✅ 本波提交 |
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
| W7.1 | `llm_insight/scoring.py`（`confidence_ceiling`）· `api/routers/prompts.py`（`units`／`reading`／ceiling 注入）· `llm_insight/official_templates.py`（advice v3.1／checkup v2.7／weekly v2.3） |
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
- **W7**：信賴分級門檻由 `scoring.trust_tier` 的決策表測試釘死（n=7 完美也「樣本不足」、
  0.6/10pp 邊界含本數、calib 未知→早期、narrative-only 不吃假 0%）；TWR 組合計分附
  **反證**（窗中入金加倍持倉、價格不動 → 量到 0，naive 值會假造 +100%）；
  `calibration_bins` 的 HALF_UP 對齊附 70.125→"70.13"（HALF_EVEN 會是 "70.12"）反證治具；
  replace 模式附 400/404/409/封存四路與「綁定任務原地升版」斷言。
- **W7.1**：`confidence_ceiling` 由純函式決策表釘死（自洽上限 39、負缺口逐點下修、**0 不設樓地板的反證**、無歷史＝70、n<8 不錨定、良好校準給 77、壞標籤不炸）；producer 側釘 `units` 三鍵與白話 `reading` 的方向字（含「不得出現『低估』」的反面斷言）；模板側釘單位工作範例（0.1336／+13.36%）、「不得改用同窗 baseline」、「輸入裡沒有的數字一律不得出現」、`confidence_ceiling` 與「為 0 時」的處置。
- **demo 首跑實測（2026-08-23 @f1efd56，這波修的來源）**：六個波一次驗，部署／遷移／站上 targeted 子集（423 passed）／`verify_live` 全綠，四本帳分毫未動；W6 首次回填 18,521 列／1,370s，立即重掃 0 列／10.8s（冪等），SPLIT 插刪循環兩表失效→重建**零幻影事件**、1,226 列價格逐列回到 `close == cap_4dp(close_raw)`；事件研究基線與獨立 oracle **12/12 相符**；W3 聯集在 2330 同時帶 yfinance 與 finmind 兩塊 PE（不平均）；W4 三區段 4／1／3 且 `daytrade=1`／`short_sale=1` 就位。**未覆蓋（誠實列出）**：W5 的 relative／volatility 兩臂與 W7-3 組合層 TWR 臂（demo 的卡全是個股層 `price_change`）、pending/undetermined 狀態章、409-封存那一路（無歷史的策略是硬刪不是封存，密封測試涵蓋）。
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
| **二代健保補充保費** | **AI-D55**：owner 2026-08-27 完全放棄。本來就沒有實作；此列把狀態從「暫停」釘成「排除」，並把名詞從活的程式碼與設計文件移除 |
| **組合層 `relative`／`volatility` 計分** | **AI-D56**：需「混合基準怎麼定」的裁示，owner 選擇不開。組合層維持只有 `price_change` 可測（AI-D35） |
| **`_ratio_str`／`_avg_str` 進位對齊** | **AI-D56**：與同表面的 `calibration_bins`（HALF_UP）不一致，owner 選擇接受現況；改動會移動 `miss_rate` 等更多顯示值的末位 |
