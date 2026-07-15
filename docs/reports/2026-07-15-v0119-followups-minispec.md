# Mini-spec — v0.1.19 follow-ups (fee-rule center, cash statement detail, settings IA, digest UX, small fixes)

Date: 2026-07-15 · Branch: `feat/v0119-followups` (from `main` @ d287837 = v0.1.19)
Owner directive: 12 items (problems + fine-tunes + features) on the v0.1.19 surface.
Process: Fable 5 plans/audits; Opus subagents implement in two waves (disjoint files per wave).

## Decisions (FU-D1..D8)

- **FU-D1 — Fee rules become user-adjustable via a DB overlay.** Defaults stay in
  `config_seed.py::FEE_RULES` (v2, authoritative). New table `fee_rule_overrides`
  (one row per rule set, JSON field-overrides, updated_at). Effective rule set =
  defaults ⊕ overrides, resolved **conn-aware at every money call site** (manual,
  CSV, edit-recompute, preview, rebalance, what-if, rebate forecast, accounts wire,
  export dump). Reset = delete overlay row(s) (per rule set + all). Validation:
  whitelisted fields per rule set, Decimal-parsed, bounds-checked. History is safe:
  per-row `fee_rule_snapshot` remains the arbiter; edits affect FUTURE rows only.
  Gate: session gate only (open in guest mode — same class as scheduler/ledger
  config; no outbound risk; reset makes demo recoverable).
- **FU-D2 — 費率明細 page becomes data-driven.** Rendered from new
  `GET /api/fee-rules` (per rule set: effective + default + per-field overridden
  flags). Editing UI + per-set and global 重設為系統預設. The static HTML block
  (drifted: wrong Moomoo US commission $0, missing SST/stamp/SEC/TAF/CAT/floor/
  rebate, stale names `tw_default`/`schwab_zero`) is deleted; rates never live in
  HTML again. Adjacent static 帳戶 cards fixed to真 rule-set keys.
- **FU-D3 — Settings IA reorg.** New tab **通知中心** (tab id `notify`): channel
  cards (`.nt-cards` ntfy/tg/email), quiet hours (`#nt-qh-*`), per-rule
  subscriptions (`#nt-subs`, `#nt-prefs-save`). Tab 排程 renamed **排程中心**
  (tab id `scheduler` unchanged). The 摘要與週報 card (`#digest-settings-card` +
  `#digest-config-wrap`) moves wholesale into 排程中心 above the jobs table —
  element ids preserved so `settings-digest.js` keeps working. Desync fix: any
  successful `PUT /api/scheduler/jobs/*` dispatches a `pd-jobs-changed`
  CustomEvent; both `settings-digest.js` and `settings-scheduler.js` listen and
  re-fetch; both also re-fetch on their tab's activation (`pd-settings-tab`).
  whatsnew CATALOG hrefs updated (notify-block entries → `settings.html#notify`;
  `digest-settings` → `settings.html#scheduler`; targets unchanged).
- **FU-D4 — Guest-mode gate adjustments (demo testability).**
  `POST /api/news/run` and `POST /api/digest/run` lose the `is_protected` 403
  (compute+cache actions; 409 in-flight lock stays). During a guest-mode digest
  run, outbound push dispatch is suppressed (outbound stays locked). All config
  writes that were 403 stay 403 (`PUT /notify/config`, `POST /notify/test`,
  `PUT /digest/config`, auth admin).
- **FU-D5 — Cash statement detail + account-level view + exports.** `CashLine`
  gains structured optional detail (symbol, name, qty, price, fee, tax, fx rate /
  counter amount) so 說明 renders human-readable per kind. `GET /api/cash/statement`
  `ccy` becomes optional: absent → all-currency account view (per-row ccy column;
  running balance stays per-(account,ccy) pool). New server-side exports:
  `POST /api/export/cash-statement` (CSV) + `POST /api/export/cash-statement-report`
  (print HTML via `report_html.py` scaffold), honoring `{account, ccy|null}`.
- **FU-D6 — Rebate month breakdown.** `GET /api/rebates` rows gain
  `trades: [{trade_date, symbol, name, side, fee, expected}]` (per-trade
  `⌊fee×rebate_rate⌋`; Σ == month `expected`; manual §3.6 math unchanged).
  UI: expandable month rows; existing e2e selectors preserved.
- **FU-D7 — Fee/tax override becomes a true toggle.** Manual-entry pencil
  (`input.js`) toggles OFF: restores `readOnly`, re-runs preview (auto values
  return), hides 已覆寫 badge, clears `fee_override`/`tax_override` from the
  commit body. Edit dialog (`ledger.js`) gets a revert-to-auto affordance
  clearing the dirty flag and re-fetching computed fee/tax.
- **FU-D8 — DB stats completeness + guard.** Register the 8 missing tables
  (`ledger_audit`, `target_weights_config`, `whatsnew_config`, `whatsnew_seen`,
  `rebate_skips`, `notify_config`, `digests`, `digest_config`) with zh labels,
  categories, date columns. Unit-test guard: build the full schema, enumerate
  `sqlite_master`, assert every app table is registered (nothing may land in
  其他). UI: grouped, scrollable (max-height + sticky header), last-refreshed
  timestamp next to the existing `#dbstats-refresh`.
- **Digest card UX (items 4+5).** Empty state gains a real link (
  `settings.html#scheduler`, flash target `#digest-settings-card`) and an inline
  立即產生 button (POST `/api/digest/run` → poll `latest` until stored). Movers
  render the instrument NAME (fallback symbol) with a tooltip
  `名稱（代號）・收盤 YYYY-MM-DD・更新 <fetched_at>`; digest payload movers gain
  per-entry `quote_date` + `fetched_at` (older stored digests: graceful fallback).

## Waves & file ownership (no two concurrent agents share a file)

**Wave 1** — A: fee-rule center (config_seed, all resolver call sites, new router,
settings.html fee section + new settings-fees.js, manual §3 note + en mirror,
stress phase-1 re-run) · C: cash statement (portfolio/cash.py, routers/cash.py,
export/*, cash.html/js, mock-data.js) · D: override toggle (input.js,
trades.html, ledger.js) + news gate (routers/news.py).

**Wave 2** — B1: settings IA + db-stats (settings.html, settings-scheduler.js,
settings-digest.js wiring, whatsnew.py hrefs, db_stats.py, settings-dbstats.js) ·
B2: digest cards UX (digest.js, digest_service.py, routers/digest.py guest gate,
index.html) · D2: rebate breakdown (api/rebates.py, routers/rebates.py,
rebate-inbox.js).

## Acceptance

Full suite green, `mypy --strict` clean, ruff clean, stress `--phase 1` fail=0,
demo deploy + `verify_live` + browser click-through of every changed flow.
Catalog (whatsnew) entries for user-facing changes are added at ship time.
