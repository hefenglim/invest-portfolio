# Mini-Spec — P3 Batch 3: Digests + Fix Pack (2026-07-14)

Owner rulings (recorded): **B3-D1** daily digest single edition ~15:10 Taipei, user-toggleable
+ trigger time adjustable. **B3-D2** weekly action list Sunday ~17:00 Taipei, toggleable +
adjustable. **B3-D3** pure-computed first; LLM one-liner is an optional switch, DEFAULT OFF.
**B3-D4** push discipline unchanged: pushes carry counts/percentages only, NEVER amounts.

Implementation is split into three sequential waves on branch `feat/p3-batch3`.
Research basis: seam maps + two deep audits (cash: SOUND-WITH-GAPS, 9 findings;
trades: SOUND-WITH-GAPS, 12 findings) — all findings CONFIRMED by probes unless noted.

Standing directives that apply to every wave:
- Every user-visible feature/change ships a `shared/whatsnew.py` CATALOG entry under
  version `0.1.19` (zh-TW user phrasing; `href` ⇒ non-empty `target`). Entries stay hidden
  until the ship bump — that is correct.
- Money is Decimal end-to-end; API returns Decimal strings; frontend never computes money.
- Guest/demo mode: all new write endpoints 403 via `auth_store.is_protected` gate
  (mirror `api/routers/notify.py:164-171`).
- Tests before/alongside implementation; mypy --strict clean; ruff clean.
- All repo artifacts in English.

---

## Wave 1 — Digests (daily close summary + weekly action list)

### Storage + config (`portfolio_dash/ops/digest.py`, new leaf — imports `shared/` only)
- Table `digests(id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('daily','weekly')),
  digest_date TEXT NOT NULL, payload TEXT NOT NULL, generated_at TEXT NOT NULL,
  UNIQUE(kind, digest_date))`. Upsert on conflict (idempotent regeneration; re-run same day
  = overwrite, never duplicate).
- `DigestConfig(BaseModel): llm_summary_enabled: bool = False` — single-row table via
  `shared/config_store.ensure_seeded` (mirror `ops/notify.py:360-446` pattern:
  `_create/_seed/ensure_seeded/load_config/save_config`).

### Assembly (`portfolio_dash/api/digest_service.py`, new — mirrors `api/news_service.py`)
- `run_digest_daily(conn, *, now) -> str` and `run_digest_weekly(conn, *, now) -> str`.
- **Daily payload** (all Decimal-as-string; every block degrades honestly to null/[] when
  data is missing — never fabricate):
  - `day_change`: NEW computation — per-holding price day-change from the last two stored
    closes (`pricing.store.get_price_history`, quote ccy — pure price move); portfolio-level
    = value-weighted by current market value in reporting ccy (weights from
    `build_dashboard`). Label semantics documented in code: price-only, excludes FX drift.
    Holdings missing 2 closes are excluded and counted in `excluded_count`.
  - `movers`: top 3 up + top 3 down by day-change % (symbol, name, pct).
  - `alerts_today`: today's `alert_events` rows (`substr(fired_at,1,10)=today`,
    excluding `rule_id LIKE 'signal_%'`) grouped by rule_id with severity + count, plus
    per-event symbol list. Severity/labels from `ops/notify.RULE_CATALOG`.
  - `signals_today`: today's `signal_%` events (rule_id, symbol).
  - `data_health`: held symbols whose latest stored close is older than 3 calendar days
    (symbol + age), count of failed jobs today (`job_runs.status='error'`).
  - `llm_note`: optional one-liner — ONLY when `DigestConfig.llm_summary_enabled` AND the
    AI-active predicate (Wave 3 adds `shared/llm_config.ai_active`; until then use
    try/except on the existing role-selection path). Reuse the cheapest existing LiteLLM
    role precedent (news organizer style); prompt passes the computed numbers and instructs
    the model to narrate ONLY those numbers (no new figures); on any failure → `null`
    (digest generation NEVER fails because of the LLM). Include prompt version in payload.
- **Weekly payload**: `items: [{id, icon, title, desc, href, target}]` — computed action
  items, each with a jump link (whatsnew-style flash target):
  - Rebalance drift: symbols currently outside the Swedroe band — reuse
    `api/alert_inputs.compute_alerts_full` and filter `rebalance_drift` alerts →
    href `index.html`, target `.rb-open-btn`.
  - Week's alert review: last-7-day `alert_events` grouped (rule, count, severity) →
    href `settings.html#alerts`, target `#alert-rules-wrap`.
  - Signal transitions this week: last-7-day `signal_%` events per symbol →
    href `instruments.html`, target `section[data-screen-label="標的清單"]`.
  - Upcoming ex-dividends: held symbols with `dividend_events.ex_date` in the next 14 days
    (skip block if no data) → href `trades.html`.
  - Data/system chores: stale quotes (as above) + failed jobs last 7 days →
    href `settings.html#scheduler`.
  - Empty week → `items: []` + friendly copy on the card (still generated + stored).
- Payloads carry a `schema_version: 1` field.

### Scheduler (`portfolio_dash/scheduler/jobs.py`)
- Runner seam (scheduler never imports api): `register_digest_runner(fn)` where
  `fn(conn, kind, now) -> str`; registered from `api/app.py` startup (mirror
  `register_news_runner`, `app.py:160`). Fallback when unregistered: return a no-op
  summary string (mirror existing fallbacks).
- Two JobSpecs appended to `JOBS`:
  - `digest_daily`, cron `10 15 * * mon-fri`, `Asia/Taipei`, enabled, desc
    "Daily close digest (assemble + push)".
  - `digest_weekly`, cron `0 17 * * sun`, `Asia/Taipei`, enabled, desc
    "Weekly action list (assemble + push)".
- Auto-appears in the settings scheduler tab via `ensure_job_rows`. Add zh labels to
  `JOB_ZH` in `web/settings-scheduler.js:49-66`: `digest_daily: '每日收盤摘要'`,
  `digest_weekly: '每週行動清單'` — AND backfill the three missing existing labels
  (`consensus_daily: '分析師共識'`, `signal_scan: '技術訊號掃描'`, `news_daily: '新聞摘要'`).

### Push (B3-D4 — hard rule)
- After assembly+store, the digest job pushes via `ops/notify.dispatch` directly
  (NOT `format_event`): gate on `cfg.subscriptions.get('digest_daily'|'digest_weekly', True)`
  and enabled channels; if `in_quiet_hours` → skip push (note "靜音時段略過推播" in the
  job summary; the stored digest is unaffected).
- Append `("digest_daily","每日收盤摘要","info")` and
  `("digest_weekly","每週行動清單","info")` to `RULE_CATALOG` (`ops/notify.py:63-81`) —
  frontend checkbox auto-renders; `load_config` self-heals defaults to True.
- Push text: title `收盤摘要 MM/DD` / `週行動清單 MM/DD`; body = counts + percentages +
  「開啟儀表板查看」 ONLY. A unit test MUST assert the composed push body contains no
  currency amount (regex guard: no `[NT$RM]|\d{1,3}(,\d{3})+` style amounts; percentages
  and small counts allowed).

### API (`portfolio_dash/api/routers/digest.py`, new)
- `GET /api/digest/latest?kind=daily|weekly` → latest stored digest (or `null`).
- `GET /api/digest/history?kind=&offset=&limit=` (limit 1..20, validation 400s mirroring
  whatsnew history) → `{total, offset, rows}`.
- `POST /api/digest/run {kind}` → 202 manual regenerate (guest 403; 409 if job in flight
  via `latest_run_unfinished` on the corresponding job id).
- `GET/PUT /api/digest/config` → `{llm_summary_enabled}` (PUT guest 403).

### Settings UI — 「摘要與週報」 card (alerts tab `view-alerts`, below notify channels)
- Per digest: enable toggle + friendly time picker. These read/write the SAME
  `schedule_config` rows through the EXISTING `GET/PUT /api/scheduler/jobs/{id}` —
  no duplicate state (single source; the scheduler tab shows the same rows).
  - daily: `HH:MM` input → cron `M H * * mon-fri`; weekly: weekday select + `HH:MM` →
    cron `M H * * <dow>`.
  - If the stored cron does not match the simple pattern (user hand-edited raw cron),
    render read-only 「自訂 cron — 於「排程」分頁編輯」 instead of the picker.
- LLM one-liner toggle → `PUT /api/digest/config` (default off). Auto-save on change
  (switch-shaped controls persist on interaction — LESSONS rule).
- 「立即產生」 button per digest → `POST /api/digest/run`.

### Dashboard cards (`web/index.html` + `web/app.js`)
- New `<section class="panel">` 「今日摘要」 directly after the KPI band (`index.html:23`),
  self-contained fetch pattern copied from `renderCashMini` (`app.js:563-607`):
  day-change headline (fmt.signedPct + signClass), movers chips, alert/signal counts with
  links, data-health line, optional llm_note, generated_at stamp + 歷史 link.
- 「週行動清單」 panel right after it: renders latest weekly `items` as a checklist with
  前往 buttons (navigate to href; reuse the arrival-flash mechanism ONLY if trivially
  reusable — plain navigation is acceptable).
- History: 「歷史」 opens a load-more modal copied from `web/whatsnew.js:415-500`
  (`openHistory`) paging `GET /api/digest/history`.
- Empty states: 「尚未產生摘要 — 於 設定→預警規則→摘要與週報 啟用,或按立即產生」.

### Tests (name-level)
- `tests/unit/test_digest.py`: assembly determinism on fixtures; day-change math (Decimal);
  missing-history exclusion; weekly item construction; push-body no-amounts regex guard;
  config seed/backfill; upsert idempotency.
- `tests/contract/test_digest_api.py`: shapes; validation 400s; guest 403 on PUT/run;
  history paging stitching (mirror whatsnew tests).
- `tests/scheduler/`: JOBS seeding includes the two ids (extend `test_seed.py`);
  runner-seam dispatch + quiet-hours skip + subscription gate.
- `tests/e2e/`: dashboard renders both cards without console errors (extend pages smoke);
  settings digest card toggles + time edit round-trip.
- whatsnew CATALOG: `0.1.19:daily-digest` (href `index.html`, target the new panel id),
  `0.1.19:weekly-action-list` (same page), `0.1.19:digest-settings`
  (href `settings.html#alerts`, target the new card id).

---

## Wave 2 — Ledger + cash hardening (from the two audits)

### 2A Trades ledger (audit findings; all CONFIRMED)
1. **[H1] Market coherence**: in `data_ingestion/validate.py:validate_transaction`, hard-reject
   when the instrument's market does not match the account's market (derive from
   `settlement_ccy` via the existing `_CCY_MARKET` inference in `api/routers/input_center.py:44-51`
   — move/share that map properly, e.g. into `shared/` or `data_ingestion`). Same check in the
   ledger edit path (`api/routers/ledgers.py` `_mutation_guard`). zh-TW message names both sides
   (e.g. 「AAPL 屬 US 市場,不可登錄於 台股帳戶」).
2. **[H2] Negative fees/tax**: hard `Issue` in `validate_transaction` (covers manual+CSV+AI);
   `ManualBody.fee_override/tax_override` constrained `>= 0`.
3. **[H3] Orphan-dividend 500**: `_replay_error` (`ledgers.py:207-248`) must catch `ValueError`
   /`KeyError` from `build_book` replay and return 422 with actionable zh-TW
   (「此更正會使 X 的股利/期初紀錄失去對應持倉,請先處理該紀錄」). Regression test hits the
   exact confirmed scenario (edit buy symbol with dependent dividend; delete buy).
4. **[M4] Overflow 500**: bound `shares`/`price` (`le=Decimal("1e12")`) on input models AND wrap
   the `fees.py` quantize so `InvalidOperation` surfaces as a validation issue, not a 500.
5. **[M5] Future dates**: soft `needs_confirm` issue when `trade_date > today` (via `get_now`);
   set `max` on the date input (`web/trades.html:92-93`).
6. **[M6] Edit recompute**: on transaction edit, when account/shares/price/side/date change and
   fee/tax were NOT explicitly edited by the user → recompute fee/tax from the (new) account's
   rule set and regenerate the per-row snapshot; explicit fee/tax edits are honored as overrides
   (snapshot notes `override: true`). UI: edit modal recomputes on field change like the entry
   form (reuse the preview seam if practical).
7. **[M7] Duplicate guard**: soft issue — on preview/commit, if an identical row
   (account+symbol+side+qty+price+date) already exists, add `needs_confirm`
   「相同交易已存在(今日已登錄一筆相同買賣),確認要再次寫入?」. No hard block, no schema change.
8. **[M8] Oversell replay scoping**: `_replay_error` diffs pre/post oversell sets and blocks only
   when the mutation INTRODUCES or WORSENS an oversell (existing acked oversell on an unrelated
   symbol must not poison unrelated corrections — regression test = the confirmed AAPL/2330 case).
9. **[M9] Honest copy + audit trail**: fix `web/trades.html:295` (and `:245`) copy — edits are
   explicit corrections with full-book replay validation, originals are captured to an audit
   trail (NOT "append-only + 原紀錄永久保留" which is currently false). Add
   `ledger_audit(id, table_name, row_id, action TEXT CHECK(action IN ('update','delete')),
   before_json TEXT, at TEXT)` written by the four ledger update/delete paths
   (`data_ingestion/store.py`). No UI viewer this wave (db-stats visibility is enough); test
   asserts before-values are captured.
10. **[L11] Precision cap**: apply the 4-dp price cap (ROUND_HALF_UP, cap-not-pad — same
    convention as `pricing/store`) at the transaction write seam.
11. **[L12] Fuzzy resolve**: require same-market candidates (or raise threshold to 0.75) in
    `data_ingestion/resolve.py:27-81`.
    (L10 tick advisory: OPTIONAL — only if trivial.)

### 2B Cash management (audit findings; CONFIRMED)
1. **[C2] Currency↔account validation**: server-side reject in `_movement_guard` +
   `/cash/fx` guard when ccy not in `{settlement_ccy, funding_ccy}` of the account; UI
   dropdowns (`web/cash.html:76-80,110-131`) constrained per selected account, each option
   labeled 交割幣/資金幣.
2. **[C3] Date-aware guard**: `_negative_after` must evaluate the RUNNING balance (min prefix
   over date-ordered ledger), not the end aggregate — back-dated withdrawals before funding
   must warn.
3. **[C1] Overdraft visibility across doors**: do NOT hard-block trades (users may not track
   cash at all). Two measures: (a) cash page banner listing every pool currently < 0
   (「資金池透支 — 可能漏登入金」); (b) manual-trade preview adds a SOFT issue when the
   account has ≥1 cash movement recorded (user opted into cash tracking) AND the trade would
   push that pool negative.
4. **[C4] Opening cash**: new movement kind `opening`(期初資金) — treated as a deposit in
   `cash_balances`, labeled distinctly; helper copy on the cash page explaining opening-
   inventory sells need an offsetting opening balance.
5. **[C5] Cash statement**: new `GET /api/cash/statement?account=&ccy=&offset=&limit=` —
   merged, date-ordered flow lines (movements + trade settlements + dividend credits +
   FX legs) each with delta + RUNNING BALANCE (computed server-side, Decimal strings).
   UI: balance cards clickable → statement table below (pdPager), replacing the
   movements-only view as the primary surface (keep the add/edit forms).
6. **[C6] Reporting total**: skip-not-abort on missing FX rate; annotate excluded pools.
7. **[C7] Color**: negative cash uses a warning/danger token, NOT `--up`
   (`web/cash.html:26`).
8. **[C9] Doc note**: one comment block in `forex/pools.py` documenting the two cash
   definitions (FX-exposure view vs funds view) and when they diverge.
   (C8 cash-in-net-worth: DEFERRED — product decision, do not implement.)
- whatsnew CATALOG 0.1.19 entries: cash statement (新增, href `cash.html`, target the
  statement section), ledger input hardening (優化, href `trades.html`, target the entry
  section), plus per-feature entries where user-visible.
- Tests: unit for every guard (each audit scenario becomes a regression test — the probes
  in the audit report are the specs); contract for statement shape/paging + validation;
  extend golden/e2e only where UI changed.

---

## Wave 3 — Fix pack A (bell dot / quota gate / news trigger) + UX pack (toolbar / inbox page)

### 3A Alert bell read-state (client-side, per diagnosis)
- `web/alerts.js`: persist a seen-set of alert ids (`localStorage`, key e.g.
  `pd_alerts_seen`) on panel open (`alerts.js:175-182`); `renderCount` (`:132-139`) lights
  the dot ONLY for unseen ids (numeric badge may keep total). Sync across tabs via the
  existing `storage` listener pattern (`:203-210`). Cap the stored set (e.g. last 200 ids).
  Do NOT touch `alert_events.consumed/notified_at` (they drive AI cards + push).
- e2e: open panel → dot clears; new alert id → dot re-lights.

### 3B Quota-low gating (per diagnosis)
- `shared/llm_config.ai_active(conn) -> bool`: true iff any role binding resolves to an
  enabled model (equivalent to `select_role_models` not raising `AINotActivated`).
- Gate `strategy/alerts.py:212`: `rules.quota_low.enabled and ai_active and ...`; add
  `ai_active: bool = True` to `compute_alerts_from`; feed from the three seams
  (`strategy/alerts.py:352-358`, `api/alert_inputs.py:159-168`,
  `api/routers/dashboard.py:65-75`).
- One-time idempotent cleanup at `ensure_tables`-adjacent migration: mark existing
  unconsumed+unnotified `quota_low` events consumed/notified so no stale card/push fires.
- Quota chip (`web/alerts.js:68-92`): when the wire says AI inactive, show 「AI 未啟用」
  neutral state instead of 「AI 額度 $0」+warning dot (requires exposing `ai_active` on the
  dashboard/alerts wire — smallest honest surface).
- Tests: engine gate unit tests (on/off × below/above threshold); cleanup idempotency;
  contract wire shape.

### 3C News manual trigger + schedule visibility (per diagnosis)
- Refactor `api/news_service.py`: extract `run_news_for(conn, symbols_with_market, now)`;
  `run_news_daily` = resolve held universe → core. Manual scope resolves from the registry
  (`data_ingestion.store`): `all` = ALL registered instruments (held + watchlist — note:
  nightly job stays held-only); single symbol = `[(symbol, market)]`.
- `POST /api/news/run {scope: "all"|"<symbol>"}` in `api/routers/news.py`: guest 403,
  409 `already_running` (reuse `latest_run_unfinished` on the news job id), 202 +
  background thread + `job_runs` row (reuse the scheduler run pattern
  `api/routers/scheduler.py:172-195`). NEVER auto-run on page load.
- News page UI (`web/news.html:60-64` panel head): scope `<select>`(全部 / per-instrument,
  populated from the already-fetched instruments list `web/news.js:139-143`) + 「抓取新聞」
  button + progress toast + reload on completion; plus a schedule hint line
  「排程:每日 06:00(設定→排程 可調)」 linking `settings.html#scheduler`.
- Tests: contract (403/409/202 + scope validation 400); unit for `run_news_for` universe
  resolution; e2e button smoke (mock/news pipeline stub — follow existing news test
  patterns; no real network/LLM).
- whatsnew entry `0.1.19:news-manual-fetch` (href `news.html`, target the toolbar).

### 3D Toolbar / button unification (site-wide)
- `web/styles.css`: introduce a named system —
  `.toolbar` row container (flex, gap 8px, align-items center, wrap; `.toolbar .spacer`
  for right-alignment instead of per-button `margin-left:auto`) and TWO button size tiers:
  `.btn` (base, 12px) and `.btn-sm` (11px). Migrate: `.btn-export` becomes
  `.btn btn-sm btn-export`-compatible (strip its baked `margin-left:auto`, font/pad now from
  tiers), `.rb-open-btn` and `.inbox-group-head .btn` become `.btn-sm` (delete the ad-hoc
  10-11px overrides), remove the two alignment patch rules (`styles.css:1149,1509`) and the
  inline `margin-bottom:1px` nudge (`instruments.html:71`).
- Same-row rule: within one `.panel-head`/`.toolbar`, all buttons share one tier. Fix the
  known offender rows: trades ledger export slot (匯出 CSV + 匯出報告 same tier + both with
  icon or both without), dashboard 持倉 head (再平衡試算 + any siblings), instruments header,
  settings save-bars (`.btn-outline-warn` folded into the shared palette in styles.css),
  cash/insights/news/pipeline rows. Modal foot is the canonical reference — do not change it.
- Mobile: extend the ≥44px touch-target block (`styles.css:1629-1631`) to cover `.btn-sm`
  (incl. ex-`.btn-export`) and `.btn-refresh`.
- Icon convention: icon glyph inside a `<span class="ico">` with a single shared gap.
- Verification: Playwright pass over every page asserting (a) zero console errors,
  (b) for each `.panel-head`/`.toolbar` row: all buttons' rendered heights within 1px
  (write this as an e2e helper assertion, not a screenshot diff).
- whatsnew entry (優化) not required per-row; ONE entry `0.1.19:ui-toolbar-polish`
  (href `index.html`, target `.panel-head`) describing the visual unification.

### 3E Dividend inbox → standalone page + reversibility
- New page `web/dividend-inbox.html` (shell scaffold, `data-page="divinbox"`), NAV entry
  `{id:'divinbox', href:'dividend-inbox.html', label:'股利收件匣', ico:…}` inserted after
  the ledger item (`web/shell.js:8-17`). Move the `#inbox-section` host + `inbox.js` boot
  there; `web/trades.html` drops the section and gains a one-line link
  「配息/配股偵測已移至 股利收件匣」 (dismissible note is fine). Re-point the nav badge
  (`shell.js:744-763`) to the new nav item; badge logic unchanged
  (`GET /api/dividend-inbox/count`).
- **Un-skip**: `POST /api/dividend-inbox/unskip {fingerprints}` (delete from
  `pending_dividend_skips`); UI: collapsible 「已忽略」 list (needs a
  `GET /api/dividend-inbox/skipped` listing skipped fingerprints with whatever detail
  `detect()` can still reconstruct — items no longer detectable show fingerprint + date
  only) with 取消忽略 per row.
- **Confirm-undo**: `confirm` already returns created dividend ids — after confirm, render
  a 「已入帳 N 筆」 strip listing each created row with a 復原 button →
  `DELETE /api/ledgers/dividends/{id}` (existing route; oversell-guard dialog reused).
  Item then resurfaces automatically (stateless re-detect). Strip persists for the session
  only (in-memory) + helper copy: 「反悔?刪除帳本中的股利紀錄即可,項目會自動重新出現在
  收件匣」. Page also explains detection is continuous (not one-shot).
- Stamp assets (`scripts/stamp_asset_version.py`) — the static-cache contract test
  auto-covers the new page. e2e: nav → page renders, confirm→undo→resurface flow,
  skip→unskip flow (seeded fixtures). whatsnew entry `0.1.19:dividend-inbox-page`
  (href `dividend-inbox.html`, target `#inbox-section`).

---

## Out of scope (deferred, reported to owner)
- Cash in dashboard net worth (cash audit #8) — product decision.
- True append-only correction rows (trades #9 option (a)) — the audit trail + honest copy
  (option (b)) is this wave's fix; a full correction-row redesign needs its own spec.
- Tick advisory (trades L10) unless trivial.

---

## Post-review adjudications (2026-07-14)

Deep-review verdict: **SOUND-WITH-FIXES**. The following were adjudicated and actioned in
the post-review fix pass:

- **MED-1** (`daytrade` not persisted → edit-recompute silently reverted a TW day-trade
  sell to the 現股 0.3% rate): **FIXED**. The flag is now a stored `transactions` column
  (migrated for legacy DBs), threaded through insert / list / update and the M6
  edit-recompute path; preserved across edits via a `None`-means-preserve wire contract
  (no new API/frontend surface this round).
- **LOW-2** (first digest could resurface a suppressed `quota_low`): **FIXED**. The digest
  `_alerts_today` / `_alert_review_week` now exclude `quota_low` whenever
  `shared.llm_config.ai_active(conn)` is False — the same gate the live-alert engine
  applies — without filtering on consumed/notified.
- **LOW-3** (H1 coherence guard could strand a legacy incoherent row on a same-key edit):
  **FIXED**. `_mutation_guard` applies the market-coherence branch only when the edit
  changes `account_id` or `symbol` vs the stored row; account-exists + symbol-registered
  checks stay unconditional.
- **W3's partial `.btn-export` / `.toolbar` migration**: **ACCEPTED** — same-row control
  heights are unified and e2e-enforced; a full `.toolbar` migration is deferred.
- **Unskip endpoint left ungated (no auth)**: **ACCEPTED** — it matches the sibling inbox
  endpoints and the demo/test data is synthetic.
- **C8 (cash in dashboard net worth)** and **true append-only correction rows**: remain
  **deferred to owner** (unchanged from the Out-of-scope list above).
