# Mini-spec — follow-ups round 2 (watchlist deletion, movers price, 資料中心, CSV import suite)

Date: 2026-07-16 · Branch: `feat/v0119-followups` (continues on top of the accepted round-1 pack @3523ebe)
Owner items: ① watchlist deletion (held/closed handling) ② movers tooltip adds the PRICE ③ db-stats →
standalone 資料中心 page ④ CSV import: template download + dead click fix + parse→confirm→commit ⑤ a
read-only site-wide design/feature review (recommendations only, separate report).

## Decisions

- **FU-D13 — watchlist deletion, three tiers.** The instruments registry IS the watchlist; ledger
  tables reference `symbol` with NO foreign keys, and the dashboard's unregistered-symbol guard
  silently DROPS orphaned history from every figure — so a hard delete with history would corrupt
  realized P&L/XIRR. Therefore:
  1. **Never-traded watch-only symbol** (no rows in transactions/dividends/opening_inventory):
     true DELETE allowed → also cleans `prices`, `dividend_events`, `signal_states`,
     `alert_events` (symbol), `pending_dividend_skips`, and the `target_weights_config` entry;
     writes `ledger_audit`.
  2. **Currently held** (`current_shares > 0` anywhere): DELETE and archive both 422 (`held`) —
     no bypass.
  3. **Closed-with-history**: DELETE 422 (`has_history`, no bypass — ledger integrity/重算), the
     dialog offers **封存 (archive/stop-tracking)** instead: new `instruments.archived` column
     (ALTER-if-missing, default 0). Archived symbols are excluded from quote/history/dividend
     fetch (`build_worklist`), signal evaluation (`_registered_symbols`), and the news "all"
     scope — but stay REGISTERED, so all money computation, exports, and 重算 are unchanged
     (invariant: archiving never changes any dashboard number). Reversible (還原). **Invariant
     held ⇒ not archived**, enforced at the single write seam: `store.add_transaction` /
     `insert_opening` un-archive the symbol on any new booking.
  UI: instruments rows get 刪除 (danger); 422 branches map to explanatory dialogs (has_history →
  offer 封存); archived rows dimmed + hidden behind a 顯示已封存 (N) toggle, with 還原.
- **FU-D14 — movers tooltip carries the price.** Digest movers entries add `close` (Decimal
  string). Tooltip = 「名稱（代號）・股價 {close}・更新 {YYYY-MM-DD HH:MM}」 (updated-at =
  fetched_at, fallback quote_date; payloads without `close` keep the round-1 format).
- **FU-D15 — 資料中心 standalone page.** `web/data-center.html` + NAV entry (id `datacenter`,
  before 系統設定). The db-stats section moves off settings verbatim (same element ids; JS renamed
  `data-center.js`), plus a summary strip (total tables / total rows / DB sizes) and per-group
  subtotals. `/api/db-stats` endpoint unchanged. Categorization: keep the 6 categories now;
  the completion report documents the split-later assessment. whatsnew `db-stats` entry re-targets
  the new page; settings 一般 gains a pointer link.
- **FU-D16 — CSV import suite.** (a) Root cause of the dead zone: `trades.html` never had the ids
  `initCsv()` binds (`csv-dropzone`, `csv-file-input`, `csv-paste`) — click, drag-drop AND
  paste-preview were all unbound. Fix the markup contract (hidden file input, ids, hint id).
  (b) Template download per import kind (transactions/dividends/fx/openings):
  `GET /api/import/template?kind=…` returning text/csv (Content-Disposition via the export
  `_respond` pattern); canonical column order lives as constants in `csv_import.py` next to the
  parsers (single source); transactions template carries one example row per scenario (TW buy
  auto-fee, TW sell daytrade, Schwab US sell, Moomoo US buy, Moomoo MY ETF buy, fee/tax override).
  **Guard test: the generated template must round-trip through the real preview builder with zero
  parse errors.** (c) The backend parse→preview→confirm(ack)→commit flow already exists and is
  kept; frontend hints/placeholder fixed to include the required `account` column.

## Wave file-ownership (parallel, disjoint)

- **W-A** watchlist + tooltip: schema.py, store.py, models/assets.py, routers/instruments.py,
  scheduler/jobs.py, signals_service.py, news_service.py, instruments.html/js,
  digest_service.py, digest.js, own test files (+ new e2e module).
- **W-B** 資料中心: data-center.html/js (new), settings.html (remove section), shell.js (NAV),
  whatsnew.py, db_stats.py (summary fields only), test_pages_smoke.py retarget.
- **W-C** CSV suite: trades.html, input.js, input_center.py, csv_import.py (+ sibling builders'
  column constants), api.js (GET download support if missing), own tests (+ new e2e module).

## Acceptance
Full pytest, bare mypy --strict (476-file scope), ruff, stress phase 1 fail=0 (archived-symbol
invariant test proves money core untouched), demo deploy + verify_live + browser click-through.
