# Research: FinMind — capabilities & system fit (validated 2026-06-08)

- **Date:** 2026-06-08
- **Status:** research note + **live validation** (7-day trial token; user tier 600 req/hr).
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

## 2. Full dataset catalogue (from docs — beyond what was validated)

- **TW technical:** stock prices, historical, **K-line**, technical indicators, index codes.
- **TW fundamental:** financial statements, cash-flow, balance sheet, **dividend policy**,
  **monthly revenue (月營收)**.
- **TW chip/shareholding:** margin trading, institutional positions, **major-shareholder /
  大戶持股**.
- **TW derivatives:** futures & options trades; **convertible bonds**; **real-time** stock/
  futures/options.
- **International:** **US** stock daily & minute (from 2021-04-28), government-bond yields;
  UK/Europe/Japan; **commodities** (oil, gold).
- **Macro:** G8 central-bank rates, **exchange rates**, money supply.

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
