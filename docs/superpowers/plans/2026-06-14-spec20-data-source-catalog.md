# Spec 20 — Data-Source Catalog & External-Snapshot Ingest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking. Work in an isolated git worktree off `main`.

**Goal:** Land the free-tier / critical-chain slice of spec 20: persist all fetched external
data into `external_snapshots`, derive chips/sentiment variable values from snapshots, flip the
7 chips/sentiment prompt variables to `available=true`, add the free quote-fallback providers,
expand the source catalog (with `pending`-status token-gated adapters), and pre-test all
no-token source types via the probe harness. Token-gated sources (alphavantage/finnhub/fred/
schwab) are catalogued + stubbed only (`status:pending`), validated later when a key is entered.

**Architecture:** Two seams (spec 20.3). (A) Quote/FX/dividend numbers of record go through the
existing `pricing/` registry + providers. (B) chips/sentiment/index data is fetched by light
single-source clients, stored append-only in `external_snapshots`, derived by pure Decimal
functions, and assembled into prompt variables. Layering (spec 20.3 / 06a): ingest jobs live in
`scheduler/jobs.py` (no `data_ingestion` import — read the symbol universe via direct SQL on
`instruments`); snapshot reads + derivations are done in the API router and fed into
`VarContext`; `llm_insight` imports neither `pricing` nor `data_ingestion`.

**Tech Stack:** Python 3.12, sqlite3 (no ORM), Decimal money, pydantic v2, FastAPI, requests,
yfinance, pytest + FastAPI TestClient + pytest-socket (network ban), mypy --strict, ruff.

**Gates (run via repo `.venv/Scripts/python`):**
`python -m pytest -q` · `python -m mypy --strict portfolio_dash` · `python -m ruff check`.

---

## File Structure

**Create:**
- `portfolio_dash/pricing/snapshots_store.py` — `external_snapshots` DDL + append-only write + latest-N read.
- `portfolio_dash/pricing/finmind_datasets.py` — FinMind Free-tier dataset client (DB-backed token).
- `portfolio_dash/pricing/sentiment_source.py` — VIX (yfinance `^VIX`) + CNN Fear&Greed client.
- `portfolio_dash/pricing/index_source.py` — yfinance index client (`^TWII`/`^GSPC`/`^KLSE`).
- `portfolio_dash/pricing/providers/twstock_provider.py` — TW intraday quote fallback.
- `portfolio_dash/pricing/providers/stockprices_dev_provider.py` — US quote fallback (latest-only).
- `portfolio_dash/pricing/providers/klsescreener_provider.py` — MY 3-dp string quote fallback.
- `portfolio_dash/pricing/providers/malaysiastock_provider.py` — MY 3-dp string quote fallback.
- `portfolio_dash/pricing/ingest.py` — snapshot ingest functions (call clients, write store).
- `portfolio_dash/portfolio/external_signals.py` — pure Decimal derivation functions (spec 20.5).
- `tests/pricing/test_snapshots_store.py`, `tests/pricing/test_finmind_datasets.py`,
  `tests/pricing/test_sentiment_source.py`, `tests/pricing/test_index_source.py`,
  `tests/pricing/test_new_quote_providers.py`, `tests/pricing/test_ingest.py`,
  `tests/portfolio/test_external_signals.py`, `tests/contract/test_prompts_external_vars.py`,
  `tests/scheduler/test_ingest_jobs.py`.

**Modify:**
- `portfolio_dash/pricing/datasources_store.py` — `SourceInfo` += `provides: list[str]`,
  `status: str`; expand `SOURCE_INFO` to the full spec-20.1 catalog.
- `portfolio_dash/pricing/defaults.py` — extend `DEFAULT_PROVIDER_ORDER` with new fallbacks;
  register new providers in `default_registry`.
- `portfolio_dash/api/routers/datasources.py` — `_source_wire` emit `provides`/`status`;
  wire the free-source provider probes into `probe_source`.
- `portfolio_dash/api/routers/prompts.py` — `_build_context` reads snapshots + derives + feeds
  new `VarContext` fields.
- `portfolio_dash/llm_insight/variables.py` — chips/sentiment 7 vars `available=true`;
  `value_for` handlers reading `VarContext`; degrade to `{"unavailable": true}`.
- `portfolio_dash/api/app.py` — lifespan: `snapshots_store.ensure_tables`; register ingest jobs.
- `portfolio_dash/scheduler/jobs.py` — 5 ingest job funcs (direct-SQL universe; 3-fail warn).
- `tests/conftest.py` — `golden_db` creates `external_snapshots` (empty → vars degrade).
- `scripts/probe/adapters/*` — extend finmind_src (5 datasets) + my_src (klse/malaysiastock)
  + new sentiment/index probe; `docs/probes/2026-06-08-data-source-probe-results.md` refresh.

---

## Task 1: `external_snapshots` store (append-only)

**Files:** Create `portfolio_dash/pricing/snapshots_store.py`, `tests/pricing/test_snapshots_store.py`.

- [ ] **Step 1: Write failing tests.** Cover: `ensure_tables` idempotent; `add_snapshot` then
  `latest_snapshots(source,dataset,symbol)` returns most-recent-`fetched_at` payload parsed back
  to a dict; append-only (two writes same key → two rows, latest wins); `latest_series(...,n)`
  returns up to n rows ordered by `as_of` desc; missing → `[]`/`None`.

```python
from datetime import date, datetime
from portfolio_dash.pricing import snapshots_store as S

def test_add_and_latest(tmp_conn):
    S.ensure_tables(tmp_conn)
    S.add_snapshot(tmp_conn, source="finmind", dataset="institutional", symbol="2330",
                   as_of=date(2026, 6, 11), payload={"net": "1200"},
                   fetched_at=datetime(2026, 6, 11, 18, 0))
    got = S.latest_snapshot(tmp_conn, source="finmind", dataset="institutional", symbol="2330")
    assert got is not None and got.payload == {"net": "1200"} and got.as_of == date(2026, 6, 11)
    assert S.latest_snapshot(tmp_conn, source="finmind", dataset="margin", symbol="2330") is None
```

- [ ] **Step 2:** Run `python -m pytest tests/pricing/test_snapshots_store.py -v` → FAIL.
- [ ] **Step 3: Implement.** DDL exactly per spec 20.4 (INTEGER PK AUTOINCREMENT + index).
  `ensure_tables(conn)` executescript + commit. Model `Snapshot(BaseModel)`: source, dataset,
  symbol|None, as_of: date, payload: dict[str, Any], fetched_at: datetime. `add_snapshot(...)`
  INSERT (payload via `json.dumps`, dates ISO). `latest_snapshot(...)` → newest `fetched_at` for
  the key (symbol may be None → `symbol IS NULL`). `latest_series(..., n)` → newest n distinct
  `as_of` (one row per as_of, newest fetch). Parse payload via `json.loads`. Provide a `tmp_conn`
  fixture in this test module (sqlite `:memory:` with `row_factory = sqlite3.Row`) if not shared.
- [ ] **Step 4:** Run tests → PASS. `mypy --strict` clean.
- [ ] **Step 5: Commit** `feat(pricing): external_snapshots append-only store (spec 20.4)`.

## Task 2: FinMind Free-tier dataset client

**Files:** Create `portfolio_dash/pricing/finmind_datasets.py`, `tests/pricing/test_finmind_datasets.py`.

- [ ] **Step 1: Write failing tests.** Monkeypatch `requests.get` to return a recorded FinMind
  envelope (`{"msg":"success","status":200,"data":[...]}`). Assert `fetch_dataset(conn, dataset,
  data_id, start_date)` returns the `data` list; token read via `datasources_store.get_api_key`
  (seed a finmind key in a tmp conn); no token → raises `MissingTokenError`; HTTP error →
  raises. Use fixtures under `tests/pricing/fixtures/finmind/` if present, else inline dicts.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement.** `FINMIND_DATASETS: dict[str,str]` mapping logical name →
  FinMind dataset id: `institutional→TaiwanStockInstitutionalInvestorsBuySell`,
  `margin→TaiwanStockMarginPurchaseShortSale`, `valuation→TaiwanStockPER`,
  `monthly_revenue→TaiwanStockMonthRevenue`, `financials→TaiwanStockFinancialStatements`.
  `fetch_dataset(conn, *, dataset, data_id, start_date) -> list[dict]`: resolve token via
  `datasources_store.get_api_key(conn, "finmind")`; raise `MissingTokenError` if falsy; GET
  `https://api.finmindtrade.com/api/v4/data` params `{dataset, data_id, start_date, token}`,
  timeout 20, `raise_for_status`, return `resp.json().get("data") or []`. Keep raw values
  as-is (Decimal conversion happens in derivations). Module must be unit-testable without network
  (all I/O via `requests.get`, monkeypatched).
- [ ] **Step 4:** Tests → PASS; mypy clean.
- [ ] **Step 5: Commit** `feat(pricing): FinMind Free-tier dataset client (spec 20.6)`.

## Task 3: Sentiment + index source clients

**Files:** Create `portfolio_dash/pricing/sentiment_source.py`, `portfolio_dash/pricing/index_source.py`,
`tests/pricing/test_sentiment_source.py`, `tests/pricing/test_index_source.py`.

- [ ] **Step 1: Write failing tests.** sentiment: monkeypatch the VIX getter + CNN getter →
  `fetch_vix()` returns `Decimal` close; `fetch_fear_greed()` returns `{"score": Decimal, "rating": str}`;
  CNN unreachable → returns `None` (degrade). index: monkeypatch yfinance batch → `fetch_indices()`
  returns `{"^TWII": Decimal, "^GSPC": Decimal, "^KLSE": Decimal}`; missing symbol omitted.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement.** sentiment_source: `fetch_vix() -> Decimal | None` via yfinance
  `^VIX` last close (`Decimal(str(x))`); `fetch_fear_greed() -> dict | None` GET
  `https://production.dataviz.cnn.io/index/fearandgreed/graphdata` with a desktop UA header,
  timeout 10, parse `fear_and_greed.score`/`rating` → `{"score": Decimal(str(score)), "rating": str}`;
  any failure → `None`. index_source: `fetch_indices() -> dict[str, Decimal]` yfinance batch of
  the three symbols, `Decimal(str(close))`, omit misses. All HTTP/yfinance calls isolated for
  monkeypatch; **no network in tests** (pytest-socket).
- [ ] **Step 4:** Tests → PASS; mypy clean.
- [ ] **Step 5: Commit** `feat(pricing): VIX/Fear&Greed + index source clients (spec 20.7)`.

## Task 4: Derivation pure functions

**Files:** Create `portfolio_dash/portfolio/external_signals.py`, `tests/portfolio/test_external_signals.py`.

- [ ] **Step 1: Write failing hand-checked tests** (no float). Cover each function in spec 20.5:
  `consecutive_buy_days([+,+,-,+,+,+]) -> 3` (trailing run of positives); `net_buy_sum(seq, 3)`;
  `chg_pct(110, 100) == Decimal("0.1")`, `chg_pct(5, 0) is None`; `yoy`/`mom` similarly None on
  denom≤0; `percentile(Decimal("15"), hist) -> Decimal` in [0,1] (rank/len); `vix_zone`:
  `<15→"low"`, `15–25→"normal"`, `25–35→"elevated"`, `≥35→"high"`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement** pure Decimal functions, signatures per spec 20.5; every ratio
  returns `None` when its denominator ≤ 0 (domain-ledger discipline, mirrors `technicals.py`).
  No I/O, no conn.
- [ ] **Step 4:** Tests → PASS; mypy clean.
- [ ] **Step 5: Commit** `feat(portfolio): external-signal derivations (spec 20.5)`.

## Task 5: New free quote-fallback providers

**Files:** Create the 4 `*_provider.py` (Task list under File Structure),
`tests/pricing/test_new_quote_providers.py`; modify `portfolio_dash/pricing/defaults.py`.

- [ ] **Step 1: Write failing tests.** Each provider: `supports(DataType.QUOTE_LATEST, <market>)`
  True for its market, False otherwise & for history/fx/dividend; `fetch_quote_latest` with a
  monkeypatched HTTP/lib layer returns `PriceRow` with `Decimal(str(...))` price + `source=name`
  + correct market quantization (MY string sources preserve 3-dp). Registry order test: TW chain
  ends with `twstock`, US has `stockprices_dev` fallback, MY has `klsescreener`/`malaysiastock`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement** each provider subclassing `ProviderBase` (read
  `pricing/providers/twse_provider.py` + `yfinance_provider.py` for the established pattern):
  `name`, `supports`, `fetch_quote_latest`; raise/empty on failure so the registry falls through.
  `stockprices_dev` latest-only (no history). MY string providers parse the 3-dp `data-value`
  string → `Decimal` directly (no float). Wire all 4 into `DEFAULT_PROVIDER_ORDER` +
  `default_registry` per spec 20.8 default orders.
- [ ] **Step 4:** Tests → PASS; mypy clean. Confirm existing `tests/pricing/test_defaults.py`
  still green (update its expected order if it asserts the chains).
- [ ] **Step 5: Commit** `feat(pricing): twstock/stockprices.dev/klse/malaysiastock quote fallbacks (spec 20.8)`.

## Task 6: Snapshot ingest functions + scheduler jobs

**Files:** Create `portfolio_dash/pricing/ingest.py`, `tests/pricing/test_ingest.py`,
`tests/scheduler/test_ingest_jobs.py`; modify `portfolio_dash/scheduler/jobs.py`,
`portfolio_dash/api/app.py`.

- [ ] **Step 1: Write failing tests.** `ingest.py`: each ingest fn (chips/valuation/
  fundamentals/sentiment/index) given a monkeypatched client writes the expected
  `external_snapshots` rows for the TW universe. Universe read = direct SQL
  `SELECT symbol FROM instruments WHERE market='TW'` (seed a couple). Jobs: `run_job_func`
  wraps each ingest in the spec-15 `job_runs` try/except; on 3 consecutive failed runs it upserts
  `data_source_health` status `error` + records a warn (assert health row). No `data_ingestion`
  import in `scheduler/jobs.py` (assert by grepping the module in a test, or by construction).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement.** `ingest.py` functions take `conn` + inject-able client callables
  (default to the real clients) so tests monkeypatch easily; convert nothing to money here — store
  raw payload. Batch FinMind by symbol with small backoff hook (sleep injected/no-op in tests).
  `scheduler/jobs.py`: 5 job funcs registered with the spec-15 runner; symbol universe via direct
  SQL; 3-consecutive-fail → `datasources_store.upsert_health(..., status="error", detail=...)`
  + warn log. `app.py` lifespan: `snapshots_store.ensure_tables(conn)` + register the 5 jobs in
  `schedule_config` (kind/payload per spec 15) without auto-running them in tests.
- [ ] **Step 4:** Tests → PASS; mypy clean.
- [ ] **Step 5: Commit** `feat(pricing,scheduler): external-snapshot ingest jobs (spec 20.4)`.

## Task 7: Catalog expansion + datasources router

**Files:** Modify `portfolio_dash/pricing/datasources_store.py`,
`portfolio_dash/api/routers/datasources.py`; update `tests/contract/test_datasources_api.py`,
`tests/pricing/test_defaults.py` as needed.

- [ ] **Step 1: Write/extend failing tests.** GET `/api/datasources` now returns every spec-20.1
  source; each wire object includes `provides: list[str]` and `status: "live"|"pending"|"blocked"`.
  `pending` token sources show `status:"pending"` with `token_masked:null`. Existing masked-key /
  fallback behaviour unchanged (keep prior assertions green).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement.** `SourceInfo` += `provides: list[str]`, `status: str` (default
  `"live"`). Expand `SOURCE_INFO` to the full catalog (spec 20.1): existing 4 live + twstock/
  stockprices_dev/klsescreener/malaysiastock/cnn_fng (`live`) + alphavantage/finnhub/fred/schwab/
  pytrends (`pending`) + bursa (`blocked`). Keep `divtracker`/`newsapi`/`fx_ecb`/`alphavantage`
  ids stable if referenced elsewhere. `_source_wire` emit `provides`/`status`. `seed` unchanged
  (still one row per id). `probe_source`: wire the free-source provider probes (a single minimal
  `fetch_quote_latest` for the quote providers; sentiment/index a minimal client call); keep the
  neutral "not implemented" result only for truly unwired ids.
- [ ] **Step 4:** Tests → PASS; mypy clean.
- [ ] **Step 5: Commit** `feat(pricing,api): full data-source catalog + provides/status (spec 20.1)`.

## Task 8: Token-gated adapters (pending, stub-level)

**Files:** Create `portfolio_dash/pricing/providers/alphavantage_provider.py`,
`finnhub_provider.py`; `portfolio_dash/pricing/fred_source.py`; `tests/pricing/test_pending_adapters.py`.
(Schwab = OAuth, defer to a doc note — no stub this round; already catalogued `pending`.)

- [ ] **Step 1: Write failing tests.** Each adapter: constructible; `supports(...)` returns True
  only for its declared data types **and only when a key is present** (mirror FinMind's
  token-gated `supports`); with no key `supports` is False (so the registry never calls them).
  No network call is made in any test.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement** minimal adapters: `name`, key-gated `supports`, and a real
  `fetch_*` body guarded so it is never exercised without a key (raise/empty if unkeyed). These
  are wired into `default_registry` but, being key-gated, stay inert until a key is set. fredapi
  client similarly stubbed (macro, future panel). Mark each with a module docstring: `pending —
  validated when a key is entered (spec 20.9)`.
- [ ] **Step 4:** Tests → PASS; mypy clean.
- [ ] **Step 5: Commit** `feat(pricing): pending token-gated adapters (alphavantage/finnhub/fred) (spec 20.9)`.

## Task 9: Wire chips/sentiment variables (flip available + value_for)

**Files:** Modify `portfolio_dash/llm_insight/variables.py`, `portfolio_dash/api/routers/prompts.py`;
create `tests/contract/test_prompts_external_vars.py`; modify `tests/conftest.py`.

- [ ] **Step 1: Write failing tests.** (a) `GET /api/prompt-vars` now reports the 7 chips/sentiment
  vars `available:true` (count of available = 24). (b) With an **empty** golden_db (no snapshots),
  `POST /api/prompts/preview {body:"{{institutional_json}}",scope:"portfolio"}` renders
  `{"unavailable": true}` (degrade) and 200. (c) After seeding an `external_snapshots` row for
  2330 institutional, preview renders the derived value (e.g. contains `consecutive_buy_days`).
  (d) `market_sentiment_json` renders `{"unavailable": true}` with no snapshot; with a VIX+F&G
  snapshot it renders `vix`/`zone`/`fng`. Keep all prior `test_prompts_api.py` assertions green.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement.** `variables.py`: set `available=True` for the 7 tokens; extend
  `VarContext` with fed fields (e.g. `chips: dict[str, Any]`, `sentiment: dict[str, Any]`,
  `index_quotes: dict[str, Any]`, defaulting empty); add `value_for` handlers returning the fed
  value or `{"unavailable": True}` when absent. **No conn, no compute of record in
  `llm_insight`.** `prompts.py` `_build_context`: read latest snapshots via
  `pricing.snapshots_store`, derive via `portfolio.external_signals`, assemble the chips/sentiment
  dicts, feed into `VarContext`. `conftest.py` `golden_db`: `snapshots_store.ensure_tables` so
  the table exists but is **empty** (every external var degrades → existing suites stay green).
- [ ] **Step 4:** Tests → PASS; mypy clean. Re-run full suite.
- [ ] **Step 5: Commit** `feat(llm_insight,api): chips/sentiment variables live from snapshots (spec 20.2)`.

## Task 10: Probe pre-test + docs refresh

**Files:** Modify `scripts/probe/adapters/finmind_src.py`, `scripts/probe/adapters/my_src.py`,
add a sentiment/index probe adapter; refresh `docs/probes/2026-06-08-data-source-probe-results.md`
(regen the auto-section only via `run_all` if a token is present, else hand-note the new rows).

- [ ] **Step 1:** Extend the finmind probe adapter to exercise the 5 datasets; my_src to cover
  klsescreener + malaysiastock 3-dp strings; add VIX/CNN/index reachability checks. Token sources
  recorded as `skipped (no key)`.
- [ ] **Step 2:** Run `python -m scripts.probe.run_all` (no-token run); confirm it does not crash
  and records the no-key skips; update the probe results doc's curated synthesis with spec-20
  status (live / pending / blocked) — keep it a bounded edit.
- [ ] **Step 3: Commit** `test(probe): spec-20 source pre-test + results refresh (spec 20.11)`.

---

## Final integration (controller, after all tasks)

- Full gates green via `.venv`: `pytest` · `mypy --strict portfolio_dash` · `ruff check`.
- CHANGELOG entry (controller only) + integrity `grep -c "^## \[v"`.
- Dispatch the global-module-final-senior-review subagent (Opus Max) over the whole diff
  (correctness, layering, money-type, no-LLM-numbers, degradation, test coverage). Fix loop.

## Self-Review (against spec 20)

- §20.4 external_snapshots → Task 1/6. §20.5 derivations → Task 4. §20.6 FinMind → Task 2.
  §20.7 sentiment/index → Task 3. §20.8 free providers → Task 5. §20.1 catalog → Task 7.
  §20.9 pending → Task 8. §20.2 variable wiring → Task 9. §20.11 probe → Task 10. ✓ all covered.
- Layering: `llm_insight` imports no pricing/data_ingestion (Task 9); `scheduler/jobs` no
  data_ingestion (Task 6, direct SQL). ✓
- Degradation: empty snapshots → `{"unavailable": true}`; golden_db empty keeps suites green
  (Task 9). ✓
- Money discipline: all parse via `Decimal(str(x))`; derivations pure Decimal; ratios None on
  denom≤0 (Task 4). ✓
- No numbers of record from LLM: derivations in `portfolio/`, assembly in `llm_insight` (Task 4/9). ✓
