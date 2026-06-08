# `data_ingestion/` Implementation Plan (4 ledgers + instruments + CSV/manual/AI Agents Input)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Validate + persist human/data-source input into the four canonical ledgers (transactions, dividends, fx_conversions, opening_inventory) + the instruments registry, via manual entry, CSV import (preview→confirm), and AI Agents Input (NL→draft→confirm). Fee/tax auto-computed from per-account config rules.

**Architecture:** Resolve (symbol) → validate → compute fee/tax → **preview** → on confirm → **store** (the only writer of the ledgers). `data_ingestion/` imports only `shared/*` (incl. a new `shared/llm.py` LiteLLM client). It never computes P&L (`portfolio/`), fetches prices (`pricing/`), or renders UI (`web_ui/`).

**Tech Stack:** Python 3.12, Pydantic v2, stdlib `sqlite3` (via `shared.db`), `Decimal` (canonical TEXT via `shared.money`), `litellm` (new, stage 6). Tests: pytest; LLM is **mocked** (no live network in the suite).

**Reference:** spec `docs/superpowers/specs/2026-06-09-data-ingestion-design.md`; shared models `portfolio_dash/shared/models/{assets,ledger,enums,types}.py`; the `pricing/store.py` upsert/read pattern; `markets-and-fees.md` (fee/tax rules); `domain-ledger.md` (dividend models, sell>holdings rule).

**Money discipline:** never float; persist Decimal via `shared.money.to_db`/`from_db`; fee/tax `quantize` per market (TW integer). Verify shared helper names before use.

---

## File Structure

```
portfolio_dash/
  shared/llm.py                 # stage 6: LiteLLM client + structured output + model registry + usage log
  data_ingestion/
    __init__.py
    schema.py                   # all ledger + instruments + accounts + llm_usage tables
    config_seed.py              # accounts + fee-rule-sets + LLM model registry defaults (from Settings)
    fees.py                     # per-account fee/tax engine (+ rule snapshot)
    resolve.py                  # symbol resolution: fuzzy (+ LLM fallback hook, filled stage 7)
    holdings.py                 # current shares per (account, symbol) from transactions
    validate.py                 # record validation (sell>holdings, fields)
    store.py                    # write/read the ledgers + instruments
    preview.py                  # generic ImportPreview (rows + issues + computed fee/tax) -> commit
    csv_import.py               # CSV parse -> records (per ledger)
    manual.py                   # single-record entry pipeline
    agents.py                   # stage 7: AI Agents Input (NL -> draft) + LLM resolve fallback
tests/data_ingestion/...
```

---

# STAGE 1 — schema, config seed, instruments + fuzzy resolution

## Task 1: `schema.py` — all tables

**Files:** Create `portfolio_dash/data_ingestion/__init__.py`, `schema.py`; `tests/data_ingestion/__init__.py`, `conftest.py`, `test_schema.py`.

- [ ] **Step 1: `conftest.py`** — in-memory DB fixture calling `create_tables` (mirror `tests/pricing/conftest.py`).
- [ ] **Step 2: Failing test** `test_schema.py` — `create_tables` idempotent; asserts tables exist: `accounts, instruments, transactions, dividends, fx_conversions, opening_inventory, llm_usage`.
- [ ] **Step 3: Run → FAIL.**
- [ ] **Step 4: Implement `schema.py`** — `create_tables(conn)` with `CREATE TABLE IF NOT EXISTS` for (Decimal as TEXT):
  - `accounts(account_id PK, name, broker, settlement_ccy, funding_ccy, fee_rule_set, dividend_model)`
  - `instruments(symbol PK, market, quote_ccy, sector, name)`
  - `transactions(id INTEGER PK AUTOINCREMENT, account_id, symbol, side, quantity, price, fees, tax, trade_date, fee_rule_snapshot, note)`
  - `dividends(id PK, account_id, symbol, date, type, gross, withholding, net, reinvest_shares, reinvest_price)`
  - `fx_conversions(id PK, account_id, date, from_ccy, from_amount, to_ccy, to_amount)`
  - `opening_inventory(account_id, symbol, shares, original_avg_cost, original_cost_total, build_date, PRIMARY KEY(account_id, symbol))`
  - `llm_usage(id PK, ts, model, agent, input_tokens, output_tokens, cost)`
- [ ] **Step 5: Run → PASS.** mypy + ruff clean.
- [ ] **Step 6: Commit** `feat(data_ingestion): schema for ledgers + instruments + llm_usage`.

## Task 2: config seed — accounts, fee-rule-sets, LLM model registry

**Files:** Modify `portfolio_dash/shared/config.py` (Settings); create `data_ingestion/config_seed.py`, `tests/data_ingestion/test_config_seed.py`.

- [ ] **Step 1:** Extend `Settings` with config-driven defaults (verify existing Settings shape first):
  - `accounts`: the four (`tw_broker`→TW/TWD/TWD; `schwab`→US/USD/TWD; `moomoo_my_us`→US/USD/MYR; `moomoo_my_my`→MY/MYR/MYR) each with a `fee_rule_set` key + dividend model.
  - `fee_rules`: per set — TW (`brokerage=0.001425, discount=1.0, min_fee=20, tax_normal=0.003, tax_etf=0.001, tax_daytrade=0.0015, round=integer`); US (`commission=0, sec_fee=...~0`); MY (`brokerage, clearing=0.0003, clearing_cap=1000, stamp_duty=..., sst=...`). All editable.
  - `llm`: `endpoint`, `api_key`, `models` (list: `{id, input_price_per_mtok, output_price_per_mtok}`), `active_model`.
  Use plain Python dict/Pydantic submodels with defaults (config-as-code; settings-page override later).
- [ ] **Step 2: Failing test** — `config_seed.seed_accounts(conn)` writes the four accounts; `get_fee_rule_set("tw_broker")` returns the TW rates (0.001425 / 0.003 / 0.001 / 0.0015, min 20).
- [ ] **Step 3–5:** Implement `config_seed.py` (`seed_accounts`, accessors for fee rules + llm config), run → PASS, gates clean.
- [ ] **Step 6: Commit** `feat(data_ingestion): account + fee-rule + LLM-model config seed`.

## Task 3: instruments store + fuzzy resolution

**Files:** Create `data_ingestion/store.py` (start it: instruments part), `resolve.py`, `tests/data_ingestion/test_resolve.py`.

- [ ] **Step 1: Failing test** `test_resolve.py`:
  - `upsert_instrument`/`get_instrument` round-trip.
  - `resolve(conn, account, "2330")` → exact symbol hit (InstrumentRef-like).
  - `resolve(conn, account, "台積電")` after an instrument named "台積電 (2330)" is registered → fuzzy match proposes 2330.
  - unknown name → returns a `Resolution(status="needs_ai", candidates=[])` (LLM fallback wired in stage 7; here it just signals).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** `store.py` instruments CRUD + `resolve.py`:
  - `resolve(conn, account, raw) -> Resolution` where `Resolution(status: Literal["exact","fuzzy","needs_ai"], instrument: Instrument | None, candidates: list[Instrument])`.
  - exact: symbol match in `instruments`.
  - fuzzy: normalize (strip/upper, drop punctuation) + `difflib.get_close_matches` over instrument names/symbols; confident (ratio ≥ threshold) → `fuzzy` with the instrument; else `needs_ai`.
  - account gives a market hint to disambiguate.
- [ ] **Step 4: Run → PASS.** mypy + ruff clean.
- [ ] **Step 5: Commit** `feat(data_ingestion): instruments store + fuzzy symbol resolution`.

---

# STAGE 2 — fee/tax engine

## Task 4: `fees.py` — per-account fee + tax, with snapshot

**Files:** Create `data_ingestion/fees.py`, `tests/data_ingestion/test_fees.py`.

- [ ] **Step 1: Failing test** `test_fees.py` (fixed fixtures, pure):
```python
# TW buy: 0.1425% of 600*1000=600000 -> 855 fee (>=20), tax 0 on buy
# TW sell 現股: tax 0.3% of proceeds; fee min 20 enforced on tiny trades; integer rounding
# TW ETF sell: tax 0.1%; TW 當沖 flag: tax 0.15%
# US Schwab: fee ~0; MY: brokerage + clearing 0.03% (cap 1000) + stamp + SST
# snapshot returned alongside the amounts
```
  Assert exact Decimal fee/tax values + that a `snapshot` dict of applied rates is returned.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement `fees.py`**:
  - `compute_fees(account, instrument, side, quantity, price, *, daytrade=False, rules) -> FeeResult(fee: Decimal, tax: Decimal, snapshot: dict)`.
  - Dispatch by the account's market/fee-rule-set; TW: `fee=max(round(brokerage*discount*notional), min_fee)`, sell `tax=round(rate*proceeds)` (rate by 現股/ETF/當沖; ETF from `instrument`), buy `tax=0`; round to integer NT$ (`quantize(Decimal("1"), ROUND_HALF_UP)`). US/MY per their rules (MY clearing capped). Quantize per market minor unit (`shared.money`).
  - `snapshot` records the exact rates/flags used.
- [ ] **Step 4: Run → PASS.** mypy + ruff clean.
- [ ] **Step 5: Commit** `feat(data_ingestion): per-account fee/tax engine with rule snapshot`.

---

# STAGE 3 — transactions (holdings, validation, store, manual, CSV)

## Task 5: holdings + validation

**Files:** Create `data_ingestion/holdings.py`, `validate.py`, `tests/data_ingestion/test_validate.py`.

- [ ] **Step 1: Failing test**: `current_shares(conn, account, symbol)` sums BUY−SELL from `transactions`; `validate_transaction(...)` raises/returns issues for: sell qty > holdings (block), unknown account, unresolved symbol, non-positive qty/price, bad side/date.
- [ ] **Step 2–4:** Implement `holdings.current_shares`; `validate.validate_transaction(conn, draft) -> list[Issue]` (empty = ok); the sell>holdings case yields a blocking `Issue(kind="sell_exceeds_holdings", needs_confirm=True)`. Run → PASS; gates.
- [ ] **Step 5: Commit** `feat(data_ingestion): holdings sum + transaction validation (sell>holdings block)`.

## Task 6: transactions store

**Files:** Modify `store.py` (transactions write/read); `tests/data_ingestion/test_store_transactions.py`.

- [ ] **Step 1: Failing test**: `insert_transaction(conn, tx, *, fee_snapshot)` persists (Decimal via to_db, snapshot as JSON TEXT); `list_transactions(conn, account=?, symbol=?)` reads back ascending by trade_date.
- [ ] **Step 2–4:** Implement; run → PASS; gates.
- [ ] **Step 5: Commit** `feat(data_ingestion): transactions store (with fee/tax snapshot)`.

## Task 7: manual transaction entry pipeline

**Files:** Create `data_ingestion/manual.py`; `tests/data_ingestion/test_manual.py`.

- [ ] **Step 1: Failing test**: `enter_transaction(conn, raw_input, *, rules, confirm=False)`:
  - resolves symbol, computes fee/tax (if blank), validates;
  - returns a `Draft` (record + computed fee/tax + issues) WITHOUT writing when `confirm=False`;
  - writes only when `confirm=True` and no blocking issues;
  - blank fee/tax auto-filled; provided fee/tax preserved (override).
- [ ] **Step 2–4:** Implement the pipeline (resolve→fees→validate→[confirm]→store). Run → PASS; gates.
- [ ] **Step 5: Commit** `feat(data_ingestion): manual transaction entry (resolve→fee/tax→validate→confirm)`.

## Task 8: CSV import (transactions) + generic preview/commit

**Files:** Create `data_ingestion/preview.py`, `csv_import.py`; `tests/data_ingestion/test_csv_transactions.py` + a fixture CSV under `tests/data_ingestion/fixtures/`.

- [ ] **Step 1: Failing test**: parse a transaction CSV (`account, symbol, side, date, shares, price[, fee, tax, note]`) → build an `ImportPreview` (per-row: resolved instrument, computed fee/tax, issues); blank fee/tax auto-filled; a sell>holdings row flagged (not dropped); `commit_preview(conn, preview, accept=[row ids])` writes only accepted, issue-free rows and returns a summary.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement**:
  - `preview.py`: `ImportPreview(rows: list[PreviewRow])`, `PreviewRow(index, draft, fee, tax, issues, resolution)`; `commit_preview(conn, preview, accept) -> ImportSummary`. (Generic — reused by later ledgers.)
  - `csv_import.py`: `build_transaction_preview(conn, csv_text, *, rules) -> ImportPreview` (parse → resolve → fees → validate per row).
- [ ] **Step 4: Run → PASS.** mypy + ruff clean.
- [ ] **Step 5: Commit** `feat(data_ingestion): transaction CSV import with preview→confirm`.

---

# STAGE 4 — opening_inventory

## Task 9: opening_inventory store + manual + CSV

**Files:** Modify `store.py`; extend `csv_import.py`, `manual.py`; `tests/data_ingestion/test_opening_inventory.py` + fixture CSV.

- [ ] **Step 1: Failing test**: opening CSV (`account, symbol, shares, original_avg_cost, build_date`) → preview → commit writes `opening_inventory` (PK account+symbol, idempotent); `original_cost_total = original_avg_cost*shares` if omitted; manual entry path too.
- [ ] **Step 2–4:** Implement store upsert/read + `build_opening_preview` (reuse `preview.commit_preview`) + manual. Run → PASS; gates.
- [ ] **Step 5: Commit** `feat(data_ingestion): opening_inventory store + manual + CSV`.

---

# STAGE 5 — dividends

## Task 10: dividends store + manual + CSV (per-account models)

**Files:** Modify `store.py`; extend `csv_import.py`, `manual.py`; `tests/data_ingestion/test_dividends.py` + fixture CSV.

- [ ] **Step 1: Failing test**: dividend CSV (`account, symbol, date, type(cash/stock/DRIP), gross[, withholding, net, reinvest_shares, reinvest_price]`) → preview → commit writes `dividends`. Per-account model defaults: TW cash → net = gross (no withholding); US DRIP → withholding 30%, net reinvested at reinvest_price → reinvest_shares; MY cash → net received. (Compute missing fields per the account's dividend model; `domain-ledger.md`.)
- [ ] **Step 2–4:** Implement dividend store + a small `dividend_model` helper (per account) + preview/manual. Run → PASS; gates.
- [ ] **Step 5: Commit** `feat(data_ingestion): dividends store + per-account models + manual + CSV`.

---

# STAGE 6 — fx_conversions

## Task 11: fx_conversions store + manual + CSV

**Files:** Modify `store.py`; extend `csv_import.py`, `manual.py`; `tests/data_ingestion/test_fx_conversions.py` + fixture CSV.

- [ ] **Step 1: Failing test**: fx CSV (`account, date, from_ccy, from_amount, to_ccy, to_amount`) → preview → commit writes `fx_conversions`; read-back exposes the implied rate (`from_amount/to_amount`); manual entry path too. (This ledger feeds forex ②'s acquisition-rate pool.)
- [ ] **Step 2–4:** Implement fx_conversions store (write/read) + `build_fx_preview` (reuse `preview.commit_preview`) + manual entry. Run → PASS; mypy + ruff clean.
- [ ] **Step 5: Commit** `feat(data_ingestion): fx_conversions store + manual + CSV`.

---

# STAGE 7 — `shared/llm.py` (AI-agent base)

## Task 12: LiteLLM client + structured output + model registry + usage log

**Files:** Create `portfolio_dash/shared/llm.py`; modify `pyproject.toml` (add `litellm`); modify `schema`/store for `llm_usage` writes; `tests/shared/test_llm.py`.

- [ ] **Step 1: Failing test** (LLM **mocked** — monkeypatch the LiteLLM completion call):
  - `complete_structured(prompt, schema, *, agent) -> validated model` parses JSON-only output → Pydantic; on invalid JSON retries once then raises `LLMUnavailable` (graceful).
  - records an `llm_usage` row (model, input/output tokens, cost = tokens/1e6 × per-Mtok price from the active model config).
  - provider/endpoint/key/model come from `Settings.llm` (config-driven).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement `shared/llm.py`**: thin LiteLLM wrapper (`litellm.completion`, OpenAI-compatible, base_url/key from config), `complete_structured` (prompt + JSON-schema instruction → parse → validate → retry once), `cost_of(model, in_tok, out_tok)`, `log_usage(conn, ...)`. Graceful degradation on provider error → `LLMUnavailable`. Add `litellm` to deps.
- [ ] **Step 4: Run → PASS.** mypy clean (add `litellm.*` to the `ignore_missing_imports` mypy override if needed); ruff clean.
- [ ] **Step 5: Commit** `feat(shared): LiteLLM client + structured output + model registry + usage/cost log`.

---

# STAGE 8 — AI Agents Input

## Task 13: AI Agents Input (NL → draft → confirm) + LLM resolution fallback

**Files:** Create `data_ingestion/agents.py`; modify `resolve.py` (wire LLM fallback); `tests/data_ingestion/test_agents.py`.

- [ ] **Step 1: Failing test** (LLM mocked):
  - `ai_agents_input(conn, text, *, rules, llm) -> ImportPreview` — mocked LLM returns a structured draft (e.g. one BUY 2330 ×10 @600 at account tw_broker) → preview built through the SAME resolve→fees→validate pipeline → NOT written until `commit_preview` with accept.
  - `resolve(...)` `needs_ai` path now calls the (mocked) LLM to propose an instrument; user-confirm still required (the function returns a candidate, not an auto-write).
  - LLM unavailable → preview returns an issue / empty draft, never crashes, never writes.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement `agents.py`**: prompt assembly (XML-tagged, one-shot JSON example, account/market hints) → `shared.llm.complete_structured` → `list[Draft]` → reuse `csv_import`/`preview` to build an `ImportPreview`. Wire `resolve.py`'s `needs_ai` to an injectable LLM resolver. LLM is injected (default from `shared.llm`) so tests mock it.
- [ ] **Step 4: Run → PASS.** mypy + ruff clean. Full suite green.
- [ ] **Step 5: Commit** `feat(data_ingestion): AI Agents Input (NL draft→confirm) + LLM symbol resolution`.

---

## Done criteria

- All four ledgers + instruments writable via manual, CSV (preview→confirm), and AI Agents Input.
- Fee/tax auto-computed per account from config rules, overridable, snapshotted; TW defaults
  0.1425% / 0.3% / 0.1% / 0.15% with min NT$20 + integer rounding.
- Symbol resolution fuzzy→LLM→confirm; nothing written without confirmation; sell>holdings blocks.
- `shared/llm.py` config-driven (endpoint/key/model), logs token usage + cost to `llm_usage`,
  degrades gracefully; LLM **mocked** in tests (no live network).
- `data_ingestion/` imports only `shared/*`; never computes P&L / fetches prices / renders UI.
- `mypy --strict` + `ruff` clean; full suite green.

## Notes for the executor

- Verify `shared/config.py` Settings shape + `shared/money.py` helper names before stages 1–2.
- Reuse `pricing/store.py`'s upsert/read idioms; reuse `preview.commit_preview` across ledgers.
- Test files need full type hints (mypy strict) — annotate the `conn` fixture param as
  `sqlite3.Connection`; narrow `... is not None and ...` before attribute access.
- `litellm` import: add `"litellm.*"` to the pyproject `ignore_missing_imports` mypy override if
  it lacks stubs. Keep all LLM calls behind `shared/llm.py`; never call the provider elsewhere.
- The cost-info **page** is `web_ui/` (deferred) — this plan builds only the registry/pricing
  config + usage log + cost calc that feed it.
