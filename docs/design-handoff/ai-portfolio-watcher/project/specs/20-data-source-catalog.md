# Spec 20 — 資料源目錄、Provider 擴充與外部快照 ingest（P1）

> **本規格吸收原 spec 06b**（external_snapshots ingest）。它是「未來新增 / 延伸任何
> 資料源」的唯一參考點：完整來源目錄、provider 擴充契約、外部快照入庫、以及每個資料
> 變數對應到哪個來源。
>
> **與 spec 14 的分工（control plane vs data plane）：**
> - **spec 14 = 控制面**：管「有哪些源、token、啟用旗標、每帳戶 fallback 順位、健康
>   狀態、連線測試」——設定面板（`settings-datasources`）。它不抓資料。
> - **spec 20 = 資料面**：照 spec 14 的設定，把資料「抓進來、入庫、衍生成變數」。
> - **共用接口（已集中於 `pricing/`）**：`pricing/providers/*` + `pricing/registry.py`。
>   兩個面都走這一層；**新增一個來源 = 一個 adapter + 一筆目錄 + 一個 probe adapter**。
>
> 原則（承 `rules/data-and-pricing.md` / `rules/architecture.md`）：
> - **報價/FX/股利數字是 numbers of record**，走 `pricing/` registry，存 `prices`/`fx_rates`/
>   `dividends`；**LLM 永不供數**。
> - **籌碼/基本面/情緒/指數**是 LLM 的決策輔助訊號，**原始回應 append-only 快照入庫**
>   （`external_snapshots`），衍生值由**純函式計算**（計算核心，非 `llm_insight`）。
> - **Decimal end-to-end**；JSON float 一律 `Decimal(str(x))`，存來源精度，顯示時 quantize。
> - 失敗**優雅降級**：快照缺漏 → 變數回 `{"unavailable": true, "last_as_of": …}`；
>   報價缺漏 → 服務最後已知值 + staleness 標記；**永不捏造、永不崩壞儀表板**。

---

## 20.0 範圍與本輪建置策略

本規格涵蓋全部 ~15 個資料源；但**建置分兩波**（使用者拍板 2026-06-14）：

- **本輪建置（免 token + 關鍵鏈所需）**：FinMind 多資料集（Free 層，token 已有
  600/hr）、情緒源（VIX/CNN F&G，免費）、指數（yfinance，免費）、免費報價後備
  providers（twstock / stockprices.dev / klsescreener / Malaysiastock.biz）、
  `external_snapshots` + 衍生 + 翻轉 chips/sentiment 變數 available、probe 驗證預測試。
- **待測試（token-gated，本輪只寫 adapter + 目錄登錄，標 `pending`）**：Alpha Vantage、
  Finnhub、FRED、Schwab。未來在前端面板輸入 token 存 DB 後，**回 Claude Code 跑 probe
  全面驗證**——此工作流不動前端程式碼、零複驗。
- **受阻（catalogue only）**：bursamalaysia.com（Cloudflare 403 JS challenge，需 headless）。

---

## 20.1 完整資料源目錄（market × data-type × auth × rate × status）

> 目錄為**靜態 config-as-code**（`pricing/datasources_store.SOURCE_INFO`，不落 DB）。
> 一個來源可供多種資料型別 → `SourceInfo` 新增 `provides: list[str]`（資料型別清單），
> 既有 `type` 仍為前端面板的主分組鍵。`status` 一欄：`live`（已實作已驗證）/
> `pending`（已實作待 token 驗證）/ `blocked`（受阻）。

| id | 來源 | markets | provides | auth | rate | status |
|---|---|---|---|---|---|---|
| `yfinance` | Yahoo Finance | US/TW/MY/FX | quote_latest, quote_history, dividend, fx, index, sentiment(VIX) | none | 寬鬆（批次） | **live** |
| `twse` | 台灣證交所 | TW | quote_latest | none | 免費 | **live** |
| `tpex` | 櫃買中心 | TW | quote_latest | none | 免費 | **live** |
| `finmind` | FinMind | TW/US/FX | dividend, quote_history, institutional, margin, valuation, monthly_revenue, financials, news, macro | apikey | 600/hr | **live**（股利已驗證；本輪擴充多資料集） |
| `twstock` | twstock | TW | quote_latest（盤中即時） | none | 免費 | **live**（本輪新增） |
| `stockprices_dev` | stockprices.dev | US | quote_latest | none | 無上限（但 flaky） | **live**（本輪新增，後備限定） |
| `klsescreener` | KLSE Screener | MY | quote_latest（3-dp string） | none | 免費（~3.5s/檔） | **live**（本輪新增，校驗/補洞） |
| `malaysiastock` | Malaysiastock.biz | MY | quote_latest（3-dp string） | none | 免費 | **live**（本輪新增，次要 string 源） |
| `cnn_fng` | CNN Fear & Greed | ALL(US) | sentiment | none | 免費 | **live**（本輪新增） |
| `alphavantage` | Alpha Vantage | US/FX | quote_latest, quote_history, fx | apikey | 25/day（免費層） | **pending** |
| `finnhub` | Finnhub | US | quote_latest, dividend | apikey | 60/min | **pending** |
| `fred` | FRED（fredapi） | ALL | macro | apikey | 免費（需 key） | **pending** |
| `schwab` | Charles Schwab API | US | quote_latest, quote_history, dividend, positions | oauth | 待申請 | **pending** |
| `pytrends` | Google Trends | ALL | trends | none | 免費（非官方，易限流） | **pending**（敘事用） |
| `bursa` | Bursa Malaysia 官網 | MY | quote_latest | none | — | **blocked**（Cloudflare） |

> 補充候選（catalogue only，未列入面板）：i3investor（僅 2-dp，敘事用）、
> marketstack / eodhd / twelvedata（需 key，`.KL`/`:XKLS` 覆蓋率未驗）。

---

## 20.2 資料變數 ↔ 來源對照（餵 spec 04 提示詞 / 大師校正模型）

> 大師校正模型可調用**全部變數**；下表是每個外部變數的資料來源與快照 dataset。
> position/price/dividend/fx/system 類（17 個）已由 spec 01/05/06a 具備，不在此表。

| 變數 token | 類別 | 來源 / dataset | 本輪 |
|---|---|---|---|
| `institutional_json` | chips | finmind `TaiwanStockInstitutionalInvestorsBuySell` | ✅ |
| `margin_json` | chips | finmind `TaiwanStockMarginPurchaseShortSale` | ✅ |
| `valuation_json` | chips | finmind `TaiwanStockPER`（PER/PBR/殖利率） | ✅ |
| `monthly_revenue_json` | chips | finmind `TaiwanStockMonthRevenue` | ✅ |
| `financials_json` | chips | finmind `TaiwanStockFinancialStatements` | ✅ |
| `market_sentiment_json` | sentiment | yfinance `^VIX` + cnn_fng | ✅ |
| `index_quotes_json` | sentiment | yfinance `^TWII`/`^GSPC`/`^KLSE`（TAIEX/SPX/KLCI） | ✅ |
| `backtest_json` | ai | spec 04 `insight_evaluations` 匯總 | spec 04 |
| `calibration_gap_json` | ai | spec 04 校正缺口 | spec 04 |

本輪完成後變數可用度：**17（既有）+ 7（本輪 chips/sentiment）= 24 live；2 → spec 04**。

---

## 20.3 Provider / ingest 擴充契約（集中接口）

兩條既有 seam，新增來源只擴充其一：

**A. 報價/FX/股利（numbers of record，走 registry）**
- 新增 `pricing/providers/<id>_provider.py`，繼承 `ProviderBase`，實作支援的子集
  （`fetch_quote_latest` / `fetch_quote_history` / `fetch_fx` / `fetch_dividends`），
  回傳正規化 row（`Decimal(str(x))` + `source` 標記）。
- 在 `pricing/defaults.DEFAULT_PROVIDER_ORDER` 排序（順位）、`default_registry` 註冊
  （keyed provider 注入 `token_getter=lambda: datasources_store.get_api_key(conn, "<id>")`）。
- 在 `datasources_store.SOURCE_INFO` 加一筆目錄。

**B. 外部快照（chips/sentiment/macro/index，append-only 入庫）**
- 快照來源**不走 registry**（非 PriceRow/DividendEvent 形狀，無 fallback 需求；單源原始
  JSON）。改用輕量 client：`pricing/finmind_datasets.py`（FinMind `dataset` 參數化呼叫）、
  `pricing/sentiment_source.py`（VIX/CNN）、`pricing/index_source.py`（yfinance 指數）。
- ingest 函式寫 `external_snapshots`（20.4）；排程 job 在 `scheduler/jobs.py` 觸發
  （`scheduler` 不含業務邏輯，且**不得 import data_ingestion**——承 spec 15）。
- 衍生純函式（20.5）由 API router 在 render context 組裝時呼叫（承 06a 分層：
  `llm_insight` 不得 import `pricing`/`data_ingestion`，帶 conn 的讀取在 router 完成
  後餵入 `VarContext`）。

---

## 20.4 `external_snapshots` 表（承原 06b）

```sql
CREATE TABLE IF NOT EXISTS external_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,        -- 'finmind' | 'sentiment' | 'index'
  dataset TEXT NOT NULL,       -- 'institutional'|'margin'|'valuation'|'monthly_revenue'|
                               -- 'financials'|'vix'|'fng'|'index_quotes'
  symbol TEXT,                 -- 標的級資料才有（情緒/指數為 NULL）
  as_of TEXT NOT NULL,         -- 該筆資料的歸屬日（ISO date）
  payload TEXT NOT NULL,       -- 原始回應 JSON 字串（append-only，回測重現用）
  fetched_at TEXT NOT NULL     -- 抓取時刻（ISO datetime, Asia/Taipei）
);
CREATE INDEX IF NOT EXISTS ix_external_snapshots_lookup
  ON external_snapshots (source, dataset, symbol, as_of);
```

- **Append-only**：同 (source,dataset,symbol,as_of) 可有多筆（重抓不刪舊；衍生取最新
  `fetched_at`）。回測可重現「當時看到的輸入」。
- 表住 `pricing/snapshots_store.py`（pricing 是「抓取資料」的家）。讀寫皆 idempotent-safe。

| job | 週期 | 內容 | 來源 |
|---|---|---|---|
| `finmind_chips_daily` | 交易日收盤後 | 持倉+觀察清單台股的法人買賣超、融資券 | finmind |
| `finmind_valuation_daily` | 交易日 | PER/PBR/殖利率 | finmind |
| `finmind_fundamentals_monthly` | 每月 12 日 | 月營收；季報季加抓 financials | finmind |
| `sentiment_daily` | 每日 | VIX（^VIX）、Fear & Greed（CNN） | yfinance / cnn_fng |
| `index_quotes_daily` | 交易日 | TAIEX/SPX/KLCI 收盤 | yfinance |

---

## 20.5 衍生純函式（計算核心）

純 Decimal 函式（`portfolio/external_signals.py`，與 06a `portfolio/technicals.py` 同級；
固定 fixture 單元測試）。由近 N 日快照算出變數值——**LLM 不自行計算**：

- `consecutive_buy_days(daily_net: list[Decimal]) -> int`（法人連買/連賣天數）
- `net_buy_sum(daily_net, days) -> Decimal`（近 N 日法人淨買超合計）
- `chg_pct(curr, prev) -> Decimal | None`（融資餘額 N 日變化率；分母≤0 → None）
- `yoy(curr, year_ago) -> Decimal | None` / `mom(curr, last_month) -> Decimal | None`（月營收）
- `percentile(value, history: list[Decimal]) -> Decimal | None`（PER/PBR 歷史分位）
- `vix_zone(vix: Decimal) -> str`（"low"/"normal"/"elevated"/"high" 分區）

衍生值缺資料（快照不存在）→ 對應變數回 `{"unavailable": true, "last_as_of": null}`。

---

## 20.6 FinMind 多資料集（Free 層，token 已有）

`pricing/finmind_datasets.py`：`GET /api/v4/data?dataset=<DS>&data_id=<ID>&start_date=<D>[&end_date=<D>]`，
**認證走 `Authorization: Bearer {token}` header**（§20.15.1；token 經
`datasources_store.get_api_key(conn, "finmind")`，DB-backed，承 spec 14.2）。

> **Free 層計數澄清（審計 2026-06-14）**：FinMind 行銷的「Free 44 資料集」是方案總數；
> 以 `llms-full.txt` 逐 dataset `Tier:` 實算，**帶 `data_id` 可免費取得約 52 個**（32 個
> 無條件 Free + 20 個「Free with data_id / 全市場才需付費」）。本 app **一律帶 data_id**，
> 故這 ~52 個皆在免費範圍。真正付費才有的是 **即時(Sponsor)/逐筆·分K(Sponsor Pro)/
> 恐懼貪婪指數·景氣對策信號·產業鏈·市值比重(Backer+)**——本輪皆不依賴
> （F&G 改走免費 CNN 直連，見 20.7；**勿向 FinMind 付費購買 F&G**）。

本輪接線 5 個（皆 Free-with-data_id，欄位名已對照 `llms-full.txt` 驗證）：

| dataset | 變數 | key fields（llms-full 驗證） |
|---|---|---|
| `TaiwanStockInstitutionalInvestorsBuySell` | institutional | buy, sell, name（`Foreign_Investor`/`Investment_Trust`/`Dealer_self`…） |
| `TaiwanStockMarginPurchaseShortSale` | margin | MarginPurchaseTodayBalance, ShortSaleTodayBalance |
| `TaiwanStockPER` | valuation | PER, PBR, dividend_yield（無條件 Free） |
| `TaiwanStockMonthRevenue` | monthly_revenue | revenue, revenue_month, revenue_year |
| `TaiwanStockFinancialStatements` | financials | type, value, origin_name（Revenue/GrossProfit/EPS…） |

- 速率 600/hr：jobs 依持倉+觀察清單**分批 + backoff**；以日期範圍批次抓，快取入庫。
- **未來可低成本擴充的免費 dataset**（皆 Free 或 Free-with-data_id，依本契約加 client +
  catalogue 即可）：`TaiwanStockPriceAdj`（還原股價）、`TaiwanStockDividend`/
  `TaiwanStockDividendResult`（股利政策/除權息結果→除息日曆）、`TaiwanStockShareholding`
  （外資持股）、`TaiwanStockNews`（相關新聞→llm_insight 敘事）、`TaiwanStockTotalReturnIndex`
  （報酬指數）、`USStockPrice`（美股日線備援）、`TaiwanExchangeRate`（19 幣 FX）、
  `InterestRate`（12 國央行利率）、`GovernmentBondsYield`（美債殖利率）、`GoldPrice`/
  `CrudeOilPrices`（商品）。**本輪 catalogue only 不接線**，未來資訊面板需要時擴充。

---

## 20.7 情緒 / 指數源（免費）

- **VIX**：yfinance `^VIX` 最新收盤 → `external_snapshots(source='sentiment',dataset='vix')`。
- **CNN Fear & Greed**：`https://production.dataviz.cnn.io/index/fearandgreed/graphdata`
  （免費 JSON，含 score 0–100 + rating）→ `dataset='fng'`。需 UA header；失敗則該日 F&G 缺。
- **指數**：yfinance `^TWII`（TAIEX）/`^GSPC`（S&P500）/`^KLSE`（KLCI）收盤 →
  `source='index',dataset='index_quotes'`。
- `market_sentiment_json` = VIX + 其分區（`vix_zone`）+ F&G score/rating（最新快照）。
- `index_quotes_json` = 三大指數最新收盤 + 漲跌（由快照衍生）。

---

## 20.8 免費報價後備 providers（本輪新增）

| provider | market | 角色 | 備註 |
|---|---|---|---|
| `twstock` | TW | quote_latest 後備（盤中即時） | 免費 lib；放 TW 鏈尾 |
| `stockprices_dev` | US | quote_latest 後備 | flaky（400/429）→ 僅後備、latest-only、無 history |
| `klsescreener` | MY | quote_latest 後備（3-dp string） | ~3.5s/檔；tick 精度校驗/補洞，不做批量 |
| `malaysiastock` | MY | quote_latest 後備（3-dp string） | 次要 string 源 |

- 預設順位（`DEFAULT_PROVIDER_ORDER`）：TW `[twse, tpex, yfinance, twstock]`；
  US `[yfinance, stockprices_dev]`；MY `[yfinance, klsescreener, malaysiastock]`。
- MY float64 精度紀律：yfinance 走 `Decimal(str(x))` + 依市場 tick quantize；
  sub-RM1（tick 0.005）/ETF（0.001）以 string 源（klsescreener/malaysiastock）校驗。

---

## 20.9 待測試（token-gated）sources

本輪**只寫 adapter + 目錄登錄 `status:pending`**，不做線上驗證（無 key）：

- **Alpha Vantage**（`alphavantage`）：US quote/history/FX；免費層約 25/day（須線上確認）。
- **Finnhub**（`finnhub`）：US latest quote / dividend；60/min。
- **FRED**（`fred`，fredapi）：總體經濟序列（macro 變數，未來面板）。
- **Schwab**（`schwab`）：OAuth，**待申請**；可帶部位自動匯入（與 yfinance 不同，是 broker）。

**未來驗證工作流**（不動前端）：使用者在 `settings-datasources` 面板輸入 token →
存 `data_sources.api_key`（spec 14.2）→ 回 Claude Code 跑 `python -m scripts.probe.run_all`
（或該源 `POST /api/datasources/{id}/test`）→ 驗證覆蓋率/精度/速率 → 升 `status:live` +
排入順位。probe adapter 本輪一併備好（`scripts/probe/adapters/`）。

---

## 20.10 敘事源（catalogue only）

- **Google Trends（pytrends）**：關鍵字搜尋熱度，敘事訊號（非數字 of record）；非官方易限流
  → `status:pending`，未來 `llm_insight` 敘事用。
- **FinMind 相關新聞網頁 URL**：TW 個股新聞種子（Free 層）；未來新聞檢索用。

---

## 20.11 probe 驗證預測試（本輪執行）

重用既有 `scripts/probe/`（run_all + report + adapters）。本輪對**所有免 token 源型別**
做驗證預測試並更新 `docs/probes/`：

- 新 adapter：`twstock`（已有）、`stockprices_dev`（us_alt 已有）、`klsescreener`/
  `malaysiastock`（my_src 擴充）、`cnn_fng`、指數（yfinance ^VIX/^TWII/^GSPC/^KLSE）、
  FinMind 五資料集（finmind_src 擴充）。
- 驗證項：覆蓋率（樣本 ticker）、MY 3-dp 忠實度（string 源）、FinMind 五 dataset 取得性
  與欄位、CNN F&G / VIX 可達性與形狀。token 源（alphavantage/finnhub/fred/schwab）標
  `skipped (no key)`。
- 樣本 ticker（使用者提供）：US `TSLA/AAPL/NVDA/IVV/VOO/RIVN/O/BEN/BABA/GOOGL/MSFT/MU/
  SNDK/ARKK/GGR/SE`；TW/TWO `0050/8299/2454/2330/6488/6531/2543/2317/3005/6139/2308/1519`；
  MY `5212/3182/5347/1155/1818`。

---

## 20.12 失敗模式 / 速率 / 降級

- FinMind 斷線/額度耗盡 → 對應 chips 變數標 `unavailable`，洞察照常產生（提示詞已要求
  「資料未提供時明說」）。
- 任一 ingest job 失敗記 `job_runs`（spec 15 既有）；**連續 3 次失敗發 warn 預警**，並
  順手 upsert 該來源 `data_source_health`（spec 14.1，detail = exception 摘要）。
- FinMind 600/hr：依持倉+觀察清單分批 + backoff；以日期範圍批次、快取入庫。
- 外部快照源（VIX/F&G/index）逾時 → 該日該 dataset 缺，衍生回 `unavailable`，不影響其他。

---

## 20.13 前端接線

- `settings-datasources.js` `DATASOURCES_DATA` → 已接 `GET /api/datasources`（spec 14）。
  本輪目錄擴充後，面板**自動多出新來源列**（依 `type` 分組；新增分組鍵 chips/sentiment/
  macro/trends）。`pending` 源顯示「待測試」徽章 + 金鑰輸入框。
- `vars.js` registry → `GET /api/prompt-vars`（06a 既有）：本輪 chips/sentiment 7 變數
  `available` 翻 true，「需後端新增」標記消失。
- 變數值由 `POST /api/prompts/preview`（06a 既有）即時組裝；快照缺 → `unavailable`。

---

## 20.14 本輪建置範圍（明確清單）

**建置 + 驗證（免 token）：**
1. `external_snapshots` 表 + `pricing/snapshots_store.py`（append-only 讀寫）。
2. `pricing/finmind_datasets.py`（5 Free dataset 參數化 client，DB-backed token）。
3. `pricing/sentiment_source.py`（VIX via yfinance ^VIX + CNN F&G）、
   `pricing/index_source.py`（yfinance ^TWII/^GSPC/^KLSE）。
4. 免費報價 providers：`twstock_provider` / `stockprices_dev_provider` /
   `klsescreener_provider` / `malaysiastock_provider` + 排入 `DEFAULT_PROVIDER_ORDER`。
5. `portfolio/external_signals.py`（衍生純函式 20.5）。
6. ingest 函式 + `scheduler/jobs.py` 五 jobs（20.4）+ 連續 3 失敗預警。
7. `datasources_store.SOURCE_INFO` 擴充全目錄（含 `provides` 欄、`status`）；
   API router `POST /test` probe seam 接上免費源 provider probe。
8. `llm_insight/variables.py`：chips/sentiment 7 變數 `available=true` + `value_for`
   讀 `VarContext`（router 餵入快照衍生值），缺 → `{"unavailable": true}`。
9. `api/routers/prompts.py` `_build_context` 擴充：讀快照 + 衍生 + 餵 VarContext。
10. probe 擴充 + `docs/probes/` 更新（20.11）。

**只寫 adapter + 目錄登錄 `pending`（不線上驗證）：**
alphavantage / finnhub / fred / schwab provider 或 client stub + `SOURCE_INFO` 登錄 +
probe adapter（skipped no-key）。

**catalogue only：** bursa（blocked）、pytrends（敘事）、i3investor/marketstack/eodhd/
twelvedata（候選）。

**驗收：** 全 gate 綠（pytest / mypy --strict / ruff）；chips/sentiment 變數在 golden_db
（無快照）回 `unavailable`、有快照回衍生值；CHANGELOG 完整性；token 源在面板顯示「待測試」。

---

## 20.15 FinMind 認證、分層權限與額度（官方手冊 2026-06-14 補強）

> 依 FinMind 官方 AI-Agent 手冊（`docs/data-provider/FinMind/llms.txt` / `llms-full.txt`）
> 補強。涵蓋使用者新需求：**前端面板標記 token 資費等級；變數/資訊面板依等級反灰並標示
> 需要的方案；free token 誤取付費資料時優雅失敗**。

### 20.15.1 認證與請求（改進既有 client）
- **認證改用 `Authorization: Bearer {token}` header**（官方標準；取代 `?token=` query param）。
  套用於 `finmind_provider.py`（股利）與 `finmind_datasets.py`（快照）。
- `/data` 參數新增 **`end_date`** 支援，做有界日期區間抓取（省額度）。
- **保證帶 `data_id`（單檔查詢）**：多個資料集「帶 data_id=Free／不帶 data_id 全市場=
  Backer/Sponsor」。本 app 所有 FinMind ingest **一律逐檔帶 data_id**，恆落 Free 層。

### 20.15.2 分層權限模型（tier-awareness）
- **TIER_ORDER**：`{"free":0, "backer":1, "sponsor":2, "sponsorpro":3}`。
- **token 等級**：`data_sources` 表新增欄 `tier TEXT`（使用者於面板標記；null = 未知，
  視同最低有效層）。`SourceInfo` 新增 `tiers: list[str] | None`（該來源可選等級，供面板
  下拉；FinMind=[free,backer,sponsor,sponsorpro]；alphavantage=[free,premium]；
  auth:"none" 來源 = None）。
- **dataset → 必要等級**：FinMind 專屬 map（`finmind_datasets.DATASET_TIER`），記錄各
  dataset **以本 app 的查詢方式（帶 data_id）** 所需等級。本輪 5 個全為 `free`。未來付費
  dataset（如還原股價=backer、可轉債=backer、即時=sponsor）登錄其等級。
- **變數 → 必要等級**：每個外部變數對應 `(source_id, required_tier)`。本輪 5 個 FinMind
  chips 變數 = `("finmind","free")`；sentiment/index（VIX/CNN/yfinance）= 無等級（恆可用）。

### 20.15.3 API 與前端體驗
- `GET /api/prompt-vars` 每個變數新增：`required_tier`（如 `"free"|"backer"|null`）、
  `tier_ok`（依該來源 token 目前標記等級計算：token_order ≥ required_order；無等級恆 true）、
  `tier_label`（如 `"需要 Backer 方案"`，僅 `tier_ok=false` 時）。
- **前端**：`tier_ok=false` 的變數/資訊面板項目**反灰**，標示 `tier_label`；資訊面板同理
  辨識所需方案。`GET /api/datasources` wire 新增 `tier`（目前標記）+ `tiers`（可選清單）；
  面板付費源顯示等級下拉 + 用量（見 20.15.5）。
- 設定等級：`PUT /api/datasources/{id}/key` body 擴充可選 `tier`，或 `PUT
  /api/datasources/{id}/tier`（擇一；對 `auth:"none"` 來源設等級 → 400）。

### 20.15.4 優雅失敗（誤用付費 / 額度耗盡）
- `finmind_datasets.fetch_dataset` 偵測非成功回應：
  - **HTTP 402 或 JSON `status==402`**（`{'msg':'Requests reach the upper limit...','status':402}`）
    → 拋 `FinMindQuotaError(msg)`。
  - **權限/層級不足**（JSON `status` 非 200 且訊息屬權限類，或 dataset 必要等級 > token
    等級的本地預檢）→ 拋 `FinMindTierError(msg, required_tier)`。**本地預檢**：呼叫前先比對
    `DATASET_TIER[dataset]` 與 token `tier`，不足即不發請求、直接拋 `FinMindTierError`
    （省額度、訊息明確）。
- ingest job 捕捉這兩類錯誤：**不寫快照**，改 upsert `data_source_health`
  （status=`error`，detail = 原因摘要，如「需要 Backer 方案」/「額度已滿」），並記 `job_runs`。
- 變數降級：快照缺漏時回 `{"unavailable": true, "reason": <可選>}`，`reason` 由 router 從該
  (source,dataset) 最新 health detail 餵入（`llm_insight` 不直接讀 health，承 06a 分層）。
  LLM 看得懂、面板可顯示「此資料需 Backer 方案」。

### 20.15.5 額度查詢（面板/測試，選配）
- `finmind_datasets.fetch_quota(conn) -> {"user_count": int, "api_request_limit": int}`：
  `GET https://api.web.finmindtrade.com/v2/user_info`（Bearer）。`POST /api/datasources/
  finmind/test` 可順帶回傳用量 + 由 `api_request_limit` 推斷等級（600=free、1600=backer、
  6000=sponsor、20000=sponsorpro）供面板顯示與標記校驗。額度耗盡（402）亦為合法測試結果。

### 20.15.6 本次增量建置範圍
1. `data_sources.tier` 欄 + 遷移；`SourceInfo.tiers`；`set_tier` / 讀取；`GET /api/datasources`
   wire 出 `tier`/`tiers`；PUT 設定等級（auth:none → 400）。
2. `finmind_datasets.py`：Bearer header；`end_date`；`DATASET_TIER` map + TIER_ORDER；
   本地預檢 + `FinMindQuotaError`/`FinMindTierError`；`fetch_quota`（選配）。
3. `finmind_provider.py`（股利）：同步改 Bearer header。
4. 變數層：`required_tier`/`tier_ok`/`tier_label` 經 `GET /api/prompt-vars` 輸出；degrade
   payload 帶 `reason`（router 由 health 餵入）。
5. ingest：捕捉 tier/quota 錯誤 → health（reason）+ job_runs，不寫快照。
6. 測試：tier 比較、本地預檢拋錯、402 解析、Bearer header 用法、變數 tier_ok 計算、
   degrade reason。**全程 monkeypatch，無網路。**

**不破壞既有**：本輪 5 個 FinMind 變數必要等級皆 `free`，使用者註冊 free token（600/hr）
下 `tier_ok=true`、不反灰；tier 基礎建設主要服務未來付費 dataset 與資訊面板。
