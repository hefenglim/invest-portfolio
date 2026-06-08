# Research: FinMind — capabilities & system fit (validated 2026-06-08)

- **Date:** 2026-06-08
- **Status:** research note + **live validation** (7-day trial token). Tiers: FREE **300
  req/hr**, registered member **600 req/hr**; **44 datasets** on the free/registered tier.
- **API:** `GET https://api.finmindtrade.com/api/v4/data?dataset=<DS>&data_id=<ID>&start_date=<D>&token=<T>`.
  Token-based; usage visible on the FinMind dashboard. Permanent token will live in the
  settings UX / `.env` (gitignored) later.
- **Docs:** <https://finmind.github.io/>

## 1. Datasets validated live (fixtures recorded under `tests/pricing/fixtures/finmind/`)

| Dataset | Rows | Key fields | Use |
| --- | --- | --- | --- |
| `TaiwanStockPrice` | 586 | date, open/max/min/**close**, Trading_Volume, spread | TW quote + history (`pricing/`) |
| `TaiwanStockDividend` | 33 | **CashEarningsDistribution**, StockEarningsDistribution, **CashExDividendTradingDate**, **CashDividendPaymentDate**, year | TW dividend ledger + **ex-dividend calendar** |
| `TaiwanExchangeRate` | 494 | currency, cash_buy/sell, **spot_buy/spot_sell** | FX (USD/TWD…) — *bank* rates, not interbank mid |
| `TaiwanStockFinancialStatements` | 221 | type, value, origin_name (long form: EPS, revenue, margins…) | 財報 → `llm_insight/` |
| `TaiwanStockInstitutionalInvestorsBuySell` | 1345 | buy, sell, name (法人別) | 法人買賣超 flow/sentiment → `llm_insight/` |
| `TaiwanStockMarginPurchaseShortSale` | 268 | MarginPurchase*, ShortSale* balances | 融資融券 leverage/sentiment → `llm_insight/` |

Confirmed: `TaiwanStockPrice` 2330 close = **2295.0** (matches TWSE), so FinMind agrees with
the government source. Values arrive as JSON numbers (floats) → same discipline as everywhere:
parse via `Decimal(str(x))`, quantize at display.

## 2. Full free-tier dataset catalogue (44 datasets; FREE 300/hr · registered 600/hr)

Authoritative list from the user's FinMind account (free/registered member tier):

- **技術面 (technical):** 台股總覽 · 台股總覽(含權證) · 台灣股價資料表 · 台股交易日 ·
  台灣類股股價表 · **個股 PER/PBR** · 每 5 秒委託成交統計 · 台股加權指數 ·
  當日沖銷標的及成交量值 · 加權/櫃買報酬指數.
- **籌碼面 (chip):** 個股融資融券 · 整體市場融資融券 · **個股三大法人買賣** ·
  整體三大法人買賣 · **外資持股** · 借券成交明細 · 暫停融券賣出(融券回補日) ·
  信用額度總量管制餘額 · 證券商資訊.
- **基本面 (fundamental):** 現金流量表 · 綜合損益表 · 資產負債表 · **股利政策表** ·
  **除權除息結果表** · **月營收表** · 減資恢復買賣參考價 · 台股下市資料 · 分割後參考價 ·
  變更面額恢復買賣參考價.
- **衍生性 (derivatives):** 期貨/選擇權日成交總覽 · 期貨/選擇權即時報價總覽 · 期貨日成交 ·
  選擇權日成交 · 期貨三大法人 · 選擇權三大法人 · 期貨各券商每日交易 · 選擇權各券商每日交易.
- **其他 (other):** **相關新聞網頁 URL** · 黃金價格 · 原油(Brent/WTI) · **美股股價** ·
  **外幣對台幣(19 幣別匯率)** · **央行利率(12 國)** · 美國國債(1 月~30 年, 12 種).

The validated 6 (§1) are a subset. **Bolded** items are the highest-value for this app
(valuation, dividends, institutional/margin flow, monthly revenue, news URLs, US price,
multi-currency FX, central-bank rates).

## 3. Mapping to `portfolio-dash` modules

### `pricing/` (TW numbers of record)
- `TaiwanStockPrice` → TW quotes + history. **Advantage over TWSE/TPEx:** one API covers
  price **+ dividend + FX + financials**, queryable by date range, 600/hr → strong TW
  **primary candidate** (or co-primary with the government string sources, which give exact
  tick strings). Recommendation: government sources for exact tick precision on latest close;
  FinMind for batch history + the everything-else below.
- `TaiwanExchangeRate` → FX. Caveat: these are **bank cash/spot** quotes (buy & sell), not a
  single interbank mid — pick a convention (e.g. mid = (spot_buy+spot_sell)/2) and document it
  in the FX helper. yfinance `USDTWD=X` remains the simple mid-rate primary; FinMind FX is a
  cross-check / TW-centric fallback.
- **`TaiwanStockDividend` is the high-value item:** full 除權息 schedule with **cash dividend,
  ex-dividend trading date, and payment date** — directly powers (a) the TW cash-dividend
  **cost-reduction** accounting (`adjusted_total -= net cash dividend`) and (b) the dashboard's
  **ex-dividend calendar** + 回本進度/股利回收率. yfinance dividends are thinner here → FinMind
  is the recommended TW dividend primary.

### `llm_insight/` (qualitative + quant context, never numbers of record)
- **Financial statements** (EPS, revenue, margin trends) + **monthly revenue momentum** →
  fundamental scoring for TW holdings.
- **Institutional buy/sell (法人)** → accumulation/distribution flow signal.
- **Margin/short (融資融券)** → leverage extremes / contrarian sentiment flag.
- **Major shareholders (大戶)** → concentration/ownership context.
- **PER / PBR** → valuation context per holding (cheap/expensive vs its own history).
- **相關新聞網頁 URL** → seed for TW news retrieval (fills the news source the probe left TBD).
- **美股股價 / 19-currency FX / 12-country central-bank rates / US treasury** → cross-market +
  macro backdrop for insight cards.
These are decision-support signals, fed *into* prompts — the LLM narrates, it never becomes the
number of record.

### `data_ingestion/`
- FinMind is **market data, not your broker** → no account/position auto-import (unlike Schwab).
  TW manual/CSV entry stays.

### `scheduler/`
- EOD datasets refresh post-TW-close; respect the 600/hr budget (batch by date range, cache).

## 4. Decision-support insight recommendations (TW)
- **Fundamental score** = f(月營收 momentum, EPS/margin trend, institutional net-buy) per holding.
- **Sentiment/risk flags** = 融資 surge / 券 extremes (contrarian), 法人 divergence.
- **Income view** = ex-dividend calendar + projected cash dividends → 回本進度 / 股利回收率 and
  upcoming ex-div alerts (this is also a core dashboard section).

## 5. LLM self-backtest / self-improvement (powers the deferred `llm_insight/` prediction-tracking)
The recorded `llm_insight/` mechanism (LLM logs each forecast, later scores itself) is fed by
FinMind for TW exactly as Schwab feeds it for US:
- **Outcome oracle:** `TaiwanStockPrice` history → realized return over the prediction window →
  score the past prediction → update its **confidence index**.
- **Feature snapshot at prediction time:** financials + 月營收 + 法人 + 融資券 captured with the
  forecast → later learn *which TW signals preceded good vs bad calls* → corrective feedback
  into future prompts/weights.
This makes the prediction-tracking loop cross-market: US via Schwab, TW via FinMind, MY via the
existing quote sources.

## 6. Considerations
- **Rate limit 600/hr** (user tier) — generous for 1–2 users; still batch by date range + cache.
- **Token** in settings/`.env` only (gitignored), runtime-swappable (the 7-day trial → the
  permanent key with no code change — matches the probe's config-driven design).
- Floats from JSON → `Decimal(str(x))`; FX is bank buy/sell (document the mid convention).
- Recorded fixtures (§1) enable deterministic `pricing/` mock tests without a live token.

## Sources
- [FinMind documentation](https://finmind.github.io/)
- Live validation 2026-06-08 (token tier 600/hr); fixtures in `tests/pricing/fixtures/finmind/`.
