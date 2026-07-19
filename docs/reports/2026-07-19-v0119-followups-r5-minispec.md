# Mini-spec — follow-ups round 5 (owner feedback batch on r4 surfaces)

Date: 2026-07-19 · Branch: `feat/v0119-followups` (on top of verified r4 @12702d4)

## Decisions

- **FU-D40 — FX ledger under 換匯中心.** The 換匯中心 tab lists the fx_conversions ledger
  (recent-first, paged like the movements ledger) so operators see history where they act.
- **FU-D41 — AI-input symbol identification hardening.** Bug (screenshot): 「前天聯電買入1張」
  parsed to symbol "UMC" (US-style ticker) for a tw_broker row → lookup dead. Fix the AI-input
  prompt (registry constant, version bump): the symbol MUST be the LOCAL exchange code of the
  account's market — TW = TWSE/TPEx numeric code (聯電→2303), US = ticker, MY = Bursa code —
  with explicit examples; row-level soft validation flags a format-mismatched symbol
  (e.g. non-numeric on a TW account) in the preview reason.
- **FU-D42 — quick-add dialog: editable symbol + auto-lookup + AI resolve fallback.**
  (a) The symbol field is EDITABLE even when opened prefilled from the AI row (lockSymbol
  removed/false); editing re-runs the lookup. (b) On open with a prefilled symbol the lookup
  fires automatically (name/sector auto-fill). (c) When the lookup finds nothing (查無報價),
  the dialog offers 「AI 判讀」: new registry prompt `ai_symbol_resolve` (+ endpoint
  POST /api/instruments/ai-resolve {query, market}) — the default-role model maps the user's
  raw input (company name / wrong-form ticker) to the local symbol + name; the REAL lookup then
  re-verifies (provider quote check stays the typo authority; the LLM never supplies numbers).
  Same behavior in the watchlist add flow (shared dialog = both entries).
- **FU-D43 — withdraw guard + max-fill + FX estimate.** (a) 出金: selecting account+ccy shows
  the pool balance; withdrawals exceeding it are hard-blocked front+back (422
  `withdraw_insufficient_balance`); the ack/negative override is removed for kind=withdraw
  (deposit/opening unchanged; trade-settlement cash flows unaffected). PUT edits validate too.
  (b) Clicking the balance figure (FX 可用餘額 or withdraw 賬戶現金) fills the max amount.
  (c) FX estimate: entering the sell amount auto-fills the buy amount from the LATEST stored
  rate via a SERVER endpoint (GET /api/cash/fx-estimate?from_ccy&to_ccy&amount →
  {estimate, rate, rate_as_of} Decimal strings; frontend displays only, never computes) with a
  caption 「以 {rate_as_of} 匯率 {rate} 試算」; the estimate stops overwriting once the user
  edits the buy amount; the LEDGER records actual entered amounts (implied actual rate remains
  authoritative).
- **FU-D44 — sell-entry hints.** Manual pane, side=sell + symbol chosen: under 股數 show
  「可賣 {shares} 股」 (click fills); under 價格 show 「持有均價 {adjusted_avg}」 (hint; click
  fills). Server-computed via an extended per-account holdings read (shares + adjusted_avg as
  Decimal strings); no frontend math.
- **FU-D45 — ledger live refresh.** After ANY successful input commit (manual/CSV/AI/dividend/
  opening), the lower 帳本記錄 active tab re-fetches in place (no full reload).
- **FU-D46 — scheduler progress + result details.** In-flight registry carries a progress
  message jobs update mid-run (`set_progress(job_id, msg)` — wired into the looping jobs:
  history backfill per-symbol, news pipeline, insight batch); status endpoint exposes it; the
  row's sub-text shows it during 執行中. On completion, clicking the status chip opens a detail
  modal: full run detail, started/finished/duration, error text on failure, and LLM cost line
  when the run's detail carries one; jobs with a natural landing page get a 前往 link
  (per-job href map, verified targets).
- **FU-D47 — dividend surfaces consolidation redesign.** Owner ruling: the OLD 股利區 and the
  NEW 股利收入 card are reviewed together and replaced by ONE freshly-designed best surface —
  dense, data-first: TTM headline + per-ccy, yearly received bars, forecast-only projection,
  ex-div calendar, 回本進度/yield-on-cost if cheaply available from the payload — removing the
  duplicated chart/calendar. Frontend-only (payload already serves the data); display-only
  attribution captions retained.
- **FU-D48 — plain-language explainers (report-time).** After completion, the owner report
  explains P1② (names resolver) and multi-user Phase 0 in plain zh with simple flow diagrams.

## Waves (all parallel — file-disjoint)

- **W-CASH** D40+D43: web/cash.html, web/cash.js, api/routers/cash.py, stress scenario
  amounts if needed.
- **W-AI** D41+D42: llm_insight/official_templates.py, data_ingestion/agents.py,
  web/inst-quickadd.js, api/routers/instruments.py, api/routers/input_center.py (row reason
  only if needed).
- **W-TRADE** D44+D45 (+ flip the AI-row quick-add call to editable symbol): web/input.js,
  web/trades.html, web/ledger.js, api/routers/input_center.py (holdings read extension).
  NOTE: input_center.py shared with W-AI — W-AI restricts itself to agents/instruments seams;
  if both need input_center.py, W-TRADE owns it and W-AI reports instead.
- **W-SCHED** D46: scheduler/jobs.py (progress seam), api/routers/scheduler.py,
  web/settings-scheduler.js, web/settings.html (scheduler section).
- **W-DIV** D47: web/index.html, web/app.js (old 股利區 section), web/charts.js (old dividend
  chart/calendar), web/dividends-card.js.

## Acceptance
Full pytest, bare mypy --no-incremental, ruff, stress with-UI fail=0, id sweep, demo deploy +
verify_live + probe; orchestrator deep coordinated review before the owner report.
