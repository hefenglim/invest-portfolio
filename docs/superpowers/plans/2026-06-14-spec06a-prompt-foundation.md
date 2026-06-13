# Spec 06a — Data-Variable & Prompt-Rendering Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development /
> test-driven-development. Checkbox (`- [ ]`) tracking. You work in an isolated git worktree.
> **Do NOT edit `CHANGELOG.md`** (the controller owns it). Commit with scoped `git add` (never
> `-A`/`.`); end each commit message with `\n\nCo-Authored-By: Claude <noreply@anthropic.com>`.
> Run gates with `.venv/Scripts/python` (NOT bare python): `pytest -q`, `mypy --strict
> portfolio_dash`, `ruff check portfolio_dash tests`. Money/price/rate is `Decimal` → JSON
> **strings**, never float. The LLM never emits numbers of record: every numeric variable value is
> computed by the calc core (`portfolio/`) and only assembled into JSON here.

**Goal:** Build the prompt "Lego-block" foundation the AI brain (specs 04/07) hangs on: a
variable **registry** (26 vars / 8 categories), a reusable **render + token-validation core**,
a **global system prompt**, and the **preview** (no-LLM) + **test** (real-LLM) endpoints.

**Architecture:** Create `portfolio_dash/llm_insight/` (new module per CLAUDE.md map) with
`variables.py` = registry + `render_prompt` + `validate_tokens` (the SINGLE source reused later by
spec 04 R1 runtime gating and spec 07 preflight) + per-token value assembly over already-computed
`DashboardData` (+ per-symbol detail). New numeric calcs (MA / volatility / price-vs-cost) are pure
functions in `portfolio/technicals.py` (calc core — NOT llm_insight, which produces no numbers).
A thin `api/routers/prompts.py` wires the endpoints; `/prompts/test` adds a free-text completion
to `shared/llm.py`. External-data variables (chips/sentiment/index) and AI-self variables
(backtest/calibration) are **registered but `available=false`** — chips/sentiment flip on in spec
06b, backtest/calibration in spec 04. They render as `{"unavailable": true}`.

---

## Reconciliations (read before coding)
1. **26 variables, 8 categories** — `web/vars.js` `PD_VARS.CATEGORIES` is the authoritative
   contract (the spec header's "24" is a miscount). Mirror token ids, categories, scope, desc,
   sample EXACTLY. Categories & counts: position 6, price 4, dividend 3, fx 2, chips 5, sentiment 2,
   ai 2, system 2.
2. **`scope` wire = `"portfolio"` | `"per_symbol"`** (English enum), mapping vars.js
   `全組合`→portfolio, `單一標的`→per_symbol.
3. **`available` per var (v1):** position(6)+price(4)+dividend(3)+fx(2)+system(2) = **17 available
   now**; chips(5)+sentiment(2)+ai(2) = **9 `available=false`** (chips/sentiment → spec 06b;
   ai → spec 04). `vars.js` marks the `ai` category `source:'ready'` — that is WRONG (it needs
   spec 04 evaluations); register them `available=false`.
4. **preview uses REAL values, not mock samples** (spec 6.2 "完整代入後的實際送出樣貌"): build the
   real `DashboardData` and assemble actual computed values. (`sample` in the registry is only the
   short preview shown in the var-list UI.)
5. **Token validation is one reusable function** (`validate_tokens`): preview path is diagnostic →
   always 200, lists `unknown_tokens`/`scope_violations`; the execution path (`/prompts/test`,
   later insight runs & post-preflight runs) → **422** on any unknown token, and a `per_symbol`
   variable used in a `portfolio`-scope body → **422** (this is spec 04 R1).
6. **Global system prompt** (spec 6.2 returns `system_prompt`; spec 07 has a `system` assembly
   layer; spec 04 `use_system_prompt` toggles it). Store ONE editable global value via
   `config_store` (category `"system_prompt"`), default = the `web/settings-prompts.js`
   `PROMPTS_DATA.system_prompt` text. Add `GET/PUT /api/system-prompt`. (Reconciliation: neither
   spec explicitly assigns this CRUD endpoint; it is foundational to rendering, so it lands here.)
7. **`/prompts/test` free-text** — `shared/llm.complete_structured` parses JSON into a schema, but
   test wants a free-form reply. Add `complete_text(prompt, *, agent, conn, system=None)` to
   `shared/llm.py` (mirror `_complete_with` minus JSON parse) returning a small result
   (reply, model, tokens_in, tokens_out, cost). Records `llm_usage` (agent passed in); budget gate
   → `LLMBudgetExceeded` → 402.
8. **est_tokens** is a heuristic (no tokenizer dep): `ceil(len(system_prompt + "\n" + rendered) / 4)`.
   Document it as an estimate.

---

### Task 1: `portfolio/technicals.py` — MA, volatility, price-vs-cost (pure, Decimal)

**Files:** Create `portfolio_dash/portfolio/technicals.py`; Test `tests/portfolio/test_technicals.py`.

All inputs/outputs `Decimal`; ratios are `Decimal` (use `Decimal.sqrt()` — never float). A series is
`list[Decimal]` of closes in chronological order (oldest→newest).

```python
def moving_average(closes: list[Decimal], window: int) -> Decimal | None:
    """Simple MA of the last `window` closes; None if fewer than `window` points."""

def ma_signals(closes: list[Decimal]) -> dict[str, Decimal | None]:
    """{'ma20','ma60','ma120','price_vs_ma20','price_vs_ma60','price_vs_ma120'} using the
    last close as the current price. price_vs_maN = (price - maN)/maN; None when maN is None."""

def annualized_volatility(closes: list[Decimal], window: int = 30, periods: int = 252) -> Decimal | None:
    """Stdev (sample) of the last `window` simple daily returns ((c[i]-c[i-1])/c[i-1]),
    annualized × sqrt(periods). None if fewer than window+1 points. All Decimal."""

def max_drawdown(closes: list[Decimal], window: int = 90) -> Decimal | None:
    """Most-negative peak-to-trough return over the last `window` closes (<= 0), as Decimal.
    None if fewer than 2 points."""

def price_vs_cost(price: Decimal, original_avg: Decimal, adjusted_avg: Decimal) -> dict[str, Decimal]:
    """{'price_vs_original','price_vs_adjusted'} = (price - cost)/cost (cost assumed > 0)."""
```

- [ ] **Step 1 — failing tests** with hand-checkable fixtures, e.g.:
```python
from decimal import Decimal
from portfolio_dash.portfolio import technicals as T

def test_moving_average_and_none():
    assert T.moving_average([Decimal("10"), Decimal("20"), Decimal("30")], 3) == Decimal("20")
    assert T.moving_average([Decimal("10")], 3) is None

def test_ma_signals_price_vs():
    closes = [Decimal(str(x)) for x in range(1, 21)]  # 1..20, last=20, ma20=10.5
    s = T.ma_signals(closes)
    assert s["ma20"] == Decimal("10.5")
    assert s["price_vs_ma20"] == (Decimal("20") - Decimal("10.5")) / Decimal("10.5")
    assert s["ma60"] is None and s["price_vs_ma60"] is None

def test_volatility_constant_series_zero():
    assert T.annualized_volatility([Decimal("100")] * 40) == Decimal("0")

def test_max_drawdown_simple():
    # 100 -> 120 -> 90 : trough 90 vs peak 120 = -0.25
    assert T.max_drawdown([Decimal("100"), Decimal("120"), Decimal("90")]) == Decimal("-0.25")

def test_price_vs_cost():
    r = T.price_vs_cost(Decimal("612.5"), Decimal("500"), Decimal("495"))
    assert r["price_vs_original"] == (Decimal("612.5") - Decimal("500")) / Decimal("500")
```
- [ ] **Step 2 — run, FAIL. Step 3 — implement (pure Decimal; sample stdev uses n-1; guard n<2
  / window underflow → None). Step 4 — green. Step 5 — gates. Step 6 — commit**
  `feat(portfolio): technicals — MA/volatility/max-drawdown/price-vs-cost (pure Decimal) (spec 06a)`

---

### Task 2: `llm_insight/variables.py` — registry + render + validate + value assembly

**Files:** Create `portfolio_dash/llm_insight/__init__.py`, `portfolio_dash/llm_insight/variables.py`;
Test `tests/llm_insight/test_variables.py`.

**Registry** — a frozen list of `VarSpec(token, name, category, scope, available, desc, sample)`
for all 26 tokens (copy ids/desc/sample from `web/vars.js`; scope English; `available` per
reconciliation #3). Expose `REGISTRY: tuple[VarSpec, ...]`, `BY_TOKEN: dict[str, VarSpec]`.

**Token regex** identical to vars.js: `re.compile(r"\{\{([a-z0-9_]+)\}\}", re.IGNORECASE)`.

```python
def tokens_in(body: str) -> list[str]:  # de-duped, in first-seen order

@dataclass(frozen=True)
class TokenValidation:
    tokens_used: list[str]
    unknown_tokens: list[str]      # not in registry
    scope_violations: list[str]    # per_symbol var used in a portfolio-scope body

def validate_tokens(body: str, scope: str) -> TokenValidation:
    """THE single validation core (reused by spec 04 R1 + spec 07 preflight). A token absent
    from the registry -> unknown; a registry var with scope=='per_symbol' used when the body
    scope=='portfolio' -> scope_violation. (per_symbol bodies may use portfolio vars freely.)"""
```

**Value assembly** — given a context, produce the real JSON string per token:
```python
@dataclass
class VarContext:
    data: DashboardData                     # from build_dashboard (portfolio-scope vars)
    symbol: str | None = None               # per_symbol target
    closes: list[Decimal] | None = None     # chronological closes from pricing.store.get_price_history
    price_points: list[dict] | None = None  # [{date, close}] for price_history_json (same source)

def value_for(token: str, ctx: VarContext) -> object:
    """Return the JSON-able value for a token. `available=false` vars and any var whose data is
    missing return {"unavailable": true} (optionally with last_as_of). Numbers come straight from
    the computed DashboardData / SymbolDetail / technicals — NEVER recomputed here beyond trivial
    shaping. Decimal stays Decimal (serialized to string by the dumper)."""

def render_prompt(body: str, ctx: VarContext) -> tuple[str, list[str]]:
    """Replace each {{token}} with json.dumps(value_for(token, ctx)) using a Decimal-aware encoder
    (reuse api.wire.to_wire / a Decimal->str default). Unknown tokens are left as a visible
    '⚠unknown:{{token}}' marker (preview shows them; the execution path rejects via validate_tokens
    BEFORE calling render). Returns (rendered_text, tokens_used)."""
```

**Value mapping (token → source field on DashboardData / SymbolDetail; read the model files for exact
names):** `holdings_json`←data.holdings; `allocation_json`←data.allocation; `kpis_json`←data.kpis;
`returns_by_ccy_json`←data.returns.by_currency; `realized_json`←data.realized.rows;
`symbol_detail_json`←assemble from `data` filtered by `ctx.symbol` (the symbol's `Holding` +
its `dividends`/`realized` rows) — do NOT call the spec-01 router (its detail logic is inline);
`price_history_json`←`ctx.price_points` (+ a `stale` flag from `data.freshness`);
`ma_signals_json`←technicals.ma_signals(ctx.closes); `volatility_json`←{vol_30d_annualized,
max_drawdown_90d} from technicals over ctx.closes; `price_vs_cost_json`←technicals.price_vs_cost
(the symbol's Holding market_price/original_avg/adjusted_avg);
`dividends_json`←data.dividends; `ex_dividend_calendar_json`←data.ex_dividend_calendar;
`dividend_projection_json`←data.dividend_projection.by_currency; `fx_json`←data.fx.by_account;
`fx_rates_json`←data.fx rates + as_of; `freshness_json`←data.freshness (missing/stale);
`as_of`←data.as_of (ISO string). The 9 `available=false` tokens → `{"unavailable": true}`.
NOTE: read `portfolio_dash/portfolio/dashboard_models.py` + `portfolio_dash/api/routers/symbol.py`
for the exact field/shape; mirror the shapes shown in the vars.js `sample` strings.

- [ ] **Step 1 — failing tests** `tests/llm_insight/test_variables.py` (use `golden_db` +
  `build_dashboard`):
```python
def test_registry_has_26_and_categories():
    from portfolio_dash.llm_insight import variables as V
    assert len(V.REGISTRY) == 26
    assert len({v.category for v in V.REGISTRY}) == 8

def test_validate_unknown_and_scope(): 
    from portfolio_dash.llm_insight import variables as V
    r = V.validate_tokens("{{holdings_json}} {{nope_json}} {{symbol_detail_json}}", "portfolio")
    assert "nope_json" in r.unknown_tokens
    assert "symbol_detail_json" in r.scope_violations  # per_symbol var in portfolio body
    r2 = V.validate_tokens("{{symbol_detail_json}}", "per_symbol")
    assert r2.scope_violations == []

def test_render_real_values(golden_db):
    from portfolio_dash.portfolio.dashboard import build_dashboard
    from portfolio_dash.llm_insight import variables as V
    from portfolio_dash.shared.enums import Currency
    from datetime import datetime; from zoneinfo import ZoneInfo
    data = build_dashboard(golden_db, now=datetime(2026,6,11,14,30,tzinfo=ZoneInfo("Asia/Taipei")),
                           reporting=Currency.TWD)
    out, used = V.render_prompt("持倉：{{holdings_json}}", V.VarContext(data=data))
    assert "2330" in out and "holdings_json" in used and "{{holdings_json}}" not in out

def test_unavailable_var_renders_marker(golden_db):
    from portfolio_dash.llm_insight import variables as V
    # institutional_json is available=false in 06a
    assert any(v.token == "institutional_json" and not v.available for v in V.REGISTRY)
```
- [ ] **Step 2 — FAIL. Step 3 — implement. Step 4 — green. Step 5 — gates. Step 6 — commit**
  `feat(llm_insight): variable registry + render_prompt + validate_tokens core (spec 06a)`

---

### Task 3: system-prompt config + `api/routers/prompts.py` (prompt-vars, preview, system-prompt)

**Files:** Create `portfolio_dash/llm_insight/system_prompt.py` (config_store single-row);
Create `portfolio_dash/api/routers/prompts.py`; Modify `portfolio_dash/api/app.py` (include router +
`ensure_system_prompt_seeded` in lifespan); Modify `tests/conftest.py` golden_db (seed system prompt);
Test `tests/contract/test_prompts_api.py`.

**system_prompt.py:** `DEFAULT_SYSTEM_PROMPT` (copy `PROMPTS_DATA.system_prompt`), single-row table
`system_prompt_config(id INTEGER PK CHECK(id=1), body TEXT, updated_at TEXT)` via
`config_store.ensure_seeded` (seed inserts the default); `get_system_prompt(conn) -> {body, updated_at}`
and `set_system_prompt(conn, body, *, now) -> {body, updated_at}`.

**Endpoints (router prefix none; under `/api`):**
- `GET /prompt-vars` → list of `{token, name, category, scope, desc, available, sample}` from REGISTRY.
- `GET /system-prompt` → `{body, updated_at}`. `PUT /system-prompt` body `{body}` → set + return it.
- `POST /prompts/preview` body `{body, scope, symbol?}` → **always 200**:
  `{system_prompt, rendered, tokens_used, unknown_tokens, scope_violations, est_tokens}`.
  Build `DashboardData` via `build_dashboard(conn, now, reporting)`; if scope=="per_symbol" and
  symbol given, fetch closes/price_points via `pricing.store.get_price_history(conn, symbol,
  as_of.date()-180d, as_of.date())` into the `VarContext`; call `validate_tokens` + `render_prompt`;
  prepend the global system prompt; `est_tokens = ceil(len(system+"\n"+rendered)/4)`. NEVER calls the LLM.
- [ ] **Step 1 — failing contract tests** `tests/contract/test_prompts_api.py` (api_client/golden_db):
```python
def test_prompt_vars_shape(api_client):
    rows = api_client.get("/api/prompt-vars").json()
    assert len(rows) == 26
    h = next(r for r in rows if r["token"] == "holdings_json")
    assert h["scope"] == "portfolio" and h["available"] is True
    assert next(r for r in rows if r["token"]=="institutional_json")["available"] is False

def test_system_prompt_get_put(api_client):
    assert api_client.get("/api/system-prompt").json()["body"]
    r = api_client.put("/api/system-prompt", json={"body": "新守則"})
    assert r.status_code == 200 and r.json()["body"] == "新守則"
    assert api_client.get("/api/system-prompt").json()["body"] == "新守則"

def test_preview_always_200_with_diagnostics(api_client):
    r = api_client.post("/api/prompts/preview", json={
        "body": "{{holdings_json}} {{bogus_json}} {{symbol_detail_json}}",
        "scope": "portfolio", "symbol": None})
    assert r.status_code == 200
    b = r.json()
    assert "bogus_json" in b["unknown_tokens"]
    assert "symbol_detail_json" in b["scope_violations"]
    assert "holdings_json" in b["tokens_used"] and b["system_prompt"] and b["est_tokens"] > 0
    assert "2330" in b["rendered"]

def test_preview_per_symbol(api_client):
    r = api_client.post("/api/prompts/preview", json={
        "body": "{{symbol_detail_json}}", "scope": "per_symbol", "symbol": "2330"})
    assert r.status_code == 200 and r.json()["scope_violations"] == []
```
- [ ] **Step 2 — FAIL. Step 3 — implement (router + system_prompt.py + app.py wiring + conftest
  golden_db `ensure_system_prompt_seeded`). Step 4 — green + full suite (no regressions — new
  endpoints are additive; auth gate stays guest in golden_db). Step 5 — gates. Step 6 — commit**
  `feat(api): /api/prompt-vars + /api/prompts/preview + /api/system-prompt (spec 06a)`

---

### Task 4: `POST /api/prompts/test` (real LLM, records usage, budget→402)

**Files:** Modify `portfolio_dash/shared/llm.py` (add `complete_text`); Modify
`portfolio_dash/api/routers/prompts.py` (add the route); Test add to `tests/contract/test_prompts_api.py`
+ `tests/shared/test_llm_complete_text.py`.

**`complete_text`** in `shared/llm.py`:
```python
class TextCompletion(BaseModel):
    reply: str; model: str; tokens_in: int; tokens_out: int; cost: Decimal

def complete_text(prompt: str, *, agent: str, conn: sqlite3.Connection,
                  system: str | None = None) -> TextCompletion:
    """Free-text completion (no JSON schema): budget gate -> role select (default) -> first
    candidate -> log_usage(agent=...) -> return reply+usage. Raises LLMBudgetExceeded / AINotActivated
    / LLMUnavailable (callers map to 402/409/503 via the global handlers)."""
```
Mirror `_complete_with` (build messages with optional system role, `litellm.completion`, log_usage,
return content) but skip JSON parsing; use `select_models(conn, vision=False)`; on all-candidates-fail
raise `LLMUnavailable`.

**Route** `POST /prompts/test` body `{body, scope, symbol?}`:
1. Build context + `validate_tokens`; **422** if `unknown_tokens` or `scope_violations` (execution
   path, reconciliation #5) — body `{error:{code:"validation_error", message, issues:[...]}}`.
2. `render_prompt`; prepend system prompt.
3. `r = complete_text(rendered, agent="prompt_test", conn=conn, system=system_prompt)` — the global
   `LLMBudgetExceeded`/`AINotActivated`/`LLMUnavailable` handlers already map to 402/409/503.
4. 200 `{reply, model, via:"litellm", tokens_in, tokens_out, cost_usd:str, quota_remaining:str}`
   (quota_remaining from `llm_config.budget_remaining(conn)` as string).

- [ ] **Step 1 — failing tests.** `tests/shared/test_llm_complete_text.py`: monkeypatch
  `llm_mod.litellm.completion` to a fake returning content+usage; assert `complete_text` logs an
  `llm_usage` row (agent="prompt_test") and returns the reply. Contract test in
  `test_prompts_api.py`: monkeypatch `complete_text` (or the litellm seam) so no network; seed an
  LLM model+budget so the call is allowed; assert 200 + `quota_remaining` is a string; AND a
  422 case (`{{bogus_json}}`); AND budget-exhausted → 402 (set budget 0). Use the existing
  llm-config seeding helpers from `tests/contract/test_llm_settings_api.py` for model/topup setup.
- [ ] **Step 2 — FAIL. Step 3 — implement. Step 4 — green. Step 5 — gates. Step 6 — commit**
  `feat(api): POST /api/prompts/test (real LiteLLM, records usage, budget->402) (spec 06a)`

## Self-review checklist
26-var registry matches vars.js; numbers only from calc core (technicals in portfolio/, values
assembled not recomputed); `validate_tokens` is the single reusable core (preview 200 / test 422);
Decimal→string everywhere; preview never calls LLM (zero cost); test records llm_usage + honours
budget (402); system prompt stored once with a default; auth gate stays guest in golden_db (no
regressions); `llm_insight` imports only portfolio/shared/api.deps (one-way deps intact).
