# Mini-spec — follow-ups round 3 (deep links, accumulative watchlist, input-suite hardening, cash tabs, polish, P4 features)

Date: 2026-07-17 · Branch: `feat/v0119-followups` (continues on top of accepted r1 @3523ebe + verified r2 @1ee66e5)

Owner items: ① push notifications carry clickable deep-link URLs ② watchlist deletion becomes
ACCUMULATIVE (soft delete; data retained; re-add restores + gap-backfills) ③ CSV date-format
hardening (annotated template headers + date-parse module + ambiguity chooser) ④ AI input repair
(dead ids), vision/screenshot parsing, per-run model picker with last-used persistence, prompt
centralized ⑤ real-run test the dividends pane ⑥ remove the FX card from trades (資金管理 owns it);
real-run test openings ⑦ unknown-symbol inline quick-add during transaction entry ⑧ watchlist add
flow: sector suggestion + 確認 button + background heavy fetch ⑨ drawer shows name ⑩ 資金管理 3 tabs
⑪ multi-user DB extensibility study (report) ⑫ data-growth capacity study (report). Plus approved
site-review items: P2/P3 polish ×8, P4 benchmark-TWR overlay, P4 target-price crossing alerts,
P4 net-worth-incl-cash (C8). P1①② and two P4 items get 5-proposal reports only (no code).

## Decisions

- **FU-D17 — push deep links.** `NotifyConfig` gains `public_base_url: str = ""` (notify_config DB
  row; editable in 通知中心; masked API untouched; empty ⇒ legacy text, no link). `alert_events`
  gains nullable `href` TEXT (ALTER-if-missing); the alert scan records each `Alert.href`.
  `notify_dispatch` builds `link = join(public_base_url, frontend_path(href))` where
  `frontend_path()` is a Python mirror of `web/alerts.js` `mapAlertHref` (unit test pins the
  mapping table). `format_event` drops 「請至儀表板查看詳情」 when a link is attached (channels
  already render `link`: ntfy `click`, Telegram/Email append). Digest pushes link to `index.html`
  and drop 「開啟儀表板查看」 when base is set. Test-send links to the base URL. Never hardcode
  hosts; the base URL is user-supplied config.
- **FU-D18 — watchlist deletion becomes accumulative soft delete (supersedes FU-D13's
  never-traded hard delete).** DELETE /instruments/{symbol}: held ⇒ 422 `held` (unchanged);
  otherwise ⇒ soft delete = `archived=1` for ALL non-held symbols (never-traded included). No
  data is removed: prices, dividend_events, signal_states, alert_events, news all stay. 422
  `has_history` disappears from the API. `store.delete_instrument` (hard delete) is retained
  internally but no longer routed. Restore paths: (a) 顯示已封存 list 還原 button; (b) POST
  /api/instruments with an existing archived symbol ⇒ restore + update provided name/sector;
  (c) `quick_register` on an archived symbol ⇒ restore. EVERY restore triggers a **background gap
  backfill**: `start = MAX(prices.as_of_date) for symbol − 7d overlap` (idempotent upserts make
  overlap safe), fallback smart 5y window; via `refresh_history(conn, registry, [ref], start)` +
  `refresh_instrument_quote`, FastAPI BackgroundTasks (never blocks the response). Dashboard
  byte-identical invariant (archiving never changes any money figure) stays guarded. UI: 刪除
  button soft-deletes with toast explaining data retention; archived list toggle relabelled to
  cover both intents; restore toast reports last-data date + 「背景補抓中」.
- **FU-D19 — CSV date hardening.** (a) Template headers carry annotations:
  `date(YYYY-MM-DD)`, optional columns marked `(選填)`; the parser canonicalizes headers by
  stripping any parenthetical annotation (half/full-width) + whitespace, so annotated templates
  round-trip (guard test updated). (b) New `data_ingestion/dateparse.py`: COLUMN-LEVEL inference
  across all rows over formats {ISO, YYYY/M/D, YYYY.M.D, YYYYMMDD, M/D/YYYY, D/M/YYYY, Excel
  serial, YYYY年M月D日}. Exactly-one-format-fits ⇒ auto. Multiple fit with conflicting readings ⇒
  NEVER guess: preview returns `date_ambiguity {column, candidates:[{id,label,example}], samples}`;
  the frontend renders a format chooser; the chosen `date_format` is passed to BOTH preview and
  commit (commit re-validates; ambiguity without a chosen format is an error, not a guess).
  Applies to all four import kinds.
- **FU-D20 — AI input.** Fix the id contract (`id="ai-text"` textarea; `id="ai-dropzone"` +
  hidden `<input type="file" id="ai-file-input" accept="image/*">`; thumbnail strip with remove;
  drag-drop + clipboard-paste images). `AiBody` gains `images: list[str] | None` (base64;
  ≤4 images; ≤5 MB decoded each; magic-byte sniff png/jpeg/webp) and `model_alias: str | None`
  (must be enabled; vision-capable required when images present). `ai_agents_input` passes
  `images=` to `complete_structured_meta` (auto-routes to the VISION role when images present).
  The AI-parse prompt moves from `agents.py::_PROMPT` into `llm_insight/official_templates.py`
  as versioned code-owned constants (`AI_INPUT_PROMPT_VERSION/_BODY`); bump `LIBRARY_VERSION`.
  Model picker `<select id="ai-model-select">` lists enabled models from GET /api/llm/config with
  「自動（角色預設）」 default; last-used persisted in localStorage `pd_ai_model`. Parse ⇒ existing
  preview table ⇒ user confirms ⇒ existing /api/import/commit (unchanged single commit door).
- **FU-D21 — real-run input testing (dividends + openings).** Drive REAL flows on the e2e flow
  server for every button/branch: TW cash (adjusted-cost reduction), TW 配股 (shares only), DRIP
  (gross/wh/net/shares/price ⇒ $0-cost shares), MY net; openings (build date, avg/total ⇒ XIRR
  flow + holdings). Verify downstream numbers via /api/dashboard + cash statement. New e2e
  modules (with the `_loopback_sockets` autouse fixture); any defect found is fixed in-wave.
- **FU-D22 — remove the FX card from trades.** Delete the orphaned block (trades.html:219-232;
  zero JS bindings — FX moved to 資金管理 2026-07-03). Tab label 換匯＋期初 → 期初庫存 (ids stay).
- **FU-D23 — unknown-symbol quick-add + add-flow UX.** New shared dialog `web/inst-quickadd.js`:
  fast lookup (new GET /api/instruments/lookup?symbol=&market= returning name + suggested sector +
  board/is_etf; provider-verified existence = typo guard) ⇒ dialog with sector combo (suggested
  default + datalist of existing sectors + free text) ⇒ 確認 (primary; replaces 取消-as-only-exit)
  + 記一筆買入 (register then jump to manual pane, symbol prefilled) + ✕ close. Registration
  writes the row immediately; heavy quote/history fetch runs in BACKGROUND (BackgroundTasks; same
  primitive as FU-D18 restore backfill). trades manual pane: the 未註冊 hint gains 立即註冊 opening
  the dialog; the auto-register-at-commit fallback stays. instruments.html add flow reuses the
  same dialog.
- **FU-D24 — drawer name.** `/api/symbol/{symbol}/detail` gains `name` (+ market); detail.js
  renders `sym-name` in the non-held branch too (held branch already does via holding.name).
- **FU-D25 — 資金管理 three tabs.** 賬戶現金 (pools + statement) / 出金入金 (deposit form +
  movements ledger) / 換匯中心 (FX form). Reuse the settings tabbar pattern (inline CSS + hash
  sync + ≤760px scroll strip); ALL existing element ids preserved (tests). Statement empty state
  upgraded to the `window.emptyState()` pattern with guidance.
- **FU-D26 — polish pack (approved P2/P3).** (1) `.save-bar` fully opaque background + border +
  shadow (no translucency over text). (2) `select.fee-input` widened (no clipped option text).
  (3) Bell panel gains a scope caption (即時狀態); digest 今日警示 labelled as scan-event count —
  the two numbers are different things by design. (4) Bell rows grouped by `rule` when ≥2
  (「報價過期 ×11」 + symbols summary). (5) Mobile wide tables: stronger edge fade + one-time
  scroll-hint pill. (6) Settings tabbar mobile: edge fade + active-tab scrollIntoView. (7) XIRR
  low-confidence: `xirr_reporting` also returns the flow-window span; `KpiSummary.xirr_window_days`
  (additive); badge 「觀察期 N 天・短窗參考」 when < 365.
- **FU-D27 — benchmark TWR overlay.** New pure `portfolio/twr.py`: daily chain-linked TWR from
  `daily_value_series` points (flow = Δnet_invested; skip/carry zero-value or incomplete days;
  Decimal end-to-end; fixed-fixture unit tests). Benchmarks (`0050.TW` TWD, `^GSPC` USD — config
  constant list) are fetched WITHOUT instruments registration (prices table has no FK): the daily
  history job + smart backfill include them. Benchmark series converted to the reporting currency
  at daily FX (carry-forward) so the comparison embeds FX like the portfolio does. New endpoint
  GET /api/performance/twr?benchmark=…&window=… → rebased-to-100 Decimal-string series with
  degrade labels; rendered as a mode toggle on the dashboard trend card (separate JS init; no
  framework). Analysis metric, not money-of-record; oracle untouched.
- **FU-D28 — target-price crossing alerts.** `instruments.target_high` column (additive,
  ALTER-if-missing) joins `target_low`. New rule `target_cross` (AlertRules + RULE_META + logic in
  compute_alerts_from; per-symbol prices fed via api/alert_inputs seam; fires 跌破/突破 with
  href=/symbol/{sym}, so FU-D17 gives it push deep links for free). Per-symbol targets are edited
  inline on the 觀察清單 page (PUT /instruments/{sym}); the rule's on/off lives in 設定 → 預警規則.
- **FU-D29 — net worth incl. cash (C8).** New pure builder composing (a) the UNCHANGED
  `daily_value_series` and (b) a daily cash-balance series derived from dated `pool_lines`
  running balances, converted at daily carry-forward FX. Additive wire: trend points gain
  `net_worth`; KPI gains 總淨值(含現金) current value. Existing verified series/contracts are not
  modified — composition only, guarded by fixed-fixture unit tests + golden-payload update +
  a formula note in the accounting manual (zh authority + en mirror).

## Reports only (no code)

R-1 multi-user DB extensibility (per-user ledger DB vs shared market-data DB; news DB is the
precedent; migration seams). R-2 data-growth capacity estimate (per-table rows/year, SQLite
headroom, retention priorities). R-3 sector-taxonomy normalization — 5 proposals. R-4 account
naming unification — 5 proposals. R-5 dividend income card + historical dividends chart — 5
design proposals. R-6 holdings notes/thesis field — 5 proposals. All in
`docs/reports/2026-07-17-r3-research-pack.md`.

## Waves (file ownership disjoint per phase; agents never commit)

- Phase 1 (parallel): **W-A** FU-D17 + bell/digest labels+grouping · **W-B** FU-D18 ·
  **W-C** FU-D25 + statement empty state · **W-D** FU-D26(1,2,5,6,7) + FU-D24 · **W-E** reports.
  (W-A must NOT edit settings.html — the base-URL field is built dynamically in settings-notify.js;
  W-D owns settings.html/settings.css/styles.css.)
- Phase 2 (sequential — shared trades.html/input.js/input_center.py): **W-F** FU-D19 →
  **W-G** FU-D20 → **W-H** FU-D21+D22+D23.
- Phase 3: **W-I** FU-D27 ∥ **W-J** FU-D28, then **W-K** FU-D29.

## Acceptance

Full pytest, bare mypy --strict (whole 482-file scope, --no-incremental), ruff, stress phase 1
fail=0, id-contract sweep (every JS-bound id exists in markup), demo deploy + verify_live +
browser click-through; then the orchestrator's deep coordinated cross-review before the owner
report.
