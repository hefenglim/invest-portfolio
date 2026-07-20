# Batch A — Phase-1 Decomposition & Dispatch Plan (round-7 follow-ups, target v0.1.21)

**Status:** Phase-0 signed off 2026-07-21 (owner: 「全部照建議」 — all recommendations
Q1–Q7 adopted). This document is the Phase-1 plan: work-unit DAG, locked interface
contracts, agent assignment, and gate criteria. Batch B (Moomoo account merge) is a
separate later round and is OUT OF SCOPE here.

Phase-0 investigation evidence (5 parallel read-only audits, 2026-07-21) is summarized
inline where a contract depends on it.

---

## 1. Work units → waves → ownership

| Unit | Scope (Phase-0 ref) | Agent | Wave | Effort |
| --- | --- | --- | --- | --- |
| **W1** | A1 MY resolve: prompt v2 + static Bursa registry + MY lookup fallback | Opus 4.8, xhigh | 1 | ~1.5–2 d |
| **W2** | A2 news fetcher: observability + HTTP hardening + extraction fallback chain | Opus 4.8, xhigh | 1 | ~2–2.5 d |
| **W3** | A3 cash-after + A4 old-vs-new (preview fields, whatif fields, drawer rewire to `/api/whatif`) | Opus 4.8, xhigh | 1 | ~1.5 d |
| **W4** | A5 clear-on-success (AI/CSV) + A6 opening-inventory simplification | Opus 4.8, xhigh | 2 (after W3) | ~2.5–3 d (C8 reader surface is large — see contract) |

- ≤3 concurrent (wave 1 = W1+W2+W3). **W4 waits for W3** because both edit
  `web/input.js` (W3: preview render region; W4: AI/CSV commit + fxopen regions) —
  file ownership is exclusive per wave, agents never run git, orchestrator audits
  every diff.
- File ownership (exclusive within a wave):
  - **W1:** `portfolio_dash/llm_insight/official_templates.py`,
    `portfolio_dash/pricing/bursa_registry.py` (NEW), `portfolio_dash/pricing/names.py`,
    `portfolio_dash/api/instrument_service.py`; tests: template/drift tests,
    `tests/pricing/test_bursa_registry.py` (NEW), new contract test file for MY lookup.
  - **W2:** `portfolio_dash/news/*` (fetcher/store/pipeline/models),
    `portfolio_dash/api/news_service.py`; `tests/news/*`.
  - **W3:** `portfolio_dash/api/routers/input_center.py`,
    `portfolio_dash/strategy/whatif.py` (+ its router model if needed), `web/input.js`,
    `web/detail.js`, `web/mock-data.js`; `tests/contract/test_input_manual_api.py`,
    `tests/contract/test_whatif_api.py`, `tests/strategy/test_whatif.py`, new e2e
    drawer-flow test. Constraint: no `web/trades.html` edits (W4-owned, wave 2).
  - **W4:** `web/input.js` + `web/trades.html` (handed off from W3),
    `portfolio_dash/data_ingestion/{opening_import,schema,store,import_templates,
    agents}.py` (+ `preview` row model if `line_no` is needed), the `OpeningInventory`
    model + ALL readers per C8 (`export/ledgers_report.py`, `export/tax.py`,
    `strategy/whatif.py` — wave-2 sequential, no conflict —,
    `portfolio/dashboard.py`, `api/routers/{symbol,ledgers,actions}.py`),
    `scripts/stress_audit/common.py` (oracle raw-SQL reader — gate-4 critical);
    test files per the C8 enumeration + new e2e clear-on-success + opening-flow tests.
- Escalation rule (standing): agents escalate ONLY on ambiguous/conflicting spec or a
  blocked dependency; each runs an internal senior-review pass before reporting with
  the unified report schema (完成項/決策/偏離/風險/測試證據).

## 2. Locked interface contracts (pre-dispatch — deviations require orchestrator sign-off)

### C1 — Bursa registry (W1, NEW module `portfolio_dash/pricing/bursa_registry.py`)

```python
BURSA_COMPANIES: dict[str, str]   # 4-digit Bursa code -> official short name
def bursa_name(symbol: str) -> str | None   # strip()+upper() normalize; exact-code lookup
```

- The table is **baked static data fetched at DEV time from the authoritative Bursa
  listing directory** (bursamalaysia.com); module docstring records source URL +
  retrieval date. NEVER fabricated from model memory. Leading zeros preserved
  (`"0166"`, not `166`).
- Coverage goal: full Main + ACE market equities list as published; ETFs included if
  present in the directory.

### C2 — MY lookup fallback (W1, `api/instrument_service.py::lookup_instrument`)

For `Market.MY` only: when the quote-fetch path yields no quote, if
`bursa_name(symbol)` hits → return `found=True`, `name=<registry name>`, board
`.KL` default, other fields unchanged. Response **shape unchanged** (no new fields).
`pricing/names.py` MY name path: registry first, yfinance `get_info()` fallback.
Effect: a valid Bursa code now verifies offline, so a correct AI answer with high
confidence reaches `status:"resolved"` even when yfinance lacks the counter.

**Scope caveats (Senior Review #3, locked):** the fallback sits in the brand-new-symbol
tail only (the known-symbol branch returns earlier). It completes ai-resolve
verification and the force-registering `POST /instruments` funnel (quick-add confirm).
The manual-trade auto-register path (`input_center` → `quick_register(force=False)`)
still 422s `quote_not_found` for a quote-less MY counter — **by design, unchanged**.
A registry-verified MY symbol registers with **no price row** (valuation degrades
gracefully as stale until a provider covers the counter) — accepted consequence.

### C3 — Prompt v2 (W1, `official_templates.py`)

- `AI_INSTRUMENT_RESOLVE_PROMPT` MY clause expanded to TW parity: (a) name⇒code
  exemplars (candidates: Maybank⇒1155, Public Bank⇒1295, Tenaga⇒5347, CIMB⇒1023,
  Inari Amertron⇒0166, IOI Corporation⇒1961, IOI Properties⇒5249 — **every exemplar
  verified against the fetched directory before baking; drop any that fail**);
  (b) explicit leading-zero rule (ACE codes keep the leading zero — 0166, never 166);
  (c) brand/mall/subsidiary rule (map to the listed parent, e.g. "IOI Mall" → IOI
  Properties; if no listed parent → not_found); never fabricate.
- Same MY guidance mirrored into `AI_INPUT_PROMPT_BODY` (trade-input path).
- Version bumps: resolve prompt → `v2`; **`AI_INPUT_PROMPT_VERSION` → `v4`** (its body
  changes too — module convention: bump on any content change); `LIBRARY_VERSION` →
  `official-v10 (2026-07-21)`. Prompt version is provenance-only (no response cache
  keyed on it) — bumps feed the drift guards, not cache invalidation.
- Drift tests to update (exact list, Senior Review #6):
  `tests/llm_insight/test_official_templates.py` (LIBRARY_VERSION pin, resolve-prompt
  version pin, input-prompt version pin, content asserts; ADD a new assert pinning the
  MY exemplars + leading-zero rule) and `tests/llm_insight/test_prompt_registry.py`
  (registry map). `tests/data_ingestion/test_agents.py` identity check is
  content-independent — safe.

### C4 — News fetch outcome + observability + retry (W2)

```python
@dataclass(frozen=True)
class FetchOutcome:
    text: str | None
    status: str   # "ok" | "http_error" | "non_html" | "too_short" | "salvaged" | "blocked_scheme" | "error"
    detail: str = ""            # e.g. "HTTP 403", exception class, salvage source
def fetch_article(url: str) -> FetchOutcome   # new; fetch_article_text() delegates for compat
```

- HTTP hardening: add `Accept` + `Accept-Language` headers; opener with
  `HTTPCookieProcessor` (consent redirects); `_MAX_BYTES` → `1_500_000`; timeout
  unchanged (20 s); no `Accept-Encoding: gzip` (urllib won't auto-decompress).
- Extraction fallback chain, in order, before giving up: (1) current block-strip;
  (2) JSON-LD `articleBody` + known embedded-JSON shapes (Yahoo `caas`/preloaded
  state) via stdlib `re`+`json`; (3) largest `<p>`-cluster; (4) if prose-guard still
  fails but text exists → return it with `status="salvaged"` (LLM trims downstream).
  `_MAX_TEXT_CHARS` (8 000) still applies at the end.
- Store: `_add_column_if_missing` pattern → `fetch_status TEXT DEFAULT ''`,
  `fetch_attempts INTEGER DEFAULT 0`; every non-ok outcome logged at WARNING with URL.
- Retry queue: `list_refetch_candidates(conn, *, max_age_days=14, max_attempts=3,
  limit=10)` → pipeline re-fetches empty-body rows each run even after they age out
  of discovery; attempts increment; bounded per run.
- No new dependencies (trafilatura/lxml rejected — locked stack + FinMind lxml<5).

### C5 — Preview payload additions (W3, additive only)

- `manual_preview` response gains `cash_after: str | null` = account-cash balance +
  the already-signed `total` (BUY negative / SELL positive), emitted only when the
  balance is known; currency = the existing `account_cash.ccy` (dynamic — USD for US
  instruments, never hardcoded TWD).
- `_position_preview` gains `old_shares`, `old_original_avg`, `old_adjusted_avg`
  (Decimal strings; `null` for a fresh position). Existing `new_*` fields unchanged.
- `/api/whatif` response gains `old_shares`, `old_original_avg`, `old_adjusted_avg`,
  `old_weight` beside the existing `new_*` fields. `old_shares`/`old_*_avg` come from
  `held_*` already in scope; **`old_weight` does NOT** (Senior Review #9) — it requires
  refactoring `_new_weight` to also surface `old_position_reporting_value /
  current_total` (operands exist internally but are not returned today).
- SELL note (Senior Review #10): `compute_whatif`'s sell branch emits no
  `new_original_avg`/`new_adjusted_avg` (averages are unchanged by a sell) — the
  drawer renders old==new for the avg pair on sells; this is correct, not a missing
  field.
- `web/mock-data.js` documents the new fields; contract tests extended (including one
  pinned case where original ≠ adjusted via a dividend-adjusted holding). W3's test
  list includes **`tests/contract/test_whatif_api.py`** (pins the whatif response
  shape) in addition to `test_input_manual_api.py` and `tests/strategy/test_whatif.py`.

### C6 — Drawer rewire (W3, `web/detail.js` simSection) — owner decision Q4(a)

- The 試算 section stops computing money locally: debounced (~300 ms — mirrors
  `inst-quickadd.js`'s existing lookup debounce) POST `/api/whatif`; renders OLD vs
  NEW pairs (持股/原始均價/調整均價/權重 + the fee/tax/proceeds figures from the
  reply). Loading state while in flight; on error show 「試算暫不可用」 — never
  fabricate, never fall back to local math.
- **Field gap resolved (Senior Review #13):** the drawer's SELL view shows 剩餘市值,
  which `/api/whatif` does not return today → **add `remaining_market_value` to the
  whatif SELL reply** (server-side `remaining_shares × current price`), keeping the
  no-local-money-math rule intact. `/api/whatif`'s request body (`symbol/side/shares/
  price/account_id`) already covers the drawer's inputs (qty+price+side only — no
  fee-override/daytrade inputs exist in the drawer) — no endpoint-body change.
- **Wave-1 constraint (Senior Review #23):** W3 renders all C5/C6 additions into the
  existing dynamic preview containers via JS only — **no `web/trades.html` edit**
  (that file is W4-owned in wave 2).
- The stale「後端 試算 模式」subtitle becomes truthful. spec-03's local-compute
  exception note for this section is retired in comments.
- Draft preview (`web/input.js`) renders the same old-vs-new pairs from C5 fields and
  the new 扣款後現金 line (label with dynamic ccy) under 該帳戶現金.

### C7 — Clear-on-success behavior (W4) — owner decisions Q5 (real filtering)

- **AI flow:** commit writes ONLY checked rows — rebuild the committed `csv_text`
  from header + checked rows' source lines (W4 verifies the preview rows ↔ csv-line
  mapping in the import-preview payload and keeps a per-row index; the
  `warnings_unacknowledged` re-commit uses the SAME filtered text). Full success
  (`skipped == 0`) → clear `#ai-text`, preview rows, `aiCsvText`, `aiImages`; partial
  success → keep ONLY the skipped rows visible with statuses. Success/partial banner
  renders in a new in-pane element (`#ai-result` in the AI pane), no longer in
  `#csv-result`.
- **CSV flow:** full success → clear `#csv-paste` + reset the date-format select;
  partial success → keep the ENTIRE pasted text + banner (raw user data is never
  rewritten). Failure paths keep everything (already correct).
- E2E pins: double-commit after success is impossible (button disabled or empty
  input); unchecked row is NOT written.
- **Row↔line mapping (Senior Review #14, replaces the earlier R3 wording):** the AI
  preview's `n` is a 0-based row index and the AI CSV is generated one-line-per-draft
  by `agents.py::_drafts_to_csv` in draft order, so checked row `n` ↔ csv data line
  `n+1` — feasible, but `_drafts_to_csv` does **no quoting**, so an embedded newline
  in a note would break the invariant. W4 must FIRST make the mapping robust: either
  sanitize newlines in `_drafts_to_csv` (notes are single-line by construction —
  verify) or add an additive per-row `line_no` through `PreviewRow` +
  `input_center::_preview_wire`. The CSV flow never rebuilds a filtered CSV (partial
  keeps the entire paste), so `csv_import.py` is untouched.

### C8 — Opening-inventory contract inversion + column drop (W4) — owner decision Q6 (cleaner)

- **CSV/import contract:** required `account,symbol,shares,original_cost_total,build_date`;
  `original_avg_cost` optional (legacy). If only avg given → derive
  `total = avg × shares` + soft issue `opening_total_derived`; if both given and
  `|avg×shares − total| > max(1 minor unit, 0.5% × total)` → soft `needs_confirm`
  issue `opening_cost_mismatch`. Legacy CSVs still import.
- **Storage:** drop `opening_inventory.original_avg_cost` (SQLite ≥3.35 confirmed —
  bundled 3.49.1; column is not in the PK and unindexed). Idempotent boot-seam
  migration guarded by `pragma table_info`; the CREATE-TABLE DDL in `schema.py` must
  drop the column too (fresh DBs never create it). Note this is the repo's **first
  destructive migration** (precedent is add-only) — pre-migrate backup path already
  exists.
- **COMPLETE reader list (Senior Review #17/#18 — the original list was
  rework-grade incomplete).** `OpeningInventory` model loses the field; every reader
  computes avg on read (`total / shares`):
  - Model-level (mypy-caught): `export/ledgers_report.py`, `export/tax.py`,
    `strategy/whatif.py`, **`portfolio/dashboard.py`** (constructor),
    **`api/routers/symbol.py`** (constructor + the trade-events "open" `price` wire
    field), **`api/routers/ledgers.py`** (constructor + the `GET /ledgers/openings`
    `avg` wire field + the **`edit_opening` PUT write path**, which must accept/store
    total-only), **`api/routers/actions.py`** (constructor).
  - Wire-contract decisions (locked): `GET /ledgers/openings` keeps its `avg` field
    but computes it on read; `symbol.py` open-event `price` likewise computed on
    read; `edit_opening` takes 股數+總成本 (avg derived for display only).
  - **Runtime-only readers mypy CANNOT catch:** `scripts/stress_audit/common.py`
    reads `r["original_avg_cost"]` from raw `SELECT *` (**gate-4 critical** — the
    oracle) and also consumes the `/api/ledgers/openings` `avg` wire field; plus
    raw-SQL test inserts in `tests/data_ingestion/test_validate.py`,
    `test_store_audit.py`, `tests/scheduler/test_backfill_windows.py`,
    `tests/contract/test_ledgers_mutations_api.py`, `test_import_normalize.py`.
  - Model-constructing tests: `test_instruments_delete.py`, `test_cost_basis.py`,
    `test_timeseries.py`, `export/test_ledgers_report.py`,
    `shared/models/test_ledger.py`, `contract/test_input_holdings_api.py`, plus
    `test_opening_inventory.py` + `test_import_template.py`.
  - `scripts/seed_demo.py` has zero opening references — **no change needed**
    (corrects the earlier assumption).
  No calc-engine change: oracle + phase checks key off `original_cost_total`
  (verified), so every money-of-record figure is preserved.
- **Form (`trades.html` + `input.js` initFxOpen):** `#o-symbol` wired to the
  already-populated `#m-symbols` datalist; inputs = 股數 + 原始總成本 (required) +
  build date; 均價 becomes a live READ-ONLY computed hint (`total/shares`, per-ccy
  formatting); required-guard + clear-list updated.
- Template header (`import_templates.py`), the CSV hint string, seed/stress scripts
  emitting opening CSVs, and the round-trip guard tests updated in lockstep.

## 3. Definition of done (per agent) & Phase-3 gates

Per agent: unit/contract/e2e tests for every changed behavior; self-check; internal
senior review; unified report (完成項/決策/偏離/風險/測試證據). No git commands.

Central gates (orchestrator, Phase 3):
1. Full pytest (detached gate scripts) — 0 FAILED / 0 ERROR.
2. **BARE** `mypy --strict --no-incremental` over the FULL scope (≈522+ files).
3. `ruff` clean.
4. `/stress-audit` — A6 touches the opening-inventory input contract (money-of-record
   source data); oracle + phase scripts must stay green with the new contract.
5. Golden-payload / contract / template round-trip suites green (updated where the
   contract legitimately changed, each with a written justification).
6. id-contract sweep (markup↔JS) re-run — no new dead zones (new `#ai-result`,
   `#o-avg-view` elements must be wired).
7. Traceability matrix: A1–A6 → implementing diff → test evidence.
8. Demo deploy + behavior probe (extend probe: MY resolve happy path stubbed, drawer
   old/new render, AI clear-on-success, opening simplified flow) + `verify_live`.

Ship as **v0.1.21** via `/ship-version` (CHANGELOG, whatsnew catalog entries with
verified targets, asset stamps, LESSONS if any, bilingual protocol).

## 4. Risks & mitigations

- **R1 (W1):** Bursa directory fetch may be blocked/format-shifted at dev time →
  fallback source = Bursa's published XLS/API mirror; if no authoritative list is
  obtainable, ship prompt-v2 alone and defer the registry (escalate, do not fabricate).
- **R2 (W3):** RESOLVED by review — `WhatIfBody` (symbol/side/shares/price/account_id)
  already covers the drawer's inputs; the only endpoint change is the additive
  `remaining_market_value` + `old_*` fields.
- **R3 (W4):** RESOLVED into C7's row↔line mapping rule — the fragility is
  `_drafts_to_csv`'s no-quoting, and the fallback seams are `agents.py` +
  `PreviewRow` + `_preview_wire` (not `csv_import.py`).
- **R4 (W4):** mypy strict catches model-field removals ONLY for model-level readers;
  raw-SQL readers (`stress_audit/common.py`, several test inserts) fail at RUNTIME —
  hence the exhaustive C8 enumeration; the pytest + stress gates are the backstop and
  the brief pre-lists every known site so gates verify rather than discover.
- **R5 (W2):** raising `_MAX_BYTES` to 1.5 MB on an e2-micro host — bounded by single
  in-flight fetch + 8 000-char text cap; memory delta ≈ transient 1.5 MB, acceptable.

## 4b. Execution deviation record (orchestrator-approved)

- **W2:** fallback-stage text that PASSES the prose guard is labeled `ok` (with the
  stage in `detail`) rather than `salvaged` — `salvaged` is reserved for
  prose-guard-failing text. More truthful than the literal C4 wording; approved.
  FM10's CSS-soup case changes from discard to `salvaged` (intended, test renamed).
- **W3:** `web/mock-data.js` does not exist (deleted in an earlier round — the plan
  inherited a stale reference); the payload contract is documented via pinned
  contract tests + field docstrings, per the R6-E precedent. Approved; C5.4 void.
- **W3:** `cash_after` sits at the response top level (not inside `account_cash`) so
  existing exact-dict pins stay byte-identical; 剩餘市值 floors at 0 on oversell while
  `remaining_shares` itself stays unfloored. Approved.

## 5. System Senior Review record

Adversarial plan-vs-code audit completed 2026-07-21 (read-only, file:line evidenced).
Verdict: **APPROVE-WITH-AMENDMENTS** — all seams confirmed real; amendments applied
in place above: C2 registration-path caveats (#3), C3 `AI_INPUT_PROMPT_VERSION` bump +
exact drift-test list (#5/#6), C5 `old_weight` requires `_new_weight` refactor (#9) +
`test_whatif_api.py` in W3 (#10) + sell-branch avg note, C6 `remaining_market_value`
addition (#13) + no-`trades.html`-in-wave-1 constraint (#23), C7 row↔line mapping
rule re-pointed at `_drafts_to_csv` (#14), C8 complete reader enumeration incl.
`dashboard/symbol/ledgers/actions` + `stress_audit/common.py` + raw-SQL tests
(#17/#18) with W4 re-estimated. Ownership disjointness verified (#21); no
`manual.py` opening path exists (#24); ship-time seams need no agent brief (#24).
