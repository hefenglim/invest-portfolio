# Claude Design Brief — portfolio-dash Dashboard (v1, 2026-06-11)

> Paste this entire document into Claude Design as the opening prompt.
> It contains: mission, hard constraints, section-by-section specs, display rules,
> a Traditional-Chinese label glossary, and the complete mock dataset to bind.

---

## 1. Mission

Design a **single-page personal investment portfolio dashboard** — the read-only
display layer of a private tool used by 1–2 people. It tracks stocks across
**Taiwan (TWSE/TPEx), US (NYSE/NASDAQ), and Malaysia (Bursa)** held in **4 broker
accounts** and **3 currencies (TWD / USD / MYR)**, reported in **TWD**.

Audience: the owner. Style target: **dense, data-first, decision-support**
("Bloomberg-lite", not a marketing page). Numbers are the heroes; chrome is minimal.

**Deliverable:** one self-contained static page — `index.html` + `styles.css` +
`app.js` (+ `mock-data.js` or inline JSON). Apache **ECharts via CDN** for all charts.
All data is bound from the mock JSON in §6.

## 2. Hard constraints (do not violate)

1. **Vanilla HTML/CSS/JS only.** No React/Vue/Svelte, no build step, no bundler,
   no CSS framework. ECharts from CDN is the only library.
2. **Never invent numbers.** Every figure on screen comes from the mock JSON.
   If a field is `null`, render the degraded state (§4) — never `0`, never a guess.
3. This export will be converted to **Jinja2 templates + HTMX/Alpine** wired to a
   real backend. Therefore: semantic HTML, repeated rows generated from the JSON
   (not hand-copied), **one consistent set of CSS custom-property design tokens**
   (colors/spacing/typography) defined once in `:root`.
4. **Color semantics — Taiwan market convention: red = gain/up, green = loss/down
   (紅漲綠跌).** Apply consistently to every P&L number, delta, and chart series.
5. **UI labels in Traditional Chinese** (glossary in §5). Numbers use Western digits.
6. **Theme: dark primary** (deep neutral background, high-contrast tabular numerals).
   Design tokens should make a light variant feasible later without redesign.
7. Desktop-first: must look excellent at 1440px and 1920px. Mobile = simple stacked
   fallback, low priority.
8. No login, no input forms, no settings pages, no routing/multi-page, no API calls,
   no loading spinners — this page renders one already-computed snapshot.

## 3. Page structure (top to bottom)

### A. Header bar
App title (portfolio-dash), 資料時間 `as_of`, 報告幣別 badge (TWD), and a global
freshness chip: green「資料新鮮」when `freshness.any_stale` is false, amber「部分過期」
when true (click/hover reveals section J detail). A visually-present but inert
「重新整理」button (backend wires it later).

### B. KPI band (8 cards)
From `kpis`: 總市值, 總報酬, 累計報酬率, 年化報酬 XIRR, 已實現損益, 未實現損益,
已實現匯損益, 未實現匯損益. Each card: big number, small label, red/green tint by
sign (per §2.4). Any `null` → "—" with a muted tooltip badge (e.g. 「匯率資料不足」).

### C. Main trend chart (the visual centerpiece)
ECharts line/area chart from `trend.points[]`: two series —
**總市值 `total_value`** (filled area) vs **累計淨投入 `net_invested`** (thin line).
X = date, daily granularity (real data has 150+ points; mock has 12 — design must
include `dataZoom` slider + 1M/3M/6M/全部 range buttons). Tooltip shows both values
+ their spread (浮動損益). Points with `incomplete: true` get a subtle marker/region
tint and a legend note 「部分標的當日無價格」. If `trend.available` is false, the
whole panel shows an empty state with `freshness.trend_unavailable_reason`.

### D. Holdings table (the core, give it room)
One row per element of `holdings[]`. Columns (in order):
代號+名稱 (symbol bold + name muted, board badge: TWSE/TPEx/.KL), 市場, 帳戶
(`account_name`), 股數, 原始均價, 調整均價, 現價 (+ 過期 badge when `price_stale`,
hover shows `price_as_of`), 市值, 未實現損益 (value + % vs adjusted cost, red/green),
股利回收率 `payback_ratio` (small progress bar), 權重 `weight` (mini horizontal bar
+ %). Dense rows (~32px), sticky header, zebra subtle. Client-side niceties in
vanilla JS: sort by column click; filter chips by 帳戶 and 市場. A totals row pinned
at bottom (sum 市值/未實現損益 in TWD from `kpis`). Rows where `market_value` is
`null` render "—" cells + a 「缺價」badge, and are excluded from the totals visual.

### E. Allocation row (two panels side by side)
- **產業配置**: ECharts donut (or treemap — designer's call) from
  `allocation.by_sector` + `weights`; legend with values & %.
- **幣別組成**: horizontal stacked bar or donut from
  `currency_view.by_currency_value` (native amounts per currency) with the blended
  TWD total `reporting_total_value` as the headline.
Both panels: if the section object is `null`, empty state 「匯率資料不足,無法合併計價」.

### F. 換匯損益 panel
One card per entry of `fx.by_account`: account name, pair (e.g. USD→TWD), 平均取得匯率
`avg_rate` vs 現時匯率 `current_spot` (delta colored), 外幣現金 `foreign_cash`,
外幣股票市值 `foreign_stock_value`, 已實現匯損益 `realized_fx`, 未實現匯損益
(stocks + cash, shown split and summed). `null` fields → "—" with 「無換匯紀錄」/
「匯率資料不足」badges. Footer line: reporting rollup (已實現 / 未實現, TWD).

### G. 已實現損益 table
Rows from `realized.rows`: 代號, 帳戶, 賣出股數, 淨收款, 調整成本移除, 已實現損益
(red/green). Footer: `realized.by_currency` per-currency totals.

### H. 股利區 (two panels)
- **年度股利**: ECharts stacked bar from `dividends.by_year` (one stack segment per
  currency, native amounts — label them clearly as 原幣金額). Headline:
  `total_by_currency` chips (TWD / USD / MYR totals).
- **除息日曆**: upcoming list from `ex_dividend_calendar[]`: 除息日 (big date block),
  代號+名稱, 每股金額 + 幣別, 發放日. Empty state: 「近期無除息事件」.

### I. AI 洞察 cards
Card grid from `insights[]`: title, 2–3 line body, `generated_at` timestamp footer,
subtle "AI" badge. **Also design the empty state** (the system ships before the AI
module): 「尚無 AI 洞察 — 洞察卡片由排程批次產生」with a muted illustration/icon.
Mock has 2 sample cards; show both states if possible (e.g. a commented variant).

### J. 資料新鮮度 footer (collapsible)
From `freshness`: two compact tables — per-symbol price `as_of` + 過期 flag;
per-pair FX `as_of` + 過期 flag. `missing_prices` / `missing_fx` as red chips.
If `xirr_unavailable_reason` / `trend_unavailable_reason` are non-null, show them
here verbatim as amber notes.

## 4. Display & formatting rules

- **Thousands separators everywhere** (639,600 / 1,618,682.54).
- Amounts: TWD 0 dp, USD/MYR 2 dp. MY share prices may need **3 dp** (e.g. 9.870).
- Percentages: 2 dp (21.47%). Rates (匯率): 2–4 dp as space allows.
- Signs: explicit + for gains (+117,500), − for losses; color per §2.4.
- Tabular/monospaced numerals in tables and KPI cards (font-variant-numeric:
  tabular-nums or a mono-numeral font).
- `null` → "—" (em-dash) + context badge. Never 0, never blank, never invented.
- Dates: YYYY-MM-DD. The `as_of` header timestamp: YYYY-MM-DD HH:mm (Asia/Taipei).
- Formatting lives in ONE JS helper module (fmtNumber/fmtPct/fmtDate) so the later
  backend integration can swap it server-side cleanly.

## 5. Label glossary (use these exact zh-TW strings)

總市值 · 總報酬 · 累計報酬率 · 年化報酬 (XIRR) · 已實現損益 · 未實現損益 ·
換匯損益 · 已實現匯損益 · 未實現匯損益 · 持倉明細 · 代號 · 名稱 · 市場 · 帳戶 ·
股數 · 原始均價 · 調整均價 · 現價 · 市值 · 權重 · 股利回收率 · 產業配置 ·
幣別組成 · 年度股利 · 除息日曆 · 除息日 · 發放日 · 每股金額 · 賣出股數 · 淨收款 ·
AI 洞察 · 資料時間 · 報告幣別 · 過期 · 缺價 · 資料新鮮 · 部分過期 · 重新整理 ·
累計淨投入 · 浮動損益

Account display names: TW Broker → 台灣券商 · Charles Schwab → 嘉信 Schwab ·
Moomoo MY (US) → Moomoo 美股 · Moomoo MY (MY) → Moomoo 馬股.
Markets: TW → 台股 · US → 美股 · MY → 馬股.

## 6. Mock dataset (bind exactly this; field names are the real contract)

Numbers are plausible but not cross-checked to the cent — treat them as display
data. The real backend emits this exact JSON shape (`DashboardData`).

```json
{
  "as_of": "2026-06-11T14:30:00+08:00",
  "reporting_currency": "TWD",
  "kpis": {
    "reporting_currency": "TWD",
    "total_market_value": 1618682.54,
    "total_return": 308529.66,
    "total_return_rate": 0.2147,
    "realized_total": 34931.12,
    "unrealized_total": 273598.54,
    "xirr": 0.1832,
    "fx_realized": 1250.00,
    "fx_unrealized": 14154.12
  },
  "holdings": [
    {"account_id": "tw_broker", "account_name": "TW Broker", "symbol": "2330",
     "name": "台積電", "market": "TW", "sector": "Semiconductors", "board": "TWSE",
     "quote_ccy": "TWD", "shares": 1000, "original_avg": 500.00, "adjusted_avg": 495.00,
     "original_cost_total": 500000, "adjusted_cost_total": 495000,
     "dividend_portion": 5000, "payback_ratio": 0.0100,
     "market_price": 612.5, "market_value": 612500, "unrealized_pnl": 117500,
     "capital_gain": 112500, "price_stale": false, "price_as_of": "2026-06-11",
     "weight": 0.3784},
    {"account_id": "tw_broker", "account_name": "TW Broker", "symbol": "0056",
     "name": "元大高股息", "market": "TW", "sector": "ETF", "board": "TWSE",
     "quote_ccy": "TWD", "shares": 10000, "original_avg": 36.20, "adjusted_avg": 34.85,
     "original_cost_total": 362000, "adjusted_cost_total": 348500,
     "dividend_portion": 13500, "payback_ratio": 0.0373,
     "market_price": 38.95, "market_value": 389500, "unrealized_pnl": 41000,
     "capital_gain": 27500, "price_stale": false, "price_as_of": "2026-06-11",
     "weight": 0.2406},
    {"account_id": "tw_broker", "account_name": "TW Broker", "symbol": "00919",
     "name": "群益台灣精選高息", "market": "TW", "sector": "ETF", "board": "TWSE",
     "quote_ccy": "TWD", "shares": 5000, "original_avg": 23.50, "adjusted_avg": 22.90,
     "original_cost_total": 117500, "adjusted_cost_total": 114500,
     "dividend_portion": 3000, "payback_ratio": 0.0255,
     "market_price": null, "market_value": null, "unrealized_pnl": null,
     "capital_gain": null, "price_stale": true, "price_as_of": null, "weight": null},
    {"account_id": "schwab", "account_name": "Charles Schwab", "symbol": "AAPL",
     "name": "Apple", "market": "US", "sector": "Tech", "board": "",
     "quote_ccy": "USD", "shares": 30, "original_avg": 182.50, "adjusted_avg": 182.50,
     "original_cost_total": 5475.00, "adjusted_cost_total": 5475.00,
     "dividend_portion": 28.80, "payback_ratio": 0.0053,
     "market_price": 211.40, "market_value": 6342.00, "unrealized_pnl": 867.00,
     "capital_gain": 867.00, "price_stale": false, "price_as_of": "2026-06-11",
     "weight": 0.1289},
    {"account_id": "schwab", "account_name": "Charles Schwab", "symbol": "MSFT",
     "name": "Microsoft", "market": "US", "sector": "Tech", "board": "",
     "quote_ccy": "USD", "shares": 12, "original_avg": 405.00, "adjusted_avg": 405.00,
     "original_cost_total": 4860.00, "adjusted_cost_total": 4860.00,
     "dividend_portion": 21.60, "payback_ratio": 0.0044,
     "market_price": 498.20, "market_value": 5978.40, "unrealized_pnl": 1118.40,
     "capital_gain": 1118.40, "price_stale": true, "price_as_of": "2026-06-06",
     "weight": 0.1215},
    {"account_id": "moomoo_my_us", "account_name": "Moomoo MY (US)", "symbol": "NVDA",
     "name": "NVIDIA", "market": "US", "sector": "Tech", "board": "",
     "quote_ccy": "USD", "shares": 25, "original_avg": 118.00, "adjusted_avg": 118.00,
     "original_cost_total": 2950.00, "adjusted_cost_total": 2950.00,
     "dividend_portion": 2.50, "payback_ratio": 0.0008,
     "market_price": 172.35, "market_value": 4308.75, "unrealized_pnl": 1358.75,
     "capital_gain": 1358.75, "price_stale": false, "price_as_of": "2026-06-11",
     "weight": 0.0876},
    {"account_id": "moomoo_my_my", "account_name": "Moomoo MY (MY)", "symbol": "1155.KL",
     "name": "Maybank", "market": "MY", "sector": "Financials", "board": ".KL",
     "quote_ccy": "MYR", "shares": 1000, "original_avg": 9.150, "adjusted_avg": 8.980,
     "original_cost_total": 9150.00, "adjusted_cost_total": 8980.00,
     "dividend_portion": 170.00, "payback_ratio": 0.0186,
     "market_price": 9.870, "market_value": 9870.00, "unrealized_pnl": 890.00,
     "capital_gain": 720.00, "price_stale": false, "price_as_of": "2026-06-11",
     "weight": 0.0430}
  ],
  "realized": {
    "rows": [
      {"account_id": "tw_broker", "symbol": "2330", "quote_ccy": "TWD",
       "shares_sold": 200, "proceeds_net": 119350, "original_cost_removed": 100000,
       "adjusted_cost_removed": 98000, "realized": 21350},
      {"account_id": "schwab", "symbol": "AAPL", "quote_ccy": "USD",
       "shares_sold": 5, "proceeds_net": 1002.30, "original_cost_removed": 589.50,
       "adjusted_cost_removed": 589.50, "realized": 412.80}
    ],
    "by_currency": {"TWD": 21350, "USD": 412.80}
  },
  "returns": {
    "by_currency": {
      "TWD": {"realized": 21350, "unrealized": 158500, "total_return": 179850,
               "gross_invested": 979500, "rate": 0.1836},
      "USD": {"realized": 412.80, "unrealized": 3344.15, "total_return": 3756.95,
               "gross_invested": 13285.00, "rate": 0.2828},
      "MYR": {"realized": 0, "unrealized": 890.00, "total_return": 890.00,
               "gross_invested": 9150.00, "rate": 0.0973}
    },
    "reporting_currency": "TWD", "reporting_total_return": 308529.66, "xirr": null
  },
  "allocation": {
    "by_sector": {"Semiconductors": 612500, "ETF": 389500, "Tech": 547099.04,
                   "Financials": 69583.50},
    "weights": {"Semiconductors": 0.3784, "ETF": 0.2406, "Tech": 0.3380,
                 "Financials": 0.0430},
    "reporting_currency": "TWD"
  },
  "currency_view": {
    "by_currency_value": {"TWD": 1002000, "USD": 16629.15, "MYR": 9870.00},
    "reporting_total_value": 1618682.54, "reporting_currency": "TWD"
  },
  "fx": {
    "by_account": {
      "schwab": {"account_id": "schwab", "home_ccy": "TWD", "foreign_ccy": "USD",
        "avg_rate": 31.80, "current_spot": 32.90, "foreign_cash": 1420.55,
        "foreign_stock_value": 12320.40, "realized_fx": 1250.00,
        "unrealized_fx_stocks": 13552.44, "unrealized_fx_cash": 1562.61},
      "moomoo_my_us": {"account_id": "moomoo_my_us", "home_ccy": "MYR",
        "foreign_ccy": "USD", "avg_rate": 4.450, "current_spot": 4.420,
        "foreign_cash": 230.10, "foreign_stock_value": 4308.75, "realized_fx": 0,
        "unrealized_fx_stocks": -129.26, "unrealized_fx_cash": -6.90}
    },
    "reporting_currency": "TWD",
    "reporting_realized_fx": 1250.00, "reporting_unrealized_fx": 14154.12
  },
  "dividends": {
    "by_year": [
      {"year": 2024, "by_currency": {"TWD": 8200}},
      {"year": 2025, "by_currency": {"TWD": 14650, "USD": 86.40, "MYR": 412.00}},
      {"year": 2026, "by_currency": {"TWD": 18500, "USD": 52.10, "MYR": 280.00}}
    ],
    "total_by_currency": {"TWD": 41350, "USD": 138.50, "MYR": 692.00}
  },
  "ex_dividend_calendar": [
    {"symbol": "2330", "name": "台積電", "ex_date": "2026-06-20",
     "pay_date": "2026-07-16", "cash_amount": 5.00, "stock_amount": null,
     "currency": "TWD", "source": "twse"},
    {"symbol": "1155.KL", "name": "Maybank", "ex_date": "2026-06-25",
     "pay_date": "2026-07-10", "cash_amount": 0.32, "stock_amount": null,
     "currency": "MYR", "source": "yfinance"},
    {"symbol": "0056", "name": "元大高股息", "ex_date": "2026-07-15",
     "pay_date": "2026-08-06", "cash_amount": 0.85, "stock_amount": null,
     "currency": "TWD", "source": "twse"}
  ],
  "trend": {
    "available": true, "reporting_currency": "TWD",
    "points": [
      {"date": "2026-01-05", "total_value": 980000, "net_invested": 1012000, "incomplete": false},
      {"date": "2026-01-20", "total_value": 1015000, "net_invested": 1095000, "incomplete": false},
      {"date": "2026-02-03", "total_value": 1124000, "net_invested": 1180000, "incomplete": false},
      {"date": "2026-02-17", "total_value": 1098000, "net_invested": 1180000, "incomplete": true},
      {"date": "2026-03-02", "total_value": 1186000, "net_invested": 1228000, "incomplete": false},
      {"date": "2026-03-16", "total_value": 1242000, "net_invested": 1262000, "incomplete": false},
      {"date": "2026-03-30", "total_value": 1198000, "net_invested": 1262000, "incomplete": false},
      {"date": "2026-04-13", "total_value": 1286000, "net_invested": 1291000, "incomplete": false},
      {"date": "2026-04-27", "total_value": 1342000, "net_invested": 1291000, "incomplete": false},
      {"date": "2026-05-11", "total_value": 1420000, "net_invested": 1310153, "incomplete": false},
      {"date": "2026-05-25", "total_value": 1518000, "net_invested": 1310153, "incomplete": false},
      {"date": "2026-06-11", "total_value": 1618682.54, "net_invested": 1310153, "incomplete": false}
    ]
  },
  "freshness": {
    "prices": [
      {"symbol": "2330", "as_of": "2026-06-11", "stale": false},
      {"symbol": "0056", "as_of": "2026-06-11", "stale": false},
      {"symbol": "00919", "as_of": null, "stale": true},
      {"symbol": "AAPL", "as_of": "2026-06-11", "stale": false},
      {"symbol": "MSFT", "as_of": "2026-06-06", "stale": true},
      {"symbol": "NVDA", "as_of": "2026-06-11", "stale": false},
      {"symbol": "1155.KL", "as_of": "2026-06-11", "stale": false}
    ],
    "fx": [
      {"base": "USD", "quote": "TWD", "as_of": "2026-06-11", "stale": false},
      {"base": "MYR", "quote": "TWD", "as_of": "2026-06-11", "stale": false},
      {"base": "USD", "quote": "MYR", "as_of": "2026-06-11", "stale": false}
    ],
    "any_stale": true,
    "missing_prices": ["00919"],
    "missing_fx": [],
    "xirr_unavailable_reason": null,
    "trend_unavailable_reason": null
  },
  "insights": [
    {"id": "ic-001", "title": "半導體部位集中度偏高",
     "body": "台積電與科技股合計約占組合 71%。若半導體景氣反轉,組合波動將顯著放大;可留意 ETF 與金融股的再平衡空間。",
     "generated_at": "2026-06-11T08:00:00+08:00"},
    {"id": "ic-002", "title": "USD 匯率順風貢獻明顯",
     "body": "Schwab 美元部位平均取得匯率 31.80,現時 32.90,未實現匯兌貢獻約 +15,115 TWD。若 USD 回落,此部分收益將回吐。",
     "generated_at": "2026-06-11T08:00:00+08:00"}
  ]
}
```

Binding notes:
- `returns.xirr` is always `null` by design — the XIRR number lives only in `kpis.xirr`.
- Real `trend.points` is daily (can exceed 300 points); mock is sparse for brevity.
- `holdings[].weight` may be `null` independently of `market_value`.
- Sections `returns` / `allocation` / `currency_view` / `fx` can each be entirely
  `null` (cold-start). Design every panel's empty state, not just the happy path.

## 7. Out of scope (do not design)

Login/auth · transaction/dividend input forms · settings pages (LLM/scheduler/fees)
· instrument registration/watchlist · multi-page navigation · real refresh behavior
· websockets/streaming. These come later as separate screens.
