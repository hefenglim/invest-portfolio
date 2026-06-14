# Foundation Hardening — Money/Decimal Wire-String Unification (#2c/M1) Plan

> REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps `- [ ]`. Isolated worktree off `main`.

**Goal:** Make the JSON wire format for EVERY Decimal (money, price, rate, ratio, cost) **one canonical
form** BEFORE the frontend binds: **`format(d, "f")`** — fixed-point, full source precision (preserves
trailing zeros as stored), **never scientific notation**. Today three behaviors coexist: `to_wire` uses
`str(Decimal)` (can emit sci-notation, e.g. `str(Decimal("1E-7"))=="1E-7"`); many add-on fields bypass it
with direct `str(<decimal>)` (e.g. `dashboard.spark_30d`, `llm_quota`); and
`input_center._money_str` uses `normalize()` (strips trailing zeros, can go sci-notation). This unifies all
three. **No money math changes; the DB form is already `format(d,"f")` (`money.to_db`) — only the wire is
aligned to it.** Full precision stays on the wire; the frontend quantizes/formats for display (per
`data-and-pricing.md` — quantize only at display).

**Architecture:** one canonical encoder in `shared/wire.py`; `to_wire` and the (former) bypass sites all
route through it. No new endpoints, no schema changes.

**Tech:** Python 3.12, Decimal, FastAPI, mypy --strict, ruff, pytest + TestClient.

**Gates (repo `.venv`):** pytest · mypy --strict portfolio_dash · ruff check.
Baseline: **980 passed / 4 skipped, mypy clean (136 files), ruff clean.** Green per task.

---

## Task 1: Canonical encoder + `to_wire` aligned to `format(,"f")`
**Files:** `portfolio_dash/shared/wire.py`, `portfolio_dash/shared/money.py`; tests
`tests/shared/test_wire.py` (new) + extend `tests/shared/test_money.py`.
- [ ] Failing tests: a canonical `decimal_str(d: Decimal) -> str` returns `format(d, "f")`:
  `decimal_str(Decimal("1E-7")) == "0.0000001"` (NO sci-notation); `decimal_str(Decimal("100")) == "100"`;
  `decimal_str(Decimal("0.10")) == "0.10"` (trailing zero preserved); `decimal_str(Decimal("1E+2")) ==
  "100"`; `decimal_str(Decimal("-0.00")) == "0.00"` (or document the sign). `to_wire(Decimal(...))` returns
  the SAME as `decimal_str` for those cases (no sci-notation anywhere). `money.to_db` and `decimal_str`
  agree for all finite Decimals (both `format(,"f")`).
- [ ] Implement: add `decimal_str` to `shared/wire.py`; change `to_wire`'s Decimal branch to
  `return decimal_str(value)`. Have `money.to_db` delegate to (or stay byte-identical with) `decimal_str`
  (keep `to_db`'s float/non-finite guards). No behavior change for non-Decimal types.
- [ ] Run full suite — **expect some golden/contract diffs** (sci-notation/trailing-zero cases). Note which
  tests now differ; they are fixed in Task 3 (golden) / Task 2 (bypass sites). Commit
  `refactor(shared): canonical decimal_str (format f); to_wire never emits scientific notation (#2c/M1)`.

## Task 2: Migrate the direct `str(<Decimal>)` wire bypasses → canonical
**Files:** audit every router + service that serializes a Decimal directly. Known sites (grep
`str(` under `portfolio_dash/api` + services and KEEP only the real-Decimal ones):
`api/routers/dashboard.py` (`spark_30d`, `llm_quota`), `api/routers/input_center.py` (remove `_money_str`,
use canonical), `api/routers/symbol.py`, `api/routers/ledgers.py`, `api/routers/llm_settings.py`,
`api/routers/instruments.py`, `api/routers/strategy.py`, `api/routers/prompts.py`, `api/insight_service.py`
/ `api/routers/insights.py` (cost_usd, quota), and any `str(budget_remaining(...))` / `str(quota_*)`.
- [ ] For each touched endpoint: failing test asserting a Decimal field that could differ (e.g. a price
  with a trailing zero, a tiny rate) renders as `format(,"f")` (no sci-notation, stable trailing zeros).
  Then replace `str(<decimal>)` with `to_wire(<decimal>)` (or `decimal_str(<decimal>)` for a scalar).
  **Do NOT touch** `str()` on ints/ids/enums/already-strings (only Decimal-valued expressions).
  Remove `input_center._money_str` entirely; route its call sites through the canonical encoder.
- [ ] Run full suite green for the touched endpoints. Commit
  `refactor(api): route all money/Decimal wire fields through the canonical encoder; drop _money_str (#2c/M1)`.

## Task 3: Regenerate the spec-17 golden + confirm spec-18 round-trip
**Files:** the golden payload fixture/test + the round-trip test (locate via `tests/` — the dashboard
contract golden + the Decimal-string round-trip).
- [ ] Re-derive the golden payload values under the new canonical form (only the values that legitimately
  changed: sci-notation → fixed-point, normalize→preserve). Keep the golden a faithful snapshot of the new
  (correct) wire. Confirm the spec-18 round-trip test (string→Decimal→string is stable) still holds and add
  a case proving no wire Decimal is scientific-notation. Document in the test why values changed.
- [ ] Full suite green. Commit `test: regenerate spec-17 golden + spec-18 round-trip for canonical money strings (#2c/M1)`.

---

## Self-Review
- One canonical encoder (`decimal_str` = `format(,"f")`); `to_wire` + all bypass sites + `to_db` agree;
  `_money_str` removed. No sci-notation on the wire; trailing zeros preserved at source precision; frontend
  still owns display quantization. No money math touched; no schema/endpoint changes. ✓
- Risk: golden value diffs are expected + intentionally regenerated (Task 3); spec-18 round-trip guards
  losslessness. mypy strict + ruff stay clean. ✓
- After this: next foundation-hardening item or frontend wiring (spec 19) per the chain.
