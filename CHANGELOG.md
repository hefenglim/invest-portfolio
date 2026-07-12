# Changelog

All notable changes to this project are documented here. Format based on
*Keep a Changelog*; released versions use the heading `## [vMAJOR.MINOR.PATCH] - YYYY-MM-DD`.

**Integrity check** — after any edit to this file, run
`grep -c "^## \[v" CHANGELOG.md`; the count must equal the number of released version
headings. (`## [Unreleased]` is intentionally not counted.)

## [Unreleased]

### Planned
- **Unified auto-import principle:** the manual ledger is the source of truth; data-source data
  (FinMind dividend/ex-div, Schwab transactions) is matched to holdings and offered for a
  **user-confirmed** auto-import into the ledger following the account's accounting rules —
  cutting manual entry, never bypassing confirmation, never double-counting (calc reads only the
  ledger), `original_cost` never overwritten; **manual entry always retained**.
- `data_ingestion/` confirmed auto-import (future): match `pricing/`'s fetched dividend/ex-div
  events (and Schwab transactions) to the holdings list → prompt "new distribution detected —
  auto-import?" → on confirm, write a ledger entry per the account's dividend model (TW cash →
  cost reduction, US DRIP $0-cost, MY cash). `web_ui/` provides the prompt UI.
- `llm_insight/` prediction self-tracking + backtest loop (future sub-project): the LLM
  records each recommendation/forecast, later replays and scores its own past predictions
  against realized outcomes, accumulating a per-prediction confidence index and a
  corrective feedback loop that informs future advice. Gets its own brainstorm at the
  `llm_insight/` stage.
- `llm_insight/` insight inputs & per-stock prompt (future): per-holding decision signals from
  FinMind (財報 / 月營收 / 法人 / 融資券 / PER-PBR / news URL) plus **US sentiment indicators —
  CNN Fear & Greed Index and VIX** — as buy/sell context. **Prompt architecture (decided
  2026-06-08):** one editable **default system prompt** (ships as a Claude-recommended best prompt; user
  fine-tunes in config) holds the output contract + invariants (JSON schema, no
  numbers-of-record, batch-only) and is immutable by overrides; reusable, named
  **Strategy Prompts** (the library ships with several Claude-generated optimized templates;
  users can add their own) add a per-type analytical focus, and each stock's Strategy is **blank by
  default**, optionally **selecting 0..1** from the library (per-stock assignment — option A; data model pre-reserves tag/category binding for a
  later upgrade). All prompts live in the settings (config) page, versioned and folded into the
  cache fingerprint + self-backtest attribution (per `llm-insight.md`).
- **AI cost-info + LLM settings page** (`web_ui/`, future): the **backend is now built** (model
  registry, four role-defaults, USD budget governance, `llm_usage` log + cost calc, vision plumbing —
  see Added). Remaining is the `web_ui/` page: usage stats + history-trend + per-model cost charts;
  model add/edit (provider / endpoint / key / vision / pricing); role-default pickers; budget
  set/reset; and the screenshot-upload widget for vision (statement → draft → confirm).
- **Design principle (all modules):** invest in adjustable structure — config-driven behavior,
  provider/strategy protocols + registries, swappable adapters, decoupled layers — so future
  changes are config edits + small additions, not rewrites; keep YAGNI on features/scale (per
  `stack.md`), deferring concrete specifics until real use surfaces them.
- **Per-user dataset management (future):** the earlier folder/dataset-switching idea is deferred
  and reframed as a **multi-user** feature — different users each independently manage their own
  dataset(s) within one deployment. (The current prod/test split is achieved by **separate
  instances** — own checkout + venv + data folder per instance — not by switching datasets on one
  site; see `engineering-process.md` → "Two-environment loop-engineering".)

## [v0.1.15] - 2026-07-12

Hotfix for the v0.1.14 notification channels (owner field report, same day).

### Fixed
- **Enable toggles now persist on click.** The channel/quiet-hours toggles only
  flipped a CSS class until the separate 儲存 button was pressed, which read as
  "enabled but cannot be turned off". A click now sends a minimal
  `PUT {channel:{enabled}}` immediately (optimistic flip, revert + toast on
  failure); the save buttons still persist field edits.
- **Provider error reasons are surfaced.** A failing test-send showed only the
  bare HTTP status line; Telegram's response body carries the actionable reason
  (e.g. "Bad Request: chat not found" = the bot was never /start-ed or the
  chat_id is wrong). ntfy and Telegram errors now include the response-body
  description (bounded, still secret-redacted); `chat_id` is trimmed before
  sending; the Telegram card documents the /start requirement and where to find
  a numeric chat_id.

## [v0.1.14] - 2026-07-12

Blueprint Phase 3 batch 1: multi-channel push notifications — alerts and rule-signal
transition events finally reach the owner's phone. Security-audited (independent ★
deep review: secret handling probed clean across every exception path).

### Added
- **`ops/notify.py` leaf module** with three channels (owner decision D1, 2026-07-12
  — all verified free): **ntfy** (JSON publish endpoint, default https://ntfy.sh,
  auto-generated long random topic — the topic IS the read secret; `allow_redirects`
  disabled, 3xx = failure), **Telegram** (bot sendMessage, PLAIN text — no
  parse_mode, no Markdown-injection surface), **Email** (stdlib smtplib, zero new
  dependencies; STARTTLS/SSL/none). **Multi-channel fan-out:** every enabled channel
  receives each message; a failing channel is isolated (logged, never blocks the
  others or the scheduler). Timeouts on every call; every channel wraps errors
  through a redactor so tokens/passwords can never reach logs, run details, or API
  responses (probe-verified incl. requests exceptions that embed the token URL).
- **Dispatch pipeline:** `alert_events` gains `notified_at` + `notify_attempts`
  (additive migration; independent of the on_alert `consumed` path). The alert_scan
  tail pushes undispatched events (covers signal_scan's 14:55 events): subscription
  filter → quiet hours (Asia/Taipei, midnight-wrap aware; hold-then-release;
  malformed config fails OPEN — an alert system must not silently suppress) →
  zh-TW message (rule label + symbol, no amounts) → **atomic claim** per event
  (closes the cron-vs-manual double-send race) → fan-out → all-channels-fail
  releases the claim and bumps `notify_attempts`, giving up at 3 so a permanently
  broken channel can never starve newer alerts; cap 10 events/run (no
  post-outage flood). Idempotent: claimed events never resend.
- **Settings UI (canonical settings → 預警規則 tab):** 通知通道 section — three
  channel cards (enable / fields / save / 傳送測試訊息), quiet hours, per-rule
  subscriptions incl. the `signal_*` events (default all on). Secrets masked on
  read, placeholder-preserving on write (LLM-key convention); ntfy topic shown
  with a copy affordance and a "topic = password" hint.
- **API:** `GET/PUT /api/notify/config`, `POST /api/notify/test` (per-channel test
  send). **Guest-mode lockdown (security review):** on a guest instance (public
  demo) PUT/test return 403 and GET masks the ntfy topic — notification channels
  are configured on the protected production site only. PUT validates: no userinfo
  in the ntfy server URL, http(s) scheme only, strict SMTP host shape, 400 on junk.

### Fixed
- **Legacy-DB boot crash caught by the deploy gate:** the `notified_at` index lived
  inside the initial DDL script and ran before the column migration on live DBs
  whose `alert_events` predates the column — the demo instance crash-looped at
  startup. The index is now created after `_add_column_if_missing`, with a
  legacy-schema regression test (a fresh-DB suite cannot see this ordering class).

## [v0.1.13] - 2026-07-11

Blueprint P2 "technical-rules engine" release: local, stateful, auditable rule
signals for every held AND watched symbol — TechScore, transition events into the
alert stream, drawer signal chips, an LLM variable, and health-check v2.5 that
interprets (never computes) them. Three batches, each gated by an independent
deep-review audit.

### Added
- **Rules engine core `strategy/rules/`** (P2-2A): frozen `rules-v1` params; four
  evidence-based rules — MA200 trend filter (±2% hysteresis band + 2-day confirm),
  SMA50/200 golden/death cross with volume confirmation (×1.00 confirmed / ×0.75
  unconfirmed = 54/72 / ×0.85 unknown — never faked) and linear age decay,
  12-1 momentum (skip-month convention; flat dead-band forces score 0), RSI(14) +
  52-week context (halved magnitude by design); composite TechScore 0–100 with
  per-rule contribution audit trail, coverage renormalization over evaluable rules,
  and a deterministic zh-TW evaluation-context sentence. Pure Decimal, honest
  `None` on thin data, `params_version` stamped on every result (replay discipline).
  **Deep-review calibration (2026-07-10, recorded here): cross decay = 60 sessions**
  (cited death-cross evidence: ~random after ~30 days — half-weight at day 30),
  superseding the initial 120; unmeasured momentum is labelled honestly (dedicated
  uptrend/downtrend context labels — never called "weakening").
- **Signals API + transition events** (P2-2B): `GET /api/signals` (+ single-symbol
  variant) serving every registered instrument with a `held` flag; history window
  derived from params (260 sessions → 583 calendar days). `signal_states` derived
  cache (rebuildable; truth stays `prices`) detects regime transitions with HOLD
  semantics — trend fires only on confirmed up↔down, momentum only on
  positive↔negative, both holding their last direction through neutral/flat noise
  (without this the momentum event was unreachable); fresh golden/death crosses
  fire with a both-days_ago-present guard. Events land in `alert_events`
  (`signal_trend`/`signal_cross`/`signal_momentum`), first scan seeds silently
  (no event storm), same-day re-scans coalesce, `params_version` changes reseed
  silently. New `signal_scan` job (weekdays 14:55 Asia/Taipei, before alert_scan);
  the `'all'` alert-subscription wildcard deliberately EXCLUDES `signal_*`
  (explicit listing subscribes — AI-card cost stays opt-in).
- **Watchlist coverage** (owner decision 2026-07-11, recorded here): watched
  (registered but unheld) symbols get the full signal treatment — scan seeding,
  transition events, API rows (`held:false`), and drawer chips — a watched symbol
  is an entry candidate (a golden cross there is exactly the wanted alert). A
  sold-but-registered symbol stays tracked as a re-entry candidate.
- **Drawer 技術訊號 chips** (P2-2B/2C): TechScore + coverage, four rule chips with
  key evidence, and the engine's condition sentence, for held and watched symbols;
  neutral accent styling (signals are not P&L); honest 資料不足 empty state.
- **`rule_signals_json` variable** (P2-2C): registry grows 33→34 (price category);
  fed via `external_vars` from the SAME evaluation/serialization seam as
  `/api/signals` (byte-identical, test-pinned); honest partial pass-through
  (per-rule nulls) and thin-data degrade with an explicit reason.
- **Health-check strategy v2.5** (library `official-v5`): cites TechScore/coverage,
  each rule's state with its key evidence number, and the condition sentence
  verbatim — interpret only, never recompute; unheld symbols are framed as entry
  assessment (建倉評估) instead of add/trim; unavailable data is stated honestly.
  Task presets reference strategies by name — no preset change.
- **Opt-in task universe "holdings + watchlist"** (`mode:"all_registered"`):
  explicit wizard option with a per-symbol cost hint; default stays holdings-only.
  A registered symbol without prices takes the zero-LLM anomaly-card path.

### Fixed
- `volume_signal`'s trailing-gap trim (v0.1.12) is consumed by the cross rule's
  volume confirmation — an unknown-volume cross day degrades the confidence
  modifier to ×0.85 instead of raising or fabricating.

## [v0.1.12] - 2026-07-09

Blueprint P1 "data foundation" release: trading volume across all three markets,
5-year price history, and the analyst-consensus variable — the data bedrock for the
upcoming rules engine (P2) and backtest/decision-quality loop (P4).

### Added
- **Trading volume end to end** (P1-1A): the yfinance provider now reads the `Volume`
  column in history/latest fetches into the long-reserved `prices.volume` column
  (canonical integer strings — volume is not money, the 2dp rule never applied);
  `get_price_history`/`PriceRead` gain an additive `volume` field; the insight
  generation AND preview paths feed `VarContext.volumes` (aligned 1:1 with closes,
  probe-gated) so `technical_signals_json` now emits its volume section
  (`ratio_to_avg` + `surge`). Live-verified on the test site: 13 instruments at
  98.9–100% recent-window volume coverage after the deep backfill.
- **FinMind TW quote-history fallback** (P1-1A): `FinMindProvider` gains
  `QUOTE_HISTORY` support (`TaiwanStockPrice`: OHLC + `Trading_Volume`), appended
  after yfinance in the TW chain — removing the yfinance single point for price
  history. Token-gated exactly as the dividend path.
- **5-year price history** (P1-1B; owner decision 2026-07-08 supersedes the
  blueprint's 3-year recommendation, recorded here): new `history_backfill_days`
  setting (default 1825, env-overridable) replaces the two scattered 365-day
  literals (quick-register backfill + smart backfill windows; the
  extend-to-first-acquisition logic is unchanged). The 52-week position now reaches
  its full 252-session window.
- **Analyst-consensus variable `consensus_json`** (P1-1C): new
  `pricing/consensus_source.py` fetches yfinance's two light endpoints
  (`analyst_price_targets` + `recommendations_summary`; never the heavy
  `Ticker.info`) into idempotent per-symbol snapshots — target prices (Decimal
  strings under the 4dp float-noise cap), this/last-month rating distributions,
  and locally computed weighted `rating_score` (1–5) + `upside_vs_mean_pct`.
  Convention (invariant #1): consensus numbers are fetched from the finance API and
  computed locally — the LLM only interprets them. New `consensus_daily` job
  (09:10 Asia/Taipei; manual trigger via the existing
  `POST /api/scheduler/jobs/{id}/run`). Variable registry grows 32→33 across
  9→10 categories (new 分析師共識 category, mirrored in `web/vars.js`); symbols
  without coverage degrade honestly with an explicit no-coverage reason.
- **Health-check strategy v2.4** (template library `official-v4`): adds a consensus
  section — target range/mean vs current + upside, rating distribution and its
  month-over-month shift — and must state 無分析師覆蓋 explicitly when the variable
  is unavailable. Official byte-freeze / reset-to-official conventions unchanged;
  the task-preset pack references the strategy by name and needed no change.

### Fixed
- **`volume_signal` None-safety + trailing-gap trim** (caught by live verification,
  not by the green unit gates): the newest TW price row is written by the twse
  latest-quote provider, which carries no volume, so the volume signal would have
  degraded — or raised on an interior gap — on every TW/MY symbol daily. It now
  accepts None-padded series (`Sequence[Decimal | None]`), trims trailing
  volume-less sessions (the next history refresh heals them a day later), and
  degrades honestly on an interior-window gap; the type bridge cast in
  `llm_insight.variables` is gone.

## [v0.1.11] - 2026-07-08

The AI-input optimization program release: four feature batches + a full-system
deep-review remediation, bundled per the 2026-07-05 decision.

### Added
- **Official task pack** (`POST /api/insight-tasks/official-pack`): one-click creation of
  the three official insight tasks (持倉週報 Sat 09:00 · 個股健檢 Mon 09:00 · 市場週報
  Sat 09:30) with strategies + weekly crons; idempotent via a `preset_key` provenance
  column (rename-safe). Official template library v2/v3 (`official_templates.py`) with
  reset/from-template endpoints; system prompt v2; templates 持倉週報 v2.1 / 個股健檢
  v2.3 / 市場週報 v1.1.
- **per_market scope**: one insight card per held market (TW/US/MY) with a codified
  zero-leak guarantee — portfolio variables slice to the market
  (`portfolio/market_view.py`, `VarContext.market`); whole-portfolio vars carry honest
  scope notes; market cards strip model-emitted predictions at store time.
- **Technical signals** (`portfolio/technicals.py`, pure Decimal): Wilder RSI(14),
  MA20/60 golden/death cross + days-ago, 52-week position (honest window), swing trend
  structure, probe-gated volume; bundled as `technical_signals_json`. CNN Fear & Greed
  local five-zone classification + 7-day trend (`fear_greed_json`, standalone variable).
- **News content pipeline** (`portfolio_dash/news/`, new module): FinMind (中文) +
  yfinance (英文, incl. .TW/.TWO) + Yahoo-TW discovery → general HTML fetcher
  (http(s)-only, bounded, non-prose guard) → default-LLM organizer (editable official
  news prompt v2, `GET/PUT/POST /api/news-prompt(/reset)`) → **separate SQLite news DB**
  (`news.db` beside the ledger DB — decision 2026-07-06: larger text volume off the
  transactional DB, multi-account-share-ready; included in the daily backup) → precise
  per-symbol mentions index (held-universe allowlist) → `symbol_news_json` variable +
  個股健檢 news section. Nightly `news_daily` job (06:00). News library page
  (`news.html`): filters (stock/source/date/server keyword), row → full-summary modal.
- **Unified AI attribution** on every LLM surface (`fmt.aiAttrib`): model · token N ·
  $cost on insight cards, dashboard AI panel, and the news modal; `insights` gains
  `tokens_in/tokens_out`, `organized_news` gains `model`.
- **LLM request ledger**: `llm_usage` gains `cache_tokens` (provider-reported cached
  prompt tokens, captured defensively); `GET /api/llm/requests` (paged, agent/model/
  time-window filters, Taipei-normalized timestamps); Request 明細 panel on the AI 與額度
  tab; 執行歷史 LLM-kind rows deep-link 「查看 AI 請求」 to the run's request window.
- **Database statistics panel** (`GET /api/db-stats`): row counts for every table across
  BOTH SQLite files, grouped by category, with oldest-record dates and file sizes —
  the owner observes before deciding retention (no pruning built).
- **Site-wide pagination**: shared `web/pager.js` (windowed pages + jump); real paging on
  Request 明細, 執行歷史 (server job filter), 系統操作記錄, news, insight cards, AI 戰績,
  持倉健診 (server-side symbol grouping), trades ledgers ×4 (server account/date
  filters), cash movements. `/api/insights` now bounded ({rows,total_count}, default
  100/max 500); `/api/ai-score` rows paged; `/api/cash` gains limit/offset.
- **每頁筆數 setting**: backend-persisted `ui_prefs` (`GET/PUT /api/ui-prefs`,
  page_size ∈ {20,50,100,200}, default 50) editable in settings 一般; `window.pdPrefs`.
- Loop-2 scoring rubric v2 (direction 40 / citation 30 / scenario 20 / timeliness 10)
  and `price_at_create` baseline snapshot (decision Q1c): cards score against the close
  the model actually saw.

### Fixed
- **Deep-review remediation** (three parallel xHigh reviews, 2026-07-06/07): insight
  schedule bind/unbind now syncs the live APScheduler (new crons fire without restart;
  deleted tasks stop firing; invalid cron → 400); disabled/archived tasks are enforced
  at execution (cron skips with reason; manual run → 409); single Asia/Taipei day-anchor
  clock (`shared/clock.app_now`) across cron/API/backup/news (fixes the UTC/Taipei split
  that re-broke the day-anchored cache on the scheduler path); news mentions merge
  instead of being wiped on headline→organized upgrade; archived tasks excluded from
  Loop-2 scoring; cron overlap guard; preview/run technical close-window unified (400d).
- **Frontend attention surfaces**: news modal opaque (undefined CSS tokens); alert bell
  copy humanized (display-quantized %, account display names, zh); 產生洞察 button wired
  (run/menu/redirect); mobile CJK vertical collapse; CLS min-height reservations;
  pipeline-hub copy productized with live counts; legacy `ledger.html`/`input.html` and
  all standalone `settings-*.html` converted to redirect stubs (single canonical tabbed
  settings surface — ends the dual-surface drift class); stale localStorage auth copy
  replaced; scraper-garbage summaries prevented (fetcher non-prose guard + organizer
  prompt rule).
- **Stale-asset cache class bug**: static files now send `Cache-Control: no-cache`
  (ETag revalidation) and every local asset tag carries `?v=<version>` (rerunnable
  `scripts/stamp_asset_version.py`; guarded by `test_static_cache_discipline.py`) —
  a cached old `format.js` had blanked the insights page after deploy.
- **iOS Safari alert panel**: the bell panel is portaled to `document.body` (a
  `backdrop-filter` ancestor hijacks fixed-position containing blocks on Safari),
  anchored from the bell rect, `100dvh`-aware; mobile panel fully on-screen.
- LLM error taxonomy honesty (F2), day-anchored cache fingerprint (F5), accounts
  catalog for the parser (F7), and the first-run 4-call/zero-card failure (F1:
  in-prompt JSON schema contract + tolerant parse) — the ignition-round fixes.

### Changed
- `log_usage` timestamps now use the Asia/Taipei app clock (single-clock discipline).
- 用量與趨勢 chart regression on the tabbed settings path fixed (guarded wiring).
- mypy strict now also covers previously-untyped test files touched this cycle.

## [v0.1.10] - 2026-07-05

LLM pillar IGNITED: the first live batch runs on the test instance exposed seven
defects invisible to the mocked suite — all fixed with regression tests — followed
by the AI-input optimization program (audited all 7 LLM surfaces, shipped the
official-v2 template library) per the 2026-07-05 user directives.

### Fixed
- **Structured output was 100% broken on live providers**: LiteLLM's capability map
  returns False for every ``openrouter/*`` id, so ``response_format`` was never sent
  and nothing in the prompt asked for JSON — models replied prose and every insight
  run failed both role models. ``shared/llm.py`` now always appends a schema-derived
  JSON-only contract and tolerates fenced/prose-wrapped replies before failing.
- **Same-day cache never hit live**: the fingerprint hashed the LLM-facing render
  whose ``{{as_of}}``/``{{now}}`` carry seconds — every re-run billed a fresh call and
  stored a duplicate card. The fingerprint now hashes a day-anchored second render
  (same-day identical data reuses; an intra-day data change still regenerates); the
  R4 anomaly snapshot fallback drops to day granularity (no same-day duplicates).
- Mid-run LLM failures were all reported as ``budget_exhausted_mid_run`` — the reason
  now carries the exception kind (+ message in ``detail``).
- AI text input was unusable beyond the example account: the parse prompt never
  listed valid account ids (the model invented ``charles_schwab``). It now carries the
  live account catalog, a ``<today>`` anchor (yearless dates resolve to the most
  recent PAST occurrence), and a multi-transaction example.
- Run rows showed a blank status while running and finished with a UTC stamp next to
  a +08:00 start; ``{}`` preflight bodies shadowed the saved combo into a bogus R3.

### Added
- **Official template library** (``llm_insight/official_templates.py``, versioned;
  ``GET /api/prompt-templates`` / ``POST /api/system-prompt/reset`` /
  ``POST /api/strategy-prompts/from-template``): system prompt v2 (timeliness-first,
  output structure, tags vocabulary, confidence calibration), 持倉週報 v2.1 and
  個股健檢 v2.1 (prediction spec, multi-account-total + FX-magnitude guards found by
  the live A/B). Fresh installs seed the official system prompt.
- **Master scoring rubric v2**: four weighted dimensions (direction 40 / citation 30
  / scenario 20 / timeliness 10), score anchors, an explicit miss definition,
  evidence-required notes — the narrative score is Loop-3's learning signal and had
  no rubric. Calibration safety lock now has a concrete 600-word cap + a timeliness
  rule; miss samples join the failed card's own claim/prediction/outcome.
- **Data diet**: ``price_history_json`` sends the last 30 sessions daily + every 5th
  beyond (checkup input −33%); ``fx_json`` carries an in-band unit note.
- ``settings-prompts``: the strategy-card section — still a design stub with local-only
  saves — is now fully wired to ``/api/strategy-prompts`` (load/save/toggle/archive/
  add) plus 重置回官方版 / 從官方模板庫新增 buttons. ``settings-llm``: the add-model
  drawer prefills the provider's public ``api_base`` and reuses the last same-provider
  model's context/output/timeout/retry settings.

### Changed
- Preflight G1 (unscheduled) now WARNS instead of hard-failing the verdict — manual
  triggering is a legitimate mode and the pipeline hub's trigger node already said
  warn (human sign-off 2026-07-05; supersedes the §7.2 fail).
- Deleting an insight task hides its historical cards/evaluations from
  ``/api/insights``, the dashboard embed, and ``/api/ai-score`` (rows stay in the
  tables — spec 4.1 archive semantics).

## [v0.1.9] - 2026-07-03

Mobile (iPhone) layout pass — layout only, zero functional change, verified at
390×844 across every page.

### Added
- **Mobile layer (≤760 px)**: the fixed 196 px sidebar becomes an off-canvas
  drawer behind a topbar hamburger (backdrop / nav-click / Esc closes; desktop
  collapse state neutralized inside the drawer; the inbox badge rides along);
  every multi-column grid collapses to one column (KPIs keep two); tables
  scroll inside their own wrap with a 640 px readable minimum so the page body
  never scrolls sideways; iOS ergonomics — 16 px inputs (kills the focus
  auto-zoom), 38–40 px touch targets, safe-area bottom padding, full-width
  modals/toasts/search overlay.
- Probe-driven overflow fixes: 幣別報酬折分 and datasources source tables get
  their own scroll regions; 5-tab segmented bars wrap; KPI sublines wrap inside
  the card; ECharts hosts clip until their resize catches up. Result: body
  horizontal overflow 0 px on 9/10 pages (dashboard 5 px sub-perceptual
  residue), down from 260–957 px. Desktop breakpoints untouched.

## [v0.1.8] - 2026-07-03

Round 6 (all 8 user-approved items): the system now manages MONEY, not just
stocks — per-account cash pools with a dedicated 資金管理 page — plus monthly
KPI snapshots, an inbox badge, and a handful of daily-flow refinements.

### Added
- **Cash pools (item 7, user spec)** — the fifth ledger: ``cash_movements``
  (入金/出金) + pure ``portfolio/cash.py`` balances per (account, currency):
  deposits − withdrawals ± FX sides ± trade settlements + cash-family dividend
  nets; opening inventory deliberately cash-neutral (record an initial deposit
  to balance history); operational view only — XIRR untouched. New 資金管理
  page manages all of it in one place (balance cards with negative-pool
  highlighting, deposit/withdraw + FX forms, movements ledger with
  edit/delete); dashboard gains a 各帳戶現金 mini panel; 換匯 entry moved here
  from 交易輸入 (one guarded door; CSV bulk path unchanged).
- **Negative-pool guard (item 2)** — the cash analog of the oversell guard:
  any entry / FX conversion / edit / delete that would drive a pool below zero
  answers 422 ``negative_cash`` (pure delta check, nothing written) until
  explicitly acked; live-verified (schwab pool −184,000 → covering deposit →
  conversion passed → USD pool math exact).
- **月度 KPI 快照 (item 8)** — nightly job upserts the current month's row
  (total value / return / rate / XIRR / by-currency); the value standing at
  month rollover IS the month-end record; ``GET /api/snapshots`` + a 月度成績
  dashboard panel (table lookup, no history replay).
- **Inbox sidebar badge (item 4)** — pending dividend count on the 交易帳本
  nav item, visible from every page.

### Changed
- 交易帳本 gains the 代號/名稱 search + date-range filters (item 1); the
  sector donut legend no longer overlaps the chart (item 3); trade input
  remembers the last-used account (item 5); one-step add offers a 記一筆買入
  handoff with the symbol prefilled (item 6 — the reverse direction, entering
  a trade for an unlisted symbol, already auto-registers since v0.1.4).

## [v0.1.7] - 2026-07-03

Round 5: the dividend inbox goes all-market and self-feeding — and booking a MY
dividend for real exposed (and fixed) a core rebuild crash that had been latent
since the schema was born.

### Added
- **All-market dividend detection** (R5 item 1): the inbox books per the
  ACCOUNT dividend model — TW cash (as before) · **US DRIP** with 30%
  withholding and an ESTIMATED reinvest (price = last stored close ≤ pay/ex
  date, shares = net/price; clearly marked and ledger-editable; without a
  stored price the item is not confirmable 缺再投資價) · **MY single-tier
  NET** · **TW 配股** (股票股利 X 元面額制 → held × X/10 zero-cost shares;
  cash+stock of one event are independent items with per-family ledger
  suppression). Live-verified with real yfinance events: NVDA DRIP (withhold
  1.50, reinvest 0.016 sh @ the real backfilled close) and Maybank NET 990
  booked on the test instance.
- **dividend_inbox_scan job** (R5 item 2): daily post-close sweep (runner seam —
  scheduler never imports api) refreshing events for acquired symbols and
  reporting the pending count in the run history (`… · 待確認 N 筆`), so the
  inbox grows by itself.

### Fixed
- **CORE: `DividendType.NET` crashed every rebuild** — bookable via CSV since
  the schema existed, but `cost_basis` routed every non-CASH type to the
  shares-branch ("requires reinvest_shares" ValueError → dashboard 500), and
  trend/XIRR silently dropped NET cashflows. Per domain-ledger.md, NET is
  cash-family (reduces adjusted cost, counts as an XIRR inflow): ONE definition
  (`shared.models.enums.CASH_DIVIDEND_TYPES`) now feeds all three replay sites;
  regression tests book a NET row end-to-end (dashboard + recompute stay 200).

## [v0.1.6] - 2026-07-03

Round 4 (user decisions on the round-3 report): the auto-import inbox becomes
real, history backfill gets position-aware windows + FX history, export audits
consolidate into the action log. Live-verified with REAL FinMind data (a genuine
TSMC dividend detected, confirmed, and booked on the test instance) before
promote.

### Added
- **FinMind 配息偵測 → 待確認匯入 for real** (decision A). Detection window per
  TW symbol = its earliest acquisition date (first BUY or opening build) →
  today; entitlement = shares held going INTO the ex-date (new dated
  ``holdings.shares_on``, strictly-before rule). Items suppress themselves when
  the dividend ledger already has a row within ±45 days of the ex-date or when
  explicitly skipped (fingerprint persisted); the pending list is computed on
  read — self-healing, nothing auto-written, 絕不自動入帳. CONFIRM recomputes
  server-side and writes a CASH row (TW model: net = gross) dated
  pay-date-else-ex-date. Endpoints: ``GET /api/dividend-inbox[?refresh=1]``
  (targeted FinMind sweep) + bulk ``confirm``/``skip``; inbox UI groups by
  symbol (collapsible, per-group 全部確認) with a 重新偵測 progress flow.
  v1 scope: TW cash (US DRIP needs broker data; MY a small extension;
  stock-only events excluded) — recorded as roadmap.
- **Smart backfill windows** (item 2): prices default 12 months, extended per
  symbol to its first acquisition date when older (watch-only symbols keep the
  default); NEW FX-history backfill (USD/TWD, USD/MYR, MYR/TWD via yfinance
  ``fetch_fx_history`` + registry/refresh seams) from the earliest ledger flow
  date — the trend chart and XIRR now have a rate on-or-before every flow
  (live-verified: the demo trend went from empty to 179 points). Registration
  initial window 92d → 365d; ``backfill-history`` with explicit ``days`` keeps a
  uniform window.

### Changed
- **Exports audit only in 系統操作記錄** (item 3): ``log_export_run`` removed;
  ``GET /api/scheduler/runs`` filters legacy ``export:*`` rows — the 排程執行歷史
  is a pure scheduler view again (double-recording gone).
- **Datasource connection test retries once** (item 5, 1.5 s spacing): transient
  TWSE/twstock probe failures from the VM stop tripping the health light; the
  fetch chain itself always degraded correctly.
- Ledger symbol/account editability stays as-is (item 4, decision A): guarded by
  registration requirement + oversell replay + the in-modal impact warning, with
  every correction traceable in the action log.

## [v0.1.5] - 2026-07-03

Round 3 (user-directed, 12 items): input-center completion, ops observability
(system action log + run-history sources), per-market quote routing made real,
full-field instrument editing, float-noise price caps — verified live on the test
instance (23 API checks + 16 browser steps + full-site screenshot review, all
green) before promote.

### Added
- **Single-entry 股利/換匯/期初 write for real** — the three forms build a
  one-row CSV and commit through the SAME tested `/api/import` preview+commit
  seam (error rows block with the backend reason; warn rows go through the ack
  confirm). 配股 mode relabels the amount field to 配股股數 and hides Net. The
  CSV dropzone is a REAL client-side file upload (FileReader → textarea →
  preview; drag-drop; per-kind column hints).
- **系統操作記錄 (system action log)** — an app middleware records every
  mutating `/api` call (timestamp, actor, Chinese action label, endpoint, HTTP
  outcome, duration; never bodies); `GET /api/system-log` + a third panel on the
  排程 page. Previews/what-ifs excluded; newest 5000 kept.
- **Run history names its sources** — job detail now reads
  `12 ok, 0 failed [yfinance: 0056, 2330, …] failed: 8299` (one `_summarize`
  seam covers every quote/history/dividend job); scheduler page shows Chinese
  job names with ids beneath, full detail on hover.
- **Per-market quote order, stored and REAL** — `data_source_market_order` +
  `PUT /api/datasources/market-order`, consumed by `default_registry(conn)`
  (scheduler crons, manual refresh, quick-add alike). Settings page: three
  market cards, drag to reorder, ✕ remove, ＋ add capable source, health dots
  from the source list. Live-verified: putting yfinance first made the next TW
  refresh answer entirely from yfinance. Supersedes the per-ACCOUNT fallback
  chains, which were stored/editable but consumed by NOTHING (and keyed on the
  wrong concept — accounts decide fees/dividends, markets decide quote routing);
  that endpoint + wire fields are removed.
- **Instruments full-field edit** — 名稱/產業/板別(TW dropdown)/ETF/目標價
  editable for ALL markets (US included); ledger search matches 代號 first then
  名稱; tx edit modal warns that changing 代號/帳戶 moves the row to another
  position.

### Fixed
- **幣別組成 rendered 權重 NaN%** for any currency holding 2+ positions —
  Decimal-string weights were summed with `+` (string concatenation). Ratios are
  display-only; now coerced explicitly.
- **PUT /api/instruments target_low null now CLEARS the alert** — exclude_none
  silently dropped explicit nulls, so clearing never worked (exclude_unset now).
- **Dividend CSV type normalization** — a lowercase `cash` was stored raw and
  poisoned `DividendType()` readers; the importer now uppercases and
  hard-rejects unknown types.
- **Float-noise cap at the price write seam** (human sign-off 2026-07-03) —
  prices capped at 4 dp, FX rates at 6 dp, ROUND_HALF_UP, cap-never-pad
  (yfinance float tails like `305.364990234375` no longer stored); recorded in
  `data-and-pricing.md`.
- The AI-input design state-switcher is retired (degraded panels driven only by
  real API errors, doubling as usage-time hints when AI is enabled later); the
  AI screenshot dropzone honestly reports Vision is not yet wired.

## [v0.1.4] - 2026-07-02

Position-management UX round 2 (user-directed): one-step onboarding, ledger row
corrections, app-wide progress visibility, deploy build identity — plus three real
bugs the new tests/live-verification forced out. All changes verified on the live
test instance with real providers (16 API checks + 18 real-browser click-through
steps, all green) before promote.

### Added
- **One-step instrument add** — `POST /api/instruments/quick` (new shared
  `api/instrument_service.py`): probes the TW board, **requires a real fetched
  quote** before registering (typo guard; 422 `quote_not_found` → explicit
  user-confirmed `force` retry), auto-fills the display name (TW: twstock static
  code table → yfinance fallback; US/MY: yfinance), and **backfills ~92 days of
  daily closes** so the symbol drawer chart renders immediately. 觀察清單 add UI
  collapses probe→confirm→detail-form into symbol + market + one button (verified
  live: `2884` → 玉山金, TWSE 上市, 現價 33.70, history backfilled).
- **Trade input auto-registers unknown symbols** — the manual commit infers the
  market from the account's settlement currency (TWD→TW / USD→US / MYR→MY) and
  runs the same quick-register (same real-quote guard); an unregistrable symbol
  still writes **nothing** (400 `symbol_auto_register_failed`). Preview shows an
  info-severity note instead of a hard error; success responses carry
  `auto_registered {symbol, name, last}`.
- **Ledger row corrections** — `PUT/DELETE /api/ledgers/{transactions,dividends,fx}/{id}`
  and `/ledgers/openings/{account}/{symbol}` ("append-only in spirit": explicit
  user corrections, never silent mutation). Every mutation **replays the would-be
  ledger through build_book first**; a correction that would strand a later sell
  answers 422 `oversell` until explicitly acked (dashboard then shows the flagged
  賣超 state). Frontend rows gain 編輯/刪除 with per-kind modals + danger confirms.
- **Progress system (app-wide)** — `web/api.js` (the single fetch seam) tracks all
  in-flight requests and drives a global top progress bar (150 ms anti-flicker);
  `pdBusy()` spinner/disabled states on action buttons (double-click-safe);
  `toastProgress()` persistent toasts for long operations (更新報價 / 重算 /
  歷史回補) — no network wait can look frozen anymore.
- **Build identity** — `GET /api/health` now reports `{version, commit, release}`
  (short git hash + exact tag on HEAD or `"unreleased"`, via new
  `shared/buildinfo.py`; env-overridable, never raises). Sidebar shows
  `vX.Y.Z · hash` on every page with an amber 未發行 marker for non-tag builds;
  settings 一般 row carries the full string; `verify_live.py --expect-release`
  asserts a prod promote runs the tag.
- **`POST /api/actions/backfill-history {days}`** + 觀察清單「回補 3 個月歷史」
  button — gives existing instruments the 3-month chart window (new registrations
  get it automatically).

### Fixed
- **Trend replay could 500 the dashboard on an acked-oversold ledger** —
  `timeseries.daily_value_series` built its per-day books without
  `allow_oversell`, bypassing the 2026-06-18 degradation the main book already
  had. Now degrades: an oversold day marks the trend point `incomplete` instead of
  raising. (Never-500 degradations must cover EVERY replay call site — LESSONS.)
- **False oversell warnings for opening-backed positions** —
  `data_ingestion.holdings.current_shares` summed only the transactions table,
  ignoring opening inventory (期初) and stock/DRIP dividend shares; selling such a
  position raised bogus 賣超 warnings and `held` flags undercounted. Now counts
  all four share sources (same replay rule as `build_book`).
- **twstock was mis-grouped as a `[probe]` extra** — deployed venvs never had it,
  silently disabling both the TW quote-chain twstock fallback AND the TW name
  lookup (found live: names came back empty). Moved to runtime dependencies;
  `lookup_name` now degrades per-source (twstock failure still falls through to
  yfinance).
- **重新探測 now persists** — it probed and toasted but never saved, so an
  unresolved TW board stayed unresolved forever; it now PUTs the result and
  resolves `board_status`.
- **Input-center design-stub prefill retired** — the form booted with fake
  2026-06-11 / 2330 / 1000 / 612.5 values; now today + empty fields with a neutral
  pristine state (no red errors on an untouched form).

### Changed
- Classic `POST /api/instruments` delegates to the shared quick-register service
  with `force=true` (back-compatible: a provider outage never blocks an explicit
  registration) and gains the same name auto-fill + history backfill.

## [v0.1.3] - 2026-07-02

### Fixed
- **Core position management stabilized (2026-07-02)** — root-caused from live-prod evidence
  (registered symbol stuck on a stale close, zero successful ledger writes):
  - **Topbar 更新報價/重算 wired for real** (`web/shell.js`): both buttons were design-preview
    stubs (success toast, no API call) since v0.1.0 even though `POST /api/actions/refresh-quotes`
    and `/api/actions/recompute` existed and were tested. Now: busy-guarded call, result toast,
    auto-reload. On-demand quote freshness is restored (each market's cron remains the scheduled
    path). Verified by a real browser click against the live test site (~10 s for all 3 markets).
  - **Unregistered symbol is a HARD block at commit** (manual + CSV): `symbol_unresolved` was a
    soft issue that `confirm=True` bypassed, so a trade for a never-registered symbol could enter
    the ledger where it could never be priced (`build_worklist` reads `instruments`) and crashed
    `GET /api/dashboard` with a bare `KeyError` (same bug class as the acked-oversell 500).
    Commit now returns 400 with a register-first message; the existing sev=error frontend gate
    disables the commit button automatically.
  - **Dashboard never 500s over legacy unregistered rows:** their events are excluded from ALL
    computation (book, XIRR, trend, dividend summary — consistently) and surfaced in
    `freshness.unregistered_symbols`; `web/app.js` renders a warning banner with a register link.
    `POST /api/actions/recompute` pre-checks and returns 422 (was a KeyError 500).
  - **Cmd+K search reads the real registry:** the hardcoded 9-symbol design mock in `shell.js` is
    retired; search lazy-loads `GET /api/instruments` (cached, degrades to the register hint).

### Added
- **Instant first quote on registration:** `POST /api/instruments` now fetches the new symbol's
  latest quote (+ reporting FX pairs) immediately via `scheduler.jobs.refresh_instrument_quote`
  — best-effort, never fails the registration — so a newly added stock is priced right away
  instead of waiting for its market's post-close cron. Verified live: registering 2603 returned
  `last=185.50` (real TWSE close) in the same request.

### Changed
- Golden dashboard payload regenerated: `freshness` gains `unregistered_symbols` (all numeric
  values byte-identical; `by_currency` key order in the file is serialization noise only).

## [v0.1.2] - 2026-07-02

### Fixed
- **Data-source connection tests wired (2026-07-02):** the settings → 資料來源 "測試" buttons now
  run a real minimal probe for the primary live sources — `yfinance` (AAPL), `twse` (2330), `tpex`
  (5347 OTC), and `finmind` (a keyed dividend request) — instead of returning the neutral
  `尚未實作連線測試` stub. `fx_ecb` is reclassified `pending` (it has no adapter — the FX path is
  yfinance), so it honestly shows 待測試 rather than the stub. A regression test asserts that no
  `live` source can fall through to the not-implemented fallback. Verified against the live VM (all
  three 官方 sources reachable from the deploy IP; the FinMind key returns data). Real quote fetching
  was never affected — only the diagnostic button was unwired.

### Added
- **App version display + `/api/health` version (2026-07-02):** a single source of truth,
  `portfolio_dash.__version__`, now (a) drives the packaging version via pyproject `dynamic` version,
  (b) is served by the open `GET /api/health` as `{"status":"ok","version":"…"}` — a quick post-deploy
  check (`curl -s …/api/health`), and (c) is displayed in the UI: a version tag under the sidebar
  `portfolio-dash` brand (every page) and the settings → 帳戶與費率 → 一般 (唯讀) row. `web/shell.js`
  fetches `/api/health` once and fills both, so the two displays share the one source.

### Changed
- **mypy strict baseline restored to clean (chore, 2026-07-02):** the type gate had accumulated 65
  pre-existing errors, all in `tests/` (production code was clean). Fixed the real ones — missing
  parameter annotations, `dict`→`dict[str, Any]` generics, a Protocol parameter-name mismatch,
  `Page`/`Writer` argument types, two stale `# type: ignore`s, a `dict.__setitem__` value-context
  hack — and relaxed only `no_implicit_reexport` for test/monkeypatched modules (it adds no value
  there); also dropped the now-unused FinMind/freezegun `ignore_missing_imports`. `mypy --strict` is
  green across all source files again.

## [v0.1.1] - 2026-06-19

### Fixed
- **First-run bootstrap completeness — fresh 0-byte DB (2026-06-19):** the app lifespan now creates EVERY
  table the running app reads AND seeds the broker accounts, so a brand-new install works out of the box.
  `_lifespan` previously omitted `create_pricing_tables` (`prices`/`fx_rates`), `datasources_store.ensure_seeded`
  (`data_sources`/tiers/health), and `seed_accounts` — an empty DB looked fine (no holdings → no price query),
  but the FIRST transaction made `GET /api/dashboard` 500 with `no such table: prices`, and with zero accounts
  no trade could be entered at all (there is no add-account UI in v0.1.0). The bug hid because the whole test
  suite seeds via the harness (`init_golden_base`), never the real boot path. Accounts seed from the single
  canonical `DEFAULT_ACCOUNTS` (idempotent upsert — add a future account there and it auto-seeds next launch;
  when an add/edit-account UI lands, switch to a settings_meta-gated seed-once so launches don't clobber edits).
  New `tests/contract/test_first_run_bootstrap.py` drives `create_app()` through its REAL lifespan against a
  throwaway DB (table creation + account seed + a holding must not 500 the dashboard). All bootstrap steps are
  idempotent (`CREATE TABLE IF NOT EXISTS` / `ON CONFLICT`), safe to re-run on an existing DB.

## [v0.1.0] - 2026-06-19

### Added
- **Frontend wiring foundation — spec 19 Phase 0 (2026-06-16):** the static `web/` frontend's single
  fetch layer + a Playwright smoke harness, landed ahead of per-page wiring.
  - **`web/api.js` (`window.pdApi`, spec 19.1):** the ONE fetch seam — `{get, post, put, del, download,
    abortable}` + `window.PdApiError`. Parses the `api/errors.py` envelope `{error:{code,message,field,
    issues}}` into a structured `PdApiError`; **401** → `window.location.replace('login.html')` (the single
    redirect site) then throws; **402/409/503** → rethrow with NO redirect (the AI block renders a degraded
    state); response Decimal **strings pass through untouched** (no `parseFloat`/`Number`/`+` — the frontend
    never computes money); `credentials:'same-origin'` (carries `pd_session`); `abortable(key)` cancels a
    prior same-key in-flight request. No page calls `fetch` directly.
  - **Playwright smoke harness (`tests/e2e/conftest.py`, reuses the spec-17 golden seed):** a subprocess
    uvicorn serves the real `create_app()` (StaticFiles `web/` + `/api/*`) against an on-disk golden DB
    (DRY-reuse of `tests/conftest.py::_seed_golden`); headless chromium drives it; reusable
    `assert_page_ok(page, base_url, path, root_selector="body")` asserts **zero console errors + zero uncaught
    pageerrors** (catches Decimal-string `.toFixed` TypeErrors once pages bind to `/api`). The global
    `--disable-socket` ban is re-enabled **for loopback only, scoped to `tests/e2e`** (autouse
    `_e2e_loopback_socket`, restored on teardown) — external network stays banned. Baseline smokes for
    `login.html` + `index.html`; per-page smokes are added by Phase 2. (`playwright>=1.44` was already a
    declared dep; raw `playwright.sync_api` used — no `pytest-playwright` added.)
- **Backend completeness — spec 19 Phase 1 (2026-06-16):** ops/observability + dashboard-completeness so
  Phase-2 pages wire against a complete backend.
  - **Ops 保全 (spec 19.3):** new leaf `portfolio_dash/ops/backup.py` — `backup_database` (sqlite3 online
    `.backup` API → gzip → `data/backups/portfolio_{YYYY-MM-DD}.db.gz`, keep-30 rotation), `check_integrity`
    (`PRAGMA integrity_check`), `pre_write_snapshot` (prefixed one-off snapshots for CSV/AI commit + migrations).
    `scheduler/jobs.py` `backup_daily` job (default 01:30 Asia/Taipei): integrity-fail → error run + structured
    warn; logs recovery after a 3-consecutive-fail streak. Pairs with the Phase-0 `make restore` target.
  - **`/api/dashboard` freshness `last_backup_at` (spec 19.3):** `ops.backup.latest_backup_at()` (newest backup
    mtime as a UTC ISO string, or None); `FreshnessReport.last_backup_at`; router-fed after `to_wire`
    (build_dashboard stays pure).
  - **Structured JSON-lines logging (spec 19.4):** new leaf `shared/logging_config.py` (`JsonLinesFormatter`
    + idempotent `configure_logging`, RotatingFileHandler 10 MB×5 → `data/logs/app.log`), configured in the app
    lifespan; a catch-all `Exception` handler in `api/errors.py` logs the traceback + returns the generic 500
    envelope (no detail leak); one `llm_usage` structured log point in `shared/llm.py` (alias/tokens/cost,
    reconciled with the `llm_usage` row). stdlib only.
  - **`calib_gap` alert rule (spec 03/04 I1):** `AlertRules.calib_gap` (default **15 pp**, not a ratio); the
    pure `compute_alerts_from`/`compute_alerts` gain a fed `calib_gap: Decimal | None`; `evaluations_store.
    scored_confidence_hits` + the SINGLE-SOURCE `api/insight_service.calibration_gap(conn)` (global `min_samples`
    gate → `scoring.calibration_error`, in pp) feed BOTH the dashboard embed and `GET /api/alerts` (they cannot
    diverge). `calibration_regression` stays an `alert_events` event, not surfaced here. (`evolution_config.
    gap_alert_pp` is the separate spec-04c regression threshold — NOT this rule's threshold.)
  - **Dashboard embeds latest N real insight cards (spec 08/04 I3):** `insights_store.latest_cards(conn, n)`
    (`is_shadow=0`, newest-first, LIMIT n); the router overwrites `payload["insights"]` after `to_wire` with the
    latest 3 as `{id, title, summary, body_md, symbol, created_at, cost_usd}` (cost_usd stays the canonical
    Decimal string; empty table → `[]`). NOTE the field names differ from the older `web/mock-data.js` insight
    shape — reconciled when Phase 2 wires the dashboard page.
- **spec-17 full-stack regression — financial golden verification + E2E user flows (2026-06-17):**
  the final acceptance pass over the wired full stack.
  - **Multi-stock financial verification (`tests/contract/test_spec17_financials.py`, spec-17 §17.2):**
    a rich 8-instrument / 4-account / 3-currency scenario (`seed_full`) seeded through the REAL write paths
    and driven through `GET /api/dashboard`, asserted against **independent first-principles oracles** derived
    from `rules/domain-ledger.md` (NOT by re-calling the calc core). Covers weighted-average cost (2330), TW
    cash-dividend cost-reduction (2330), partial-sell realized P&L (0056), 配股 stock dividend (2603),
    missing-price degradation + XIRR all-or-nothing (00919), US DRIP $0-cost reinvest with 30% withholding
    (AAPL), age-stale-but-valued price (MSFT), MY cash dividend + 3-dp price fidelity (1155.KL), the
    cross-currency reporting blend at spot, and **invariant #6 — FX gain/loss is an attribution of the
    reporting total, never added on top** (`total_return == realized + unrealized`; realized FX 2,000 TWD
    hand-verified). A frozen `tests/golden/dashboard_full.json` snapshot (regenerated deliberately via
    `scripts/regen_golden_full.py`) pins the whole payload for regression (spec-17 §17.6.1). New reusable
    `tests/conftest.py::dashboard_client_factory` (+ extracted `init_golden_base`) builds a TestClient over a
    fresh, custom-seeded golden-base DB; the fixed subset `golden_db` (and the 1067 tests on it) are untouched.
  - **E2E user flows (`tests/e2e/test_flows_e1_e10.py`, spec-17 §17.5):** Playwright against per-flow ISOLATED
    uvicorn subprocesses (new `tests/e2e/conftest.py::flow_server` factory + `fresh_page` isolated context) so
    write/auth flows are order-independent. E1 dashboard (golden KPIs + 00919 缺價 badge + asof/stale chip), E2
    manual buy commit (form → preview → confirm 201 → position grows 1000→2000 in the API), E4 oversell soft
    warning (ack gates the confirm button, then writable), E6 login loop (protected mode: wrong pass 401 stays
    on /login.html → correct → dashboard). Expect-polling only, no sleeps (§17.7.4). Harness robustness
    added during a senior full-stack review (the suite is green — exit 0 — every run; these prevent rare
    real infra races, NOT a failing assertion): 60s readiness + Playwright ceilings (not 30s) absorb
    Windows subprocess cold-start contention (one genuine TimeoutError seen under review load);
    `flow_server` retries the spawn with a fresh port on early-exit (the `_free_port` bind→release→spawn
    TOCTOU race, amplified by spawning a server per flow); best-effort `fresh_page` / `_terminate`
    teardown so a passed test never errors on Playwright/subprocess cleanup. NOTE: the benign captured
    log `asyncio: Task was destroyed but it is pending!` (Playwright `Page._on_route` GC at close) is
    NOT a failure and only shows under `-rA`/`-rE`, never under the `-q` gate (see LESSONS_LEARNED).

### Fixed
- **Deterministic `/api/dashboard` freshness ordering (spec-17 regression, 2026-06-17):** `freshness.fx` and
  `freshness.missing_fx` iterated `RateResolver.reads`, whose order derives from set iteration over quote
  currencies (PYTHONHASHSEED-dependent across processes) — so the API list order was non-deterministic and a
  golden snapshot flapped between runs. `portfolio/dashboard.py` now sorts both by `(base, quote)`. (`prices`
  was already stable via `sorted(held_symbols)`.)
- **Oversold (賣超) ledger no longer 500s the dashboard (2026-06-18, human sign-off — lightweight, NOT short
  accounting):** an acked oversell (`POST /api/input/manual/commit` `side=sell` qty>held + `ack_oversell=true`)
  writes a sell exceeding holdings; the NEXT `GET /api/dashboard` then crashed (`build_book` raised
  `OversellError`, uncaught → 500). Surfaced by the spec-17 regression. Fix: `build_book(allow_oversell=True)`
  (the dashboard path) DEGRADES GRACEFULLY — nets the position to negative shares, drops its now-undefined cost
  basis, emits no realized row; the holding is flagged `oversold` with 待釐清 (null) value/P&L and is **excluded
  from portfolio aggregates** (auto via the existing `market_value is not None` gates). XIRR degrades to None
  with a reason when any position is oversold. The 重算/rebuild action (`actions.py`) and all input-time
  oversell warnings (preview/whatif detect it independently) are unchanged — `build_book` still raises by
  default. `Holding`/`HoldingRow` gain an `oversold` flag; the holdings table renders a **賣超** badge +
  tooltip prompting the user to record the missing opening inventory / buy. New
  `tests/contract/test_oversell_graceful.py` + an e2e display flow; full short-position accounting is
  deliberately out of scope (it would invert cost basis, dividend direction, weights/allocation/XIRR — over
  scope for a 1–2-user long-only tracker; revisit only if real short trades are needed).
- **`/api/health` exempt from the protected-mode auth gate (2026-06-17, human-approved):** the liveness probe is
  added to `auth_store._OPEN_PATHS` (alongside `/api/auth/login` + `/api/auth/session`). It returns only
  `{"status":"ok"}` (no data), so it must answer regardless of login — previously, once ≥1 user existed (protected
  mode) an unauthenticated Docker/k8s/monitoring liveness probe got a 401. Every OTHER `/api/*` path still requires a
  session in protected mode (regression test pins protected `/api/health`→200 AND `/api/dashboard`→401).
- **Makefile runs the full suite (spec 19 Phase 0, 2026-06-16):** `make test`/`make regress`/`make all` now
  run `pytest tests --ignore=tests/e2e` (the whole tree minus browser e2e) — previously `make test` targeted
  only `tests/unit tests/contract`, collecting **266 of 1012** tests, so `make all` was not real regression.
  `make e2e` is the explicit Playwright gate; the `e2e` pytest marker is registered in `pyproject.toml`.
  Added a guarded `make restore FILE=... [DB=...]` ops target (copies a backup over the live SQLite DB at
  `data/portfolio.db`).
- **Atomic batch import (#1 backend hardening, 2026-06-15):** CSV/broker batch import is now
  all-or-nothing on an unexpected error. `data_ingestion/preview.commit_preview` previously looped
  accepted rows calling writers that each `conn.commit()` per row, so a mid-batch unexpected exception
  left a partial ledger (rows 1..k committed, the rest not) — breaking 重算/append-only reproducibility.
  Now the writer loop runs in ONE transaction (a `commit: bool` param threaded through the four batch
  store inserts; batch passes `commit=False`), commits once at the end, and `rollback()`s + re-raises on
  any exception. The single-row/manual path is unchanged (default `commit=True`); intentional skips of
  hard-issue rows stay contract-level partial success (not a rollback trigger). New
  `tests/data_ingestion/test_preview_atomicity.py`.
- **pricing→data_ingestion cross-peer import removed (#2 layering, 2026-06-15):**
  `pricing/datasources_store.py` no longer imports `data_ingestion.config_seed.DEFAULT_ACCOUNTS`
  (architecture.md: pricing and data_ingestion are sibling lower layers). It now iterates the file's own
  local `_ACCOUNT_MARKET` map (already enumerating the 4 accounts) — byte-equivalent fallback-chain
  seeding. New `tests/pricing/test_layering.py` AST-guards that `pricing/**` imports no `data_ingestion`.

### Changed
- **Renamed `web/AI Pipeline Hub.html` → `web/pipeline-hub.html` (2026-06-19):** the only frontend page
  whose filename had spaces + Title Case, out of step with the lowercase-hyphenated convention
  (`index.html`, `settings-scheduler.html`, …). `git mv` + updated all LIVE references —
  `web/shell.js` (sidebar nav), `web/alerts.js` (`/pipeline` href map), `web/settings-prompts.html`
  (cross-link), and the e2e smoke (`/pipeline-hub.html`, dropping the `%20` URL-encoding). The frozen
  `docs/design-handoff/` export bundle (its own `AI Pipeline Hub.html` + shell.js + spec-07 reference)
  is left untouched — it is a self-consistent historical snapshot, not the served app.
- **spec 19 deferred follow-ups resolved (2026-06-16):** ① the 自我進化設定 panel is wired to `GET/PUT
  /api/evolution-config` (read-then-PUT preserves the non-panel knobs `horizon_basis`/`defer_limit_days`/
  `shadow_on_alert`; `gap_alert_pp` sent as a Decimal string; the `localStorage pd_evolution_cfg` path removed);
  ② removed the dead `window.PD_HISTORY` trend trade-marker code in `charts.js` (the E8 large-trade markers had no
  backend source for the portfolio-level trend after the mock deletion); ③ `rebalance.js` now derives trades/fees via
  the authoritative `POST /api/rebalance/preview` (debounced + `pdApi.abortable`) instead of a client-side estimate —
  the module computes NO money (`FX_TWD`/`pdFeeTax`-call/lot-snapping/turnover removed); ④ `api.js` `download()`'s
  401-redirect now carries the same `!endsWith('login.html')` guard as `_handle`; ⑤ `prompts.py` registry docstring
  26→29; ⑥ added `web/favicon.svg` (+ a `shell.js`-injected `<link>` and a login.html `<link>`) to retire the app-wide
  `/favicon.ico` 404. Each fix shipped with a per-change senior review + page smoke + an E2E Playwright flow
  (evolution-config round-trip, trend-chart mount, rebalance-preview round-trip, favicon presence). Suite now
  **1067 passed / 3 skipped + 33 e2e**.
- **Frontend wired to the live API — spec 19 Phase 2 (page wiring) + Phase 3 (cleanup) (2026-06-16):** every
  static `web/` page now consumes the real `/api/*` through the single `window.pdApi` fetch layer; ALL mock-data
  globals are retired and the mock FILES deleted. No framework, no build step (decision B). Per page (each: mock →
  `pdApi`, money via `fmt.*` [Decimal strings, never client-computed], async boot, Playwright page-smoke):
  - **shell.js** — async `GET /api/auth/session` guard (guest / signed-in / signed-out→`login.html`), replacing the
    localStorage guard; sync globals (`toast`/`confirmDialog`/`pdOpenSymbol`/search/nav) preserved; logout/lock via pdApi.
  - **dashboard** (index/app.js + charts.js + alerts.js) — one shared `window.pdDashboard = pdApi.get('/api/dashboard')`
    promise consumed by all three; sparkline from `spark_30d`; insight cards from the real `{summary,body_md,created_at,
    cost_usd}` shape; alert `href` mapped to static routes; the embedded `alerts`/`llm_quota` rendered (no client recompute).
  - **symbol detail drawer** — `GET /api/symbol/{symbol}/detail` + the shared dashboard promise; feeTax offline mirror
    kept (documented exception); 合計 consumes backend `unrealized_pnl` (no client money-sum).
  - **ledger** — `GET /api/ledgers/*` (implied_rate from the backend; account filter keys on `account_id`).
  - **instruments** — `GET /api/instruments` + probe/register/edit (`POST /probe`, `POST/PUT /instruments`).
  - **input center** — `GET /api/input/context` + manual/CSV/AI preview+commit (oversell + warnings ack-confirm flows);
    manual dividend/FX/opening forms are design-stage (no single-entry endpoint — CSV import is the path).
  - **settings** — LLM (`/api/llm/config`), scheduler (`/api/scheduler/jobs`+`/runs`), datasources (`/api/datasources`),
    prompts + vars (`/api/system-prompt`, `/api/prompt-vars`, `/api/prompts/{preview,test}`), users (`/api/users`),
    alert-rules editor (`GET/PUT /api/alert-rules`). Fixed the C2 bare-`.toFixed` money sites + war-game Finding 8
    (`cost_usd == null` nil-check). Retired the shell `setSession` transitional shim.
  - **alerts.js (I1)** — off-dashboard pages now read `GET /api/alerts` (bell) + `GET /api/llm/config` (quota chip);
    the client-side rule-compute orphan removed.
  - **login.html** — `POST /api/auth/login` (cookie session); api.js's 401-redirect is suppressed ON `login.html` so a
    wrong-password 401 surfaces in the form instead of self-reloading.
  - **insights + AI Pipeline Hub** — `/api/insights`, `/api/ai-score`, `/api/insight-tasks/{status,preflight,diagnose,
    runs}`, `/api/calibrations`; folded the 07 watch-items (`'off'`→`'idle'`, `fix.kind`→one-click buttons,
    `recent_skips` reason labels, calibration version chain).
  - **Phase 3 cleanup** — wired rebalance.js to the shared `/api/dashboard`; DELETED the 4 mock files
    (`mock-data.js`/`history-mock.js`/`input-mock-data.js`/`pipeline-data.js`); added `tests/contract/test_web_pdapi_only.py`
    asserting **no `web/*.js` except `api.js` calls `fetch(` directly** (single-fetch-layer guardrail, spec 19 §6).
  - **Backend fix exposed by the real-server e2e:** `shared/db.py` now opens the SQLite connection with
    `check_same_thread=False` — FastAPI runs the sync `get_conn` dependency in an anyio threadpool, so a per-request
    connection can be created on one worker thread and closed on another (no concurrent use); the default same-thread
    guard wrongly raised on close → a 500 under the real subprocess server (the in-process TestClient never hit it).
    Guarded by a cross-thread regression test.
  - **Test harness:** the Playwright smoke harness (spec 17 seed) now guards every wired page + key interactions
    (drawer, account filter, input preview, instrument probe, rebalance drawer, alert bell, login) — 29 e2e smokes,
    zero console/page errors per page. Suite: 1009 → **1067 passed / 3 skipped**.
  - **Deferred (tracked follow-ups, none ship-blocking for 1–2 users):** wire the 進化設定 panel to `GET/PUT
    /api/evolution-config` (backend already implemented; panel still uses localStorage); the dashboard trend's
    trade-event markers no longer render (`charts.js` `window.PD_HISTORY` is now dead code after the mock deletion —
    remove or source from `/api/dashboard` trend); rebalance.js authoritative result could use `POST /api/rebalance/preview`
    (currently a documented client what-if estimate); `prompts.py` docstring says "26 variables" (registry is 29).
- **Money/Decimal wire-string unification (#2c/M1 foundation hardening, 2026-06-15):** every Decimal
  now serializes to the JSON wire in ONE canonical form — `format(d, "f")` (fixed-point, full source
  precision, trailing zeros preserved, **never scientific notation**) — identical to the DB form
  (`money.to_db`). New `shared/wire.decimal_str`; `to_wire`'s Decimal branch routes through it (was
  `str(Decimal)`, which could emit `1E-7`-style sci-notation); `money.to_db` delegates to it
  (byte-identical, float/non-finite guards kept). All direct `str(<Decimal>)` wire bypasses migrated to
  the canonical encoder across `api/wire.py` + routers (dashboard `spark_30d`/`llm_quota`, input_center
  [**`_money_str`/`normalize()` removed**], symbol, ledgers, llm_settings, instruments, strategy,
  prompts, insights) and `api/insight_service.py`; `str()` on ints/ids/enums left untouched. Done
  **before frontend wiring** so the UI binds to a stable money format and formats for display itself
  (full precision stays on the wire; quantize only at display, per `data-and-pricing.md`). One spec-17
  golden value changed (a trailing zero now preserved: `612500.0`, not `612500`); spec-18 round-trip +
  a no-scientific-notation guard added. (+21 tests; 980 → 1001 passed.)
- **LLM budget model — single topup-cumulative (2026-06-13, human sign-off; senior-review
  finding I-1):** the USD budget is now one number — `budget_remaining = Σ top-ups − Σ usage`
  (`shared/llm_config`). `remaining <= 0` blocks (`check_budget` raises `LLMBudgetExceeded`),
  so an unfunded/$0 account is blocked even when fully configured; exhaustion coincides exactly
  with `Σ top-ups == Σ usage`. Top-ups ADD cumulatively (no reset). The gate, settings page
  (`GET /api/llm/config` `quota.remaining_usd`), dashboard chip (`GET /api/dashboard`
  `llm_quota.remaining_usd`), and the spec-16 `quota_remaining` alias all read this single value
  (`reset_budget` removed; `quota_remaining` delegates to `budget_remaining`). **Supersedes the
  earlier append-only "reset ledger" model** (remaining = latest reset − Σ usage since that reset;
  unset = no cap). End-to-end reconciliation proof: `tests/contract/test_quota_accounting.py`.
- **Web-layer architecture decision — option (B) (2026-06-13, human sign-off):** the web
  layer is now a **FastAPI JSON API (`portfolio_dash/api/*`) + a static vanilla-JS frontend
  (`web/`)**, superseding the originally-locked **Jinja2 + HTMX server-rendering** (CLAUDE.md
  locked decision #1 "no frontend/backend split / no JSON contract"; `stack.md`;
  `design-handoff.md` "convert to Jinja2 templates"). Rationale: the Claude-Design export is
  vanilla JS + ECharts CDN with **no framework and no build step** (the stack-drift guardrail
  is honored) and pushes **all computation to the backend** (the web layer still does not
  compute — invariant #4 intent preserved). The trade-off ("single codebase / no contract to
  drift") is mitigated by `mock-data.js` as the version-controlled contract and spec-17 golden
  payload + spec-18.4 string-serialization round-trip tests. Net upside: the JSON contract makes
  the automated regression loop machine-diffable (stronger than HTML-fragment assertions).
  `CLAUDE.md`/`stack.md` web rows to be amended; the HANDOFF.md CLAUDE.md template is
  **reconciled, not applied verbatim** (locked accounting/ledger/process rules preserved).
  Full reconciliation: `docs/design/spec-reconciliation-2026-06-13.md`.
- **Scope expansions adopted from design-handoff specs 01–19 (2026-06-13, human sign-off):**
  a new `api/` HTTP layer (08/19); `strategy/` alerts rule-engine + what-if + rebalance as
  pure functions (03, with config-row editable thresholds — a narrow, bounded step toward
  user-editable rules, explicitly NOT a DSL); a full `llm_insight/` self-evolution system —
  insight composers, calibration version chains, backtest scoring, a new `master` LLM role
  (04, far beyond the prior "batch insight cards"; invariant #1 preserved — quant hits are
  code, the LLM only writes narrative/calibration text); external-data ingest + an append-only
  `external_snapshots` store (06: FinMind chips/fundamentals/valuation, VIX, Fear&Greed,
  indices); auth/users via stdlib `hashlib.scrypt` (09, no new dependency); a full test/
  regression harness — `make all`, golden dataset, FastAPI TestClient contract tests,
  Playwright E2E, hypothesis/mutmut, pytest-socket network ban (17/18); SQLite backup/restore
  + structured logging (19). Schema migrations (additive, via `_add_column_if_missing` /
  `config_store`): `instruments += target_low/board_status/is_etf`, `transactions +=
  fee_snapshot`, `schedule_config += kind/payload`, `job_runs += payload/reason/cost_usd`, plus
  new tables for auth/datasources/external-snapshots/insight. Enum extensions: `DividendType +=
  NET`, `LLMRole += MASTER/MASTER_FALLBACK`. `FeeRuleSet` structural fixes (flat_fee, US/MY
  min_fee, stamp_duty_rate+cap) and US/MY fee-rate backfill (spec 18.0, pending real-statement
  confirmation). Build order in the reconciliation doc §6.
- **Accounting model decision (2026-06-06, human sign-off):** P&L now uses the
  adjusted-cost model — cash dividends fold into cost (no separate dividend-income line),
  realized/unrealized computed vs `adjusted_cost`; `original_cost` retained for the
  return-rate denominator and the capital-gain-vs-dividend split. Supersedes the prior
  original-cost-plus-separate-dividend rule in `domain-ledger.md`. The no-double-count
  principle is preserved (dividends still counted exactly once). Return-rate denominator
  stays original invested cost; cost basis is all-in (incl. buy fees+tax).

### Added
- **Insight pipeline-hub UX — status / preflight / diagnose (spec 07, Phase 4 — the observability
  layer):** read-only convergence over the spec-04 machinery — NO new tables, NO LLM calls, NO new
  business logic (03/04/06 reused). `GET /api/insight-tasks/status` returns a single source of truth:
  health (master_ok, quota_remaining, last_batch) + per-task 5-node states (trigger/input/assemble/exec/
  output, §7.1.1 derivation) aggregated to a level — the pure `llm_insight/pipeline_status.py`
  `derive_node_states` over facts gathered in `api/insight_service.py` (schedule_config, resolved
  universe, **reused dashboard freshness**, templates, budget/quota_low/master, last non-shadow run).
  `POST /api/insight-tasks/{id}/preflight` (also a draft `body` for the wizard's check-before-create) is
  a zero-cost dry run that calls the **SAME `gating.evaluate_gates` as execution** (the §7.2 hard rule —
  no "preflight passed, run failed"; asserted via a spy + an end-to-end demo) wrapped with G0/G1/G7,
  returns ordered gates + verdict (blocked/degraded/clean) + the spec-06 assembled preview + `fix.kind`
  one-click hints — never calls the LLM, never writes job_runs/llm_usage. `GET …/diagnose` adds
  first_blocker + recent_skips (single-enum reasons); `GET …/runs` is the task-view job_runs (is_shadow
  excluded). §7.0 naming: `/api/insight-tasks/*` is a full **alias of the same resource** as
  `/api/insight-types/*` (one `_dual` route registration, no logic duplication; old routes + table names
  kept). Senior review: APPROVE-WITH-NITS → fixed the R6 (quota) gate emitting a wrong `create_schedule`
  fix (quota has no one-click action in the enum). The 3 §7.6 failure demos reproduced. **This completes
  the 04→07 insight chain backend.** (+48 tests; 932 → 980 passed.)
- **AI self-evolution / Loop Engineering (spec 04, Phase 4 — the four-self loop):** the
  insight-composer + generation + backtest + calibration + shadow-promote system, built in three
  sub-phases (04a design/CRUD, 04b generation, 04c evolution) under the §4.10 locked decisions
  (mechanism reviewed + human-signed-off 2026-06-14). **04a** — composer tables
  (`strategy_prompts`/`insight_types`/`insight_type_strategies`/`calibration_prompts`) +
  `evolution_config`, CRUD/cascade (4.1)/schedule-binding (4.2 kind=insight)/active-calibration/
  evolution-config API, R1 create-time gate reusing `validate_tokens`. **04b (Loop 1 自運作)** —
  `InsightCard`+`Prediction` schema (confidence required with a prediction), `insights` table with
  fingerprint cache + trading-day `due_at`, layer assembly (system+strategies+active calibration via
  06a `render_prompt`), the single **R1–R8 runtime gate** (shared with spec-07 preflight),
  `run_insight_type` generation (default role, R4 zero-LLM anomaly card, R6 partial, cache hits),
  scheduler `kind=insight` dynamic dispatch via an injected `register_insight_runner` (no scheduler→api
  cycle), date variables (`now`/`card_created_at`/`eval_date`, ISO-8601 +08:00),
  `complete_structured` `response_format` enforcement w/ graceful fallback, and the `alert-scan` job +
  `alert_events` + on_alert (R7) trigger (24h debounce, ≤3-day horizon). **04c (Loops 2–4)** — the
  **master LLM role** completion path, `insight_evaluations` store + `/api/ai-score` aggregation,
  pure `score_quant`/calibration-binning/`decide_promotion`, the daily `evaluate_insights` job
  (objective quant_hit + master narrative_score, **pending_data anti-poison** → `undetermined` after
  `defer_limit_days`), the weekly `generate_calibrations` job (master writes a validated new version,
  `min_samples`-gated, append-only), shadow evaluation + auto-promote + `calibration_regression`
  alert, and the §4.8 calibration validator (keyword denylist + one master review). **Layering held:**
  `llm_insight/*` import no `pricing`/`data_ingestion`/`api` (the only price-reading seam is
  `api/insight_service.py`; the wire encoder moved to `shared/wire.py` to kill a pre-existing
  `llm_insight→api` import). LLM emits no numbers of record (quant_hit is code; master writes only
  narrative/calibration text); single budget governs all roles. Cross-module senior review:
  APPROVE-WITH-NITS → fixed insights.model provenance, the reverse import, shadow `job_runs`
  distinction (`is_shadow` column, excluded from user-facing runs), and single-enum skip reasons.
  Deferred v1 watch-items: `relative`/`volatility`/`portfolio_return` quant metrics (narrative-only for
  now, anti-poison-safe). New tables: `insights`, `insight_evaluations`, `alert_events`,
  `alert_dispatch_log` + the four composer tables; `job_runs += is_shadow`; `insight_types +=
  horizon_days/eval_prompt`. (+265 tests; 667 → 932 passed.)
- **Data-source catalog, provider expansion & external-snapshot ingest (spec 20, Phase 4 —
  absorbs the planned 06b):** the data layer that makes the chips/sentiment prompt variables
  live. **Two seams** (control plane = spec 14 settings/keys/health/fallback; data plane =
  spec 20): the existing `pricing/` registry + providers stays the single interface — adding a
  source = one adapter + one catalog row + one probe adapter. New `pricing/snapshots_store.py`
  (append-only `external_snapshots`: source/dataset/symbol/as_of/payload/fetched_at, latest
  `fetched_at` wins; created EMPTY in `golden_db` so every external var degrades and prior
  suites stay green); `pricing/finmind_datasets.py` (FinMind Free-tier client for
  institutional/margin/PER/monthly-revenue/financials, **always per-`data_id` → Free tier**);
  `pricing/sentiment_source.py` (VIX via yfinance `^VIX` + CNN Fear&Greed free JSON) +
  `index_source.py` (yfinance `^TWII`/`^GSPC`/`^KLSE`); 4 free quote fallbacks
  (`twstock`/`stockprices_dev`/`klsescreener`/`malaysiastock`) wired into
  `DEFAULT_PROVIDER_ORDER`; `portfolio/external_signals.py` (pure Decimal derivations —
  consecutive-buy-days, net-buy-sum, chg/yoy/mom with None on denom≤0, percentile, vix_zone —
  numbers of record stay out of `llm_insight`); `pricing/ingest.py` + 5 scheduler ingest jobs
  (TW universe via direct SQL — `scheduler` imports no `data_ingestion`; 3-consecutive-fail
  warn → `data_source_health`). Catalog (`datasources_store.SOURCE_INFO`) expanded to the full
  ~15-source matrix with `provides`/`status` (`live`/`pending`/`blocked`); token-gated adapters
  (alphavantage/finnhub/fred) catalogued `pending` + key-gated `supports` (inert until a key is
  entered — not in the fallback order); the 7 chips/sentiment variables flipped `available=true`,
  served from snapshots via `VarContext` (router-fed; `llm_insight` imports neither `pricing`
  nor `data_ingestion`), degrading to `{"unavailable": true}` when a snapshot is missing.
- **FinMind auth & tier-awareness (spec 20.15, per the official AI-agent manual):** both
  FinMind callers switched to `Authorization: Bearer {token}` (token still DB-resolved), added
  optional `end_date`. Per-source token tier marking — `data_sources.tier` (additive idempotent
  migration), `SourceInfo.tiers`, `TIER_ORDER`, `PUT /api/datasources/{id}/tier` (400 unknown
  tier / `auth:"none"`; 404 unknown id), `tier`/`tiers` on the GET wire. `DATASET_TIER` (all 5
  = `free`) + a **local tier preflight** that raises `FinMindTierError` BEFORE any network call
  when the marked token tier is too low; HTTP 402 / JSON `status==402` → `FinMindQuotaError`
  carrying FinMind's message; `fetch_quota` reads `user_info` (`user_count`/`api_request_limit`).
  `GET /api/prompt-vars` now carries `required_tier`/`tier_ok`/`tier_label` so the frontend greys
  out variables/panels needing a higher plan; ingest catching tier/quota errors writes no
  snapshot and records `data_source_health` (status=error, reason) → the variable degrade payload
  carries the `reason` (router-fed). Non-regression: under a free/unset token the 5 chips vars
  stay `tier_ok=true`. Probe harness extended (Bearer, `fetch_quota`, tier-from-limit) +
  bounded `docs/probes/` refresh; full source matrix authored in
  `docs/design-handoff/.../specs/20-data-source-catalog.md`.
- **Data-variable & prompt-rendering foundation (spec 06a, Phase 4 — the AI brain's base):**
  the prompt "Lego-block" layer that specs 04/07 build on. New module `portfolio_dash/llm_insight/`
  (`variables.py` = a **26-variable / 8-category registry** mirroring `web/vars.js` + `render_prompt`
  + `validate_tokens` — the SINGLE reusable validation core that spec 04 §4.9 R1 runtime gating and
  spec 07 §7.2 preflight will also call; `system_prompt.py` = one editable global system prompt via
  `config_store`, default seeded). New `portfolio_dash/portfolio/technicals.py` (pure Decimal: MA
  20/60/120 + deviation, sample-stdev annualized volatility via `Decimal.sqrt`, max drawdown,
  price-vs-cost) — the **LLM emits no numbers of record**, so every numeric variable value is
  computed by the calc core and only ASSEMBLED into JSON here. Endpoints (`api/routers/prompts.py`):
  `GET /api/prompt-vars`, `GET/PUT /api/system-prompt`, `POST /api/prompts/preview` (diagnostic —
  ALWAYS 200, lists `unknown_tokens`/`scope_violations`, REAL computed values, **never calls the
  LLM**, `est_tokens` heuristic), `POST /api/prompts/test` (execution path — **422** on unknown
  token or a `per_symbol` var in a `portfolio`-scope body = R1; else real LiteLLM via a new
  `shared/llm.complete_text`, records `llm_usage` agent=`prompt_test`, budget exhausted → 402,
  returns `quota_remaining`). Money/price/rate are Decimal **strings**.
  - **Availability:** position+price+dividend+fx+system (17 vars) are live now; chips+sentiment
    (7) are `available=false` until spec 06b external ingest; backtest/calibration (2) until spec 04
    (`web/vars.js` mislabels the `ai` category `ready` — corrected to `false`). Unavailable vars
    render `{"unavailable": true}`. (Reconciliation: the spec prose says "24" variables; its own
    table and `web/vars.js` enumerate **26** — the authoritative count.)
  - **Senior-review (Opus, APPROVE-WITH-NITS) fixes folded in before merge:** `fx_rates_json` now
    emits the real spot rate (was as_of/stale only — `freshness.fx` carries no rate; the router
    resolves it via `get_fx`); `dividends_json` is the per-event ledger list with currency (was a
    yearly summary, contradicting its contract); `price_vs_cost` returns each ratio independently so
    a non-positive `adjusted_avg` (high-yield payback, allowed by `domain-ledger.md`) no longer
    drops the valid original ratio; `to_wire` now transforms Mapping keys (defensive); +coverage
    (all available tokens render valid JSON, fx rate present, per-event dividends). Conn-bearing
    reads (FX rates, dividend rows) are resolved in the api router and fed into `VarContext` —
    `llm_insight` imports only `portfolio`/`shared`/`api.serialize` (one-way deps intact).
  - **Deferred to spec 06b** (intentional split): `external_snapshots` table + 5 ingest jobs
    (FinMind chips/fundamentals/valuation, VIX/Fear&Greed, indices) + derivations + flipping
    chips/sentiment vars to available. **Global system-prompt CRUD lands here** (neither spec 06
    nor 04 assigned the endpoint; it is foundational to rendering).
- **Scheduler management API (spec 15, Phase 3):** `portfolio_dash/api/routers/scheduler.py` over the
  existing in-process scheduler. `GET /api/scheduler/jobs` (config + latest run + next fire),
  `PUT /api/scheduler/jobs/{id}` (cron/tz/enabled subset-merge with live reschedule), `POST
  /api/scheduler/jobs/{id}/run` (async **202** + a daemon thread that opens its own `session()`;
  `409 already_running` when the latest run is unfinished), `GET /api/scheduler/runs` (history;
  `limit>500 → 400`). Cron/tz validated via `CronTrigger.from_crontab` — invalid → **400
  `invalid_cron`** with the `field` pointing at the real offender (tz checked separately from cron),
  and **no DB write**. Every route degrades gracefully when `app.state.scheduler` is `None`
  (`PD_DISABLE_SCHEDULER=1`, e.g. tests): `next` = null, reschedule is a no-op. `cost_usd`/`reason`
  are Decimal-string/null, never stringified. New `scheduler/runtime.py::reschedule_job` (None-safe)
  + `scheduler/jobs.py` helpers (`start_job_run`/`finish_job_run`/`latest_run_unfinished`/
  `run_job_func`). **§15.0 schema columns (SR 2026-06-13; specs 04/07 depend on these):**
  `schedule_config += kind ('system'|'insight'), payload`; `job_runs += payload, reason, cost_usd`,
  added idempotently in `create_scheduler_tables` via a **local** `_add_column_if_missing` (no
  `scheduler → data_ingestion` dependency). v1 lists `kind='system'` jobs only (no insight jobs yet).
- **Sessions & authorized users (spec 09, Phase 3):** stdlib-only auth (`hashlib.scrypt` +
  `secrets`; no new dependency). `portfolio_dash/api/auth_store.py` (table DDL, scrypt
  hash/verify with `hmac.compare_digest`, user/session CRUD, mode check) + routers `auth.py`
  (`POST /api/auth/login` sets an `HttpOnly; SameSite=Lax; Path=/` `pd_session` cookie; `GET
  /api/auth/session`; `POST /api/auth/logout`/`lock` → 204) and `users.py` (`GET/POST/DELETE
  /api/users`; 201 create / 409 `duplicate_username` / 400 short-or-empty). **Guest vs protected
  mode:** `auth_users` empty → everything open; ≥1 user → a global `require_session` dependency
  (wired into `create_app`, sharing `Depends(get_conn)` so it is test-overridable — NOT middleware)
  gates all `/api/*` except `login`/`session` → 401 without a valid, unlocked cookie. `golden_db`
  seeds no user (guest), so the entire pre-existing suite stays green. Stores only salted scrypt
  hashes; `password_hash` is never returned or logged; bad-username and bad-password are
  indistinguishable in status, body, **and timing** (a dummy scrypt verify equalizes the
  missing-user path — no username enumeration).
  - **`GET /api/auth/session` shape (additive to the spec's two literal examples):** not protected
    → `{"mode":"guest"}`; protected + valid/known cookie → `{"mode":"user", username, name, locked}`
    (a locked-but-known session reports `locked:true`); protected + absent/unknown cookie →
    `{"mode":"user", username:null, name:null, locked:false}` so the shell shows the login screen.
  - **Senior-review fixes folded in before merge:** equalized login timing (closes the
    username-enumeration side-channel); `PUT /scheduler/jobs` 400 `field` attribution (valid tz +
    bad cron now blames `cron`); `require_session` treats a missing `auth_users` table as guest
    (defensive, no 500 before lifespan); non-empty `username` validation; +coverage (authenticated
    request through the gate, `/api/users` gated when protected, valid-tz/bad-cron field).
    **Deferred (low risk for the 1–2-user localhost threat model, filed as follow-ups):** `/run`
    check-then-insert TOCTOU; cookie `Secure` flag (HTTPS only); `run_job_func` outer-except
    logging; last-user deletion silently reverting to guest mode.
- **Dividend projection in dashboard payload (spec 05, Phase 2):** `DashboardData.dividend_projection`
  — annual declared-dividend cash flow `{year, by_currency: {<ccy>: {declared_gross, declared_net,
  events}}, basis: "declared_only"}`, computed by the pure `portfolio/dividends.py::project_dividends`
  over the ex-dividend calendar + valued holdings. Net applies each holding account's dividend model
  via `apply_dividend_model` (drip_us → 30% US withholding; cash/cash_cost_reduction → net=gross).
  **Per-currency, never summed across currencies.** v1 is `declared_only` (events with `ex_date.year ==
  current year`); v2 `declared_plus_estimated` deferred. **Reconciliation:** the Moomoo-US per-dividend
  platform fee mentioned in the spec is NOT encoded (no per-dividend fee config; probe-pending) — v1 net
  applies withholding only.
  - **Account model: `dividend_model` is now a first-class field** (`shared/models/assets.py` +
    `list_accounts` SELECT). `project_dividends` reads it from the DB-sourced `accounts` param (single
    source of truth; fail-loud KeyError on an unknown account_id), resolving the prior split where the
    projection read config-as-code while `accounts.py` read the DB (senior-review finding).
- **strategy/ module: alerts, what-if, rebalance (spec 03, Phase 2):** a new
  `portfolio_dash/strategy/` consumer layer (pure functions over computed outputs; writes
  no ledger) + five endpoints. **Alert engine** — `compute_alerts_from(data, rules, *,
  quota_remaining, quota_threshold)` is the single source for both the dashboard payload's
  embedded `alerts` and `GET /api/alerts` (the dashboard path reuses its already-built
  `DashboardData`, no second build); six v1 rules (single_weight, sector_weight, stale_price,
  missing_price, fx_drift, exdiv_upcoming, quota_low — `quota_low` escalates warn→risk at
  remaining 0). `GET/PUT /api/alert-rules` — editable thresholds in a single-row JSON config
  (`alert_rules_config`), Decimal-as-string, bounds-validated (out-of-bounds → 400). **what-if**
  `POST /api/whatif` — buy/sell trade sim reusing the real `compute_fees` (compute, no write);
  `account_id` defaults to the most-shares account and is echoed; `oversell=true` still returns
  full numbers. **rebalance** `POST /api/rebalance/preview` — target-weight trades with integer
  shares (MY market rounds to 100-unit board lots), per-row fee/tax + `new_weight`, and a summary
  (turnover/fees in reporting ccy, cash_after, excluded). Missing-price symbols are excluded and
  missing FX leaves `new_weight` null — never fabricated.
  - **Reconciliations (recorded):** (R1) `calib_gap` / `calibration_regression` rules DEFERRED
    to spec 04 (their AI-calibration data source does not exist yet) — absent, not stubbed with
    fake data; (R2) `quota_low` threshold is sourced from spec-16's `llm_config.get_alert_threshold`
    (single source of truth), NOT stored in alert-rules; (R3) alerts single-sourced via
    `compute_alerts_from`; (R4) rebalance v1 acts only on symbols present in `targets` (held
    symbols absent from `targets` are left untouched).
- **Fixed — quota alert threshold default (spec-03 §3.1 SR):** `llm_config._DEFAULT_THRESHOLD`
  changed `0 → 1.00` so `quota_low` fires when remaining < 1.00 until the user sets their own
  threshold, matching the SR ("預設值 1.00"). Spec 16's contract is unaffected (it asserts the
  key's presence, not the default value).
- **Export endpoints (spec 02, Phase 2):** a new consumer-layer module `portfolio_dash/export/`
  + `POST /api/export/{holdings,ledgers,llm-usage,job-runs,tax-package}`. All output is
  reconciliation-grade: **raw `Decimal` strings** (no rounding/thousands separators), **UTF-8
  with BOM**, **CRLF**, `Content-Disposition: attachment`. holdings → 21-column snapshot CSV
  (incl. `reporting_ccy_value` via the promoted public `RateResolver`; blank on missing FX,
  never fabricated) + `# as_of/fx_rates/generated` footer; ledgers → zip of the four raw ledger
  CSVs + `fee_rules_snapshot.json` (Decimals as strings via `to_wire`) + `manifest.json`
  (counts/as_of/schema_version); llm-usage/job-runs → range-filtered raw CSV (`from>to` → 400
  `validation_error`); tax-package → annual zip (`realized_gains`/`dividends`/`fx_realized`/
  `summary.md`), **year-cut by trade date**, **per-currency never summed**, realized converted
  at **trade-date FX** with the rate recorded (blank when no stored rate). Each endpoint writes
  one `job_runs` audit row.
  - **Calc-core enrichment:** `RealizedRow.sell_date` (the sell transaction's trade date), so
    realized gains can be cut by tax year. Domain-model enrichment only — no accounting-semantics
    change.
  - **DRY:** `forex.fx_pnl.realized_fx_rows` is the single source of the realized-FX formula;
    `_realized_fx` now sums over it.
  - **Reconciliation — audit `kind`:** spec 02 §3 says the audit row carries `kind=export`, but
    `job_runs` has no `kind` column and spec 15.0 places `kind` on `schedule_config` (not
    `job_runs`). Implemented instead as a namespaced `job_id=export:<type>` via
    `scheduler.jobs.log_export_run`.
  - **Reconciliation — module map:** `portfolio_dash/export/` added as a consumer layer
    (`web_ui → export → {portfolio, forex, pricing, data_ingestion, scheduler, shared}`; nothing
    lower imports it; the router stays thin and computes no numbers of record).
- **Review fixes I-2 / I-3 (2026-06-13):** a single shared secret-masking helper
  `shared/masking.py::mask_secret` (`prefix•••suffix`, with a short-key guard that fully masks
  keys too short to safely reveal a prefix/suffix) — now the one masker for `api_key_masked` and
  data-source key views (I-2); and `default_registry(conn)` wiring the FinMind token from the
  `data_sources` DB into the provider chain (env/ctor fallback retained) so the configured key is
  actually used at runtime (I-3).
- **Instruments API (spec 10, Phase 1):** `GET /api/instruments` (list + held flag + latest
  price + `chg_pct` + target_low; TW board serialized `null` until confirmed),
  `POST /api/instruments/probe` (TW board probe via `probe_tw_board`),
  `POST/PUT /api/instruments` (register/update through `register_instrument`, with
  `duplicate_symbol` 409 / `validation_error` 400 / `not_found` 404 envelopes). Schema/model:
  `instruments += target_low/board_status/is_etf` (idempotent migration); `target_low`/`is_etf`
  on the `Instrument` model, `board_status` a registration-only column set by
  `register_instrument`; `is_etf` is the single source of truth for ETF (no `sector=="ETF"`).
- **Ledgers read API (spec 11, Phase 1):** `GET /api/ledgers/{transactions,dividends,fx,openings}`
  read-only over the four append-only ledgers — account-name join, account/symbol/date-range
  filters, desc pagination (`limit`/`offset`/`total_count`), the buy/sell `total` sign convention,
  `implied_rate`, and the **lowercase wire format** for `side`/`type` (Currency stays uppercase).
  Reuses the existing `transactions.fee_rule_snapshot` column (mapped to API `fee_snapshot`) — no
  new column; `openings` gets a synthetic display id (its PK is account_id+symbol). No write routes.
- **Input center — context + manual entry (spec 12a, Phase 1):** `GET /api/input/context`
  (accounts + mapped `div_model`, fee-rule serialization with label, instruments + `etf`,
  current holdings) and `POST /api/input/manual/{preview,commit}` over `enter_transaction`.
  New `api/wire.py` shared mappers: lowercase `side` in/out (`parse_side`), `Issue` →
  `{sev,code,text,field}` (`issue_wire`), `fee_rules_wire` (reused by spec 13), `div_model`
  mapping (`cash_cost_reduction→tw`/`drip_us→drip`/`cash→net`). Commit is **ack-gated**: hard
  issues → 400, unacked oversell → 422 `oversell_unacknowledged`, else append. (Known follow-up:
  unify API money-string formatting — `_money_str` trims trailing zeros in manual preview/commit
  while `to_wire`/ledgers use raw `str`; cosmetic, deferred to the frontend-wiring phase.)
- **Input center — CSV import + AI input (spec 12b, Phase 1):** `POST /api/import/{preview,commit}`
  (4 ledger kinds; preview → `{rows:[{n,status,reason,data}],summary}`; **commit re-derives from
  `csv_text`** and re-validates vs the current ledger, ack-gating warn rows → 422
  `warnings_unacknowledged`) and `POST /api/input/ai/preview` (LLM text → preview + `meta` +
  `csv_text`; degradation mapped `budget_exceeded`→402 / `ai_not_activated`→409 /
  `llm_unavailable`→503). `ai_agents_input` now returns `AiInputResult{preview, meta, csv_text}`
  (meta from the `llm_usage` row; `completer` default resolved at call time). Also fixed
  `build_transaction_preview` to catch `decimal.InvalidOperation` (a malformed number now yields a
  `parse_error` row instead of crashing — matching its siblings + docstring). Senior review added a
  soft `fuzzy_resolved` (ack-gated) issue so a fuzzy symbol match surfaces + writes the resolved
  symbol (no silent phantom-symbol writes), in both `txn_preview_row` and `enter_transaction`.
- **Top-bar actions (spec 08 §8.2–8.3, Phase 1 close-out):** `POST /api/actions/refresh-quotes`
  (triggers the per-market `quotes_*` jobs synchronously, returns their `job_runs` ids; unknown
  market → 400) and `POST /api/actions/recompute` (re-runs `build_book` over the ledgers to validate
  consistency, `OversellError` → 422; append-only, writes nothing). `run_job` now returns its run id.
  (Sync 200 instead of the spec's 202-background — the `GET /api/scheduler/runs` poll endpoint is
  spec 15, not yet built; `run_job` swallows provider errors so a failed fetch is a logged run, not a
  500. Revisit when spec 15 lands.) **Phase-1 core data flow (specs 08 / 10 / 11 / 12) backend complete.**
- **Settings batch — accounts/fees + datasources + LLM settings (specs 13 / 14 / 16, Phase 2; built as
  3 parallel worktree-isolated sub-projects):**
  - **spec 13:** `GET /api/accounts` (read-only) — accounts + dividend model + fee-rule serialization
    (reusing `api/wire.py`); `version.seeded_at` is `null` (accounts aren't recorded in `settings_meta`).
  - **spec 14:** data-source management — new `pricing/datasources_store.py` (config_store tables
    `data_sources` / `data_source_health` / `data_source_fallbacks`); `GET /api/datasources`,
    `PUT …/{id}/key`, `POST …/{id}/test`, `PUT …/fallbacks`; API keys write-only (masked
    `prefix•••suffix`); `FinMindProvider` reads its token from the DB via an injected getter
    (env/ctor fallback retained).
  - **spec 16:** `GET /api/llm/config` + model CRUD (`POST/PUT/DELETE /api/llm/models/{alias}`,
    api_key write-only, `model_in_use` 422) + `PUT /api/llm/roles` + quota topup/threshold + model
    connection-test; `LLMRole += MASTER / MASTER_FALLBACK` (spec 04 overlay); usage aggregation reads
    (`shared/llm_usage_reads.py`: by-model / by-agent / 30-day daily series).
  - Routers mounted in `api/app.py`; `golden_db` seeds the data_sources tables.
- `shared/` foundation layer: `Currency`/`Market` enums; `Decimal` money primitives
  (canonical TEXT persistence via `to_db`/`from_db`, per-currency `quantize_amount`
  with ROUND_HALF_UP, float + non-finite guards); single pure `fx.convert` helper
  (rejects non-positive / non-finite rates); env-driven `Settings` + cached
  `get_settings`; stdlib `sqlite3` `get_connection`/`session` (WAL, foreign keys on).
- Package + tooling bootstrap: `pyproject.toml` (pydantic, pydantic-settings; dev:
  mypy strict, ruff, pytest, pytest-asyncio; strict `asyncio_mode`); `portfolio_dash/`
  package with `py.typed`; `tests/` layout.
- `portfolio/` calculation core: chronological ledger replay (`build_book`) →
  holdings + realized P&L; `value_holdings` (unrealized vs adjusted, capital-gain vs
  original, stale-price flagging); `total_return` (per-currency + reporting blended);
  reporting-currency `xirr_reporting` (pyxirr); `sector_allocation`; `combined_view`.
- `shared/models/`: canonical domain models (`Account`, `Instrument`, `Transaction`,
  `Dividend`, `FXConversion`, `OpeningInventory`) + `Money` finite-Decimal type.
- Dependency: `pyxirr` (irregular-cashflow XIRR).
- `forex/` FX (換匯) P&L: per-account foreign-currency pool (weighted-avg acquisition
  rate from home→foreign conversions), reconstructed foreign cash balance, realized FX on
  reconversions, unrealized FX (stocks + cash) marked to spot; reporting-currency
  `FXSummary` rollup. Presented as an attribution decomposition of the portfolio return
  (asset + FX), never additive.
- Data-source availability probe (spike) under `scripts/probe/`: typed harness
  (`ProbeResult` model, `run_probe` runner + fixture recorder, markdown report renderer)
  + live adapters (yfinance, TWSE, TPEx, twstock, stockprices.dev, klsescreener; FinMind /
  AlphaVantage / Finnhub keyed). Produced a ranked primary/fallback recommendation per
  (data type × market) and recorded raw fixtures under `tests/pricing/fixtures/` for
  `pricing/` mock tests. Results + `pricing/` architecture recommendation:
  `docs/probes/2026-06-08-data-source-probe-results.md`. Key findings: yfinance is the
  US/MY/FX workhorse primary; TW latest quotes from TWSE/TPEx string sources for true tick
  precision; MY 3-dp verified via klsescreener (yfinance is float64 — convert via
  `Decimal(str(...))`); TW board (上市/上櫃) must be resolved per instrument; keyed sources
  (FinMind/AlphaVantage/Finnhub) and Schwab await keys/OAuth.
- FinMind **validated** (2026-06-08, trial token, 600/hr): 6 datasets confirmed (price,
  dividend/除權息, FX, financial statements, institutional, margin) with fixtures under
  `tests/pricing/fixtures/finmind/`. Added capability research notes under `docs/research/`
  for **Schwab Trader API** (enables US account/transaction auto-import for `data_ingestion/`)
  and **FinMind** — both feeding `pricing/` source selection, `llm_insight/` fundamentals, and
  the LLM self-backtest loop.
- `pricing/` market-data layer (A+B+C): config-driven, capability-aware provider chain
  (yfinance / TWSE / TPEx / FinMind-keyed) writing idempotent SQLite rows
  (`prices`/`fx_rates`/`dividend_events`) — the only writer of those tables. (A) latest quotes +
  FX, (B) historical daily backfill, (C) dividend/ex-dividend **reference** data (FinMind 除權息
  + yfinance fallback). Graceful degradation (last-known + staleness; never raises/fabricates),
  per-row source provenance, `Decimal(str())` precision, per-instrument TW board resolution.
  Read API (`get_latest_price`/`get_fx`/`get_price_history`/`get_dividend_events`) + orchestrators
  (`refresh_quotes`/`refresh_history`/`refresh_dividends`). Providers tested against the probe's
  recorded fixtures (no live network). Dividend events are reference-only — never the ledger,
  never in P&L. Plan: `docs/superpowers/plans/2026-06-08-pricing-market-data-layer.md`.
- `data_ingestion/` ledger input (the only ledger writer): SQLite schema for the four
  source-of-truth ledgers (`transactions`/`dividends`/`fx_conversions`/`opening_inventory`) +
  `instruments` registry + `accounts`/fee-rule/LLM-model config seed. Per-account **fee/tax
  engine** (config rules + per-row snapshot; TW 0.1425% / 0.3% / 0.1% / 0.15%, min NT$20, integer
  rounding; US/MY structures). Three input modes through one resolve→fee/tax→validate→
  **preview→confirm** pipeline: **manual**, **CSV import**, and **AI Agents Input** (natural
  language → LLM structured draft → confirm; the LLM never writes directly). Symbol resolution
  fuzzy → LLM-fallback → confirm; sell>holdings blocks until confirmed; per-account dividend
  models (TW cash / US DRIP 30% / MY cash). New `shared/llm.py` (LiteLLM client + structured
  output + model registry + `llm_usage` token/cost log + graceful degradation; `litellm` dep).
  Spec/plan: `docs/superpowers/{specs,plans}/2026-06-09-data-ingestion*`.
- LLM config management + token-budget governance (`shared/`): DB-backed model registry
  (`llm_models`; per-model provider / endpoint / key / `vision` flag / pricing / context-window /
  timeout / retries / enabled). Four **nullable** role-defaults (`default` / `default_fallback` /
  `vision` / `vision_fallback`) — all empty = AI cleanly **off** (first-launch seed). `complete_structured`
  now: budget gate → role selection → **runtime failover** to the fallback model on provider error →
  **image (vision)** input → cost logged from the *selected* model's registry pricing. Three
  degradation signals — `AINotActivated` / `LLMUnavailable` / `LLMBudgetExceeded` (all subclass
  `LLMError`) — surfaced to callers (mapped to issue `kind`), never crash or fabricate. **USD budget**
  as an append-only reset ledger (`llm_budget_events`): remaining = latest reset amount − Σ usage cost
  since that reset; **unset = no cap**; **remaining < 0 blocks** ("額度用盡"); per-model usage/trend from
  `llm_usage` is never reset (a reset is a fresh start line, not a counter overwrite). Reusable
  `config_store` create-always / seed-once settings framework; package-root `portfolio_dash/bootstrap.py`
  composition root (so `shared/` keeps importing nothing internal); `llm_usage` ownership moved from
  `data_ingestion/` to `shared/llm_config`. AI Agents Input rewired to the registry API (no
  caller-supplied pricing). The settings-page UI stays deferred to `web_ui/`. Spec/plan:
  `docs/superpowers/{specs,plans}/2026-06-09-llm-config-and-budget*`.
- `scheduler/` in-process job scheduling (APScheduler, **triggers-only**): an extensible `JobSpec`
  registry + DB-backed `schedule_config` (on the `config_store` framework; idempotent per-job seeding,
  so a newly-registered job auto-gets a default row while user edits are preserved) + a `job_runs` log.
  v1 jobs trigger `pricing.refresh_*`: per-market post-close quotes + FX (`quotes_tw` / `quotes_us` /
  `quotes_my`, editable cron defaults in each exchange's tz), plus daily `history_daily` +
  `dividends_daily` sweeps; a manual `trigger_job` shares the same `run_job` path (job_runs logging; a
  job failure is logged as `error`, never crashes the scheduler). `build_worklist` reads the
  `instruments` table — a new nullable **`instruments.board`** column (idempotent migration) carries the
  resolved TW board, falling back to the market default (US `""` / MY `.KL` / TW `TWSE`) when unset.
  New dependency: `APScheduler` (locked in `stack.md`), confined to `scheduler/runtime.py`. The
  Scheduler settings-page UI is deferred to `web_ui/`. Spec/plan:
  `docs/superpowers/{specs,plans}/2026-06-10-scheduler*`.
- TW board resolution at instrument registration (`data_ingestion/` + `pricing/` + `shared/`):
  `Instrument` gains a persisted **`board`** attribute (`store.py` reads/writes it). `pricing.probe_tw_board`
  guesses a TW instrument's board by trying TWSE then TPEx (injectable providers, graceful on a network
  error). `data_ingestion.register_instrument` fills the board — US `""` / MY `.KL` deterministic; TW via
  an **injected** prober (keeping `data_ingestion` decoupled from `pricing`) — and upserts on confirm,
  raising a soft `board_unresolved` flag (never blocking) when a TW probe finds nothing. Resolves the
  board once so the scheduler work-list picks the right `.TW`/`.TWO` source; the listing/confirm UI is
  deferred to `web_ui/`. Spec/plan: `docs/superpowers/{specs,plans}/2026-06-10-tw-board-resolution*`.
- `portfolio/dashboard.py` — the orchestration combiner: `build_dashboard(conn, now,
  reporting)` assembles one complete `DashboardData` (KPIs, enriched holdings, realized
  P&L, returns, sector allocation, currency view, FX P&L, dividend summary, ex-dividend
  calendar, daily-replay trend series, freshness report, insight placeholders) from the
  ledgers + stored prices/FX; the contract `web_ui` (and later `llm_insight`) binds to.
  Introduces the one-way dependency edge `portfolio -> forex` (spec
  2026-06-10-dashboard-combiner-design).
- `portfolio/timeseries.py` — pure daily ledger-replay valuation series (market value
  vs cumulative net invested, carry-forward prices/FX, honest `incomplete`/unavailable
  flags).
- `pricing/store.py` — `get_fx_on` (on-or-before point-in-time rate) and
  `get_fx_history` reads; `data_ingestion/store.py` — `list_accounts` read.
- **Phase 0 — web API foundation (decision B):** `portfolio_dash/api/` FastAPI app
  factory (lifespan boots DB + scheduler; serves static `web/` via StaticFiles; routers
  under `/api/*`), the common error envelope (incl. LLM 402/409/503 mapping), the
  Decimal→string wire serializer (`to_wire`), per-request `get_conn`/`get_now`/`get_reporting`
  dependencies, and `GET /api/health` + `GET /api/dashboard` (serialized `build_dashboard` +
  `spark_30d` + `llm_quota`). Spec-17 test harness: `golden_db` fixture (seeded via the real
  write paths), injected clock (`GOLDEN_NOW`), `api_client`, `pytest-socket` network ban, and
  a `Makefile` (`make all`). Fee engine (spec 18): `FeeRuleSet` gains `flat_fee` /
  `stamp_duty_rate` / `stamp_duty_cap` and US/MY `min_fee`; MY stamp duty books to `tax`;
  worked examples W1–W9; US/MY rates backfilled from the spec-18.0 truth table (pending
  real-statement confirmation). `DividendType += NET` (MY single-tier).

## [v0.0.0] - 2026-06-05

### Added
- Project bootstrap: `CLAUDE.md`; `.claude/rules/` (stack, architecture,
  domain-ledger, markets-and-fees, data-and-pricing, llm-insight, engineering-process,
  design-handoff); `.claude/skills/` (resume-dev, ship-version); README, this
  changelog, LESSONS_LEARNED, .gitignore.
- Locked technology selection (Python 3.12 monolith: FastAPI + Jinja2 + HTMX +
  Alpine + ECharts + SQLite + LiteLLM + APScheduler; mypy strict; pytest).
- Domain model: `account` as a first-class entity (TW broker · Charles Schwab US ·
  Moomoo MY US · Moomoo MY); three markets (TW / US / MY); multi-currency
  (TWD / USD / MYR) with a single-reporting-currency combined XIRR (trade-date FX)
  and a currency-exchange ledger.
- Numeric precision model: `Decimal` end to end; store at full source precision
  (MY prices up to 3 dp), quantize amounts per currency minor unit at settlement.

_No application code yet — conventions and specification scaffold only._
