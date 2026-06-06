# Design: `shared/` Foundation

- **Date:** 2026-06-06
- **Status:** Approved (design); pending spec review
- **Module:** `portfolio_dash/shared/`
- **Author:** Claude Code (from human spec, brainstorming flow)

## Context & purpose

`shared/` is the foundation layer of the `portfolio_dash` monolith. Per
`architecture.md` it depends on nothing internal and may be imported everywhere; lower
layers never import the web layer. This is the first application code in the repo
(`v0.0.0` was scaffold only), so this increment also bootstraps the Python package and
the tooling (mypy/ruff/pytest).

Responsibilities delivered here: env-driven settings, a SQLite connection/session
helper, the `Decimal`/money primitives, the single FX-conversion helper, and the two
stable cross-cutting enums (`Currency`, `Market`).

## Decisions (settled in brainstorming)

1. **Decimal persistence = TEXT canonical string.** Every `Decimal` is stored as a
   fixed-point string via `format(value, "f")` and restored with `Decimal(text)`.
   Exact at any scale; no per-column scale metadata to maintain; human-readable for
   debugging. SQL-side arithmetic/ordering is not needed because all calculation runs
   in Python (`portfolio/`), not in SQL.
2. **Scope = foundation + cross-cutting enums.** Build settings, DB session, money/FX
   helpers, and the `Currency`/`Market` enums. Full canonical-table Pydantic models are
   deferred to the module that first needs them (avoids rework before downstream specs
   are detailed).
3. **FX helper is a pure conversion function**; the caller supplies the rate. Rate
   lookup (which date / which source) is a domain concern (`domain-ledger.md`), not a
   `shared/` concern, and is not coupled to the `fx_rates` schema here.
4. **DB access = stdlib `sqlite3`** (no ORM). Consistent with the "default no to new
   dependencies" rule in `stack.md`.
5. **Amount rounding mode = `ROUND_HALF_UP`** (四捨五入), per `markets-and-fees.md`
   (TW fee/tax rounding) and applied uniformly to per-currency amount quantization.

## Module layout

```
portfolio_dash/
  __init__.py
  shared/
    __init__.py
    config.py     # Settings (pydantic-settings) + get_settings() singleton
    enums.py      # Currency(TWD/USD/MYR), Market(US/TW/MY)
    money.py      # Decimal <-> TEXT, per-currency minor-unit quantize, float guard
    fx.py         # convert() pure conversion
    db.py         # sqlite3 connection + session context manager
```

## Component specs

### `enums.py`

```python
class Currency(str, Enum):
    TWD = "TWD"
    USD = "USD"
    MYR = "MYR"

class Market(str, Enum):
    US = "US"
    TW = "TW"
    MY = "MY"
```

`str`-Enum so values serialize to plain strings in DB rows and JSON. `Side`,
`AccountId`, and other domain enums are deferred to their owning models.

### `config.py`

`Settings(BaseSettings)` (pydantic-settings v2), reads `.env`, fields needed by the
foundation only:

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `db_path` | `Path` | `data/portfolio.db` | SQLite file location |
| `app_env` | `Literal["dev","prod"]` | `"dev"` | environment switch |
| `tz_display` | `str` | `"Asia/Taipei"` | display tz; storage is always UTC |
| `reporting_currency` | `Currency` | `Currency.TWD` | single reporting ccy for combined XIRR |

`get_settings()` returns a cached singleton (`functools.lru_cache`). Invalid env raises
pydantic `ValidationError` at startup (fail loud). LLM/LiteLLM settings are **not**
added now — they arrive with `llm_insight/`.

### `money.py`

```python
MINOR_UNITS: dict[Currency, int] = {Currency.TWD: 0, Currency.USD: 2, Currency.MYR: 2}

def to_db(value: Decimal) -> str: ...
def from_db(text: str) -> Decimal: ...
def quantize_amount(value: Decimal, currency: Currency,
                    rounding: str = ROUND_HALF_UP) -> Decimal: ...
```

- `to_db`: rejects `float` input with `TypeError` (enforces "no money in floats");
  returns `format(value, "f")` — fixed-point, no scientific notation, **preserves
  significant trailing zeros** (e.g. `Decimal("38.50") -> "38.50"`,
  `Decimal("0.005") -> "0.005"`).
- `from_db`: `Decimal(text)`; an invalid string raises loudly (no silent coercion).
- `quantize_amount`: quantizes to the currency's minor unit (TWD -> 0 dp integer,
  USD/MYR -> 2 dp) using `ROUND_HALF_UP`. **Called only at settlement/display** — prices
  and FX rates are stored at full source precision and are never quantized here. An
  unknown currency raises a clear error.

### `fx.py`

```python
def convert(amount: Decimal, rate: Decimal, *,
            to_currency: Currency | None = None) -> Decimal: ...
```

- Pure function returning `amount * rate`. `rate` direction: **1 unit of source ccy =
  `rate` units of target ccy** (documented in the docstring).
- `rate <= 0` raises `ValueError`; `amount` may be negative (cashflow signs).
- When `to_currency` is given, the result is quantized to that currency's minor unit;
  otherwise full precision is returned (XIRR flows stay full precision; quantize at
  settlement). This is the single FX helper — no ad-hoc multiply-by-rate elsewhere.

### `db.py`

```python
def get_connection() -> sqlite3.Connection: ...

@contextmanager
def session() -> Iterator[sqlite3.Connection]: ...
```

- `get_connection`: opens the SQLite file from `Settings.db_path`,
  `row_factory = sqlite3.Row`, PRAGMA `foreign_keys = ON`, `journal_mode = WAL`. It
  resolves the path through `get_settings()`, so tests point it at a temp-file db by
  overriding `db_path` (env var + `get_settings.cache_clear()`, or a fixture).
- `session`: yields a connection; `commit` on success, `rollback` then re-raise on
  exception, `close` in `finally`.
- No global Decimal adapter is registered. `Decimal <-> TEXT` conversion happens
  explicitly at repository boundaries (future modules) via `money.to_db`/`from_db`,
  keeping the conversion visible and free of hidden global state.

## Package & tooling bootstrap

- `pyproject.toml`: project metadata, `[tool.mypy]` (strict), `[tool.ruff]`,
  `[tool.pytest.ini_options]`.
- Dependencies installed now (foundation only): runtime `pydantic`,
  `pydantic-settings`; dev `mypy`, `ruff`, `pytest`, `pytest-asyncio`. The rest
  (pandas/numpy/numpy-financial/litellm/apscheduler/httpx/pricing clients) are added by
  the module that first needs them, per `stack.md`.
- Test layout under `tests/shared/`.

## Error handling

All failures are loud; nothing is silently coerced:
- Bad env -> pydantic `ValidationError` at startup.
- Invalid Decimal string in `from_db` -> raises.
- Unknown currency in `quantize_amount` -> clear error.
- `float` reaching money primitives -> `TypeError`.
- Non-positive FX rate -> `ValueError`.
- Exception inside `session` -> `rollback`, then re-raise.

## Testing plan (TDD — tests written first)

- `test_money`: Decimal<->TEXT round-trip preserves precision incl. trailing zeros
  (`38.50`), MY 3 dp (`0.005`), FX 6 dp; per-currency `quantize_amount` with
  `ROUND_HALF_UP` (TWD `1234.5 -> 1235`, USD `1.005 -> 1.01`); `float` input rejected.
- `test_fx`: exact conversion, rate direction, opt-in quantization via `to_currency`,
  non-positive rate rejected, negative amount allowed.
- `test_config`: env loading, defaults, `ValidationError` on bad value, `get_settings`
  returns a cached singleton.
- `test_enums`: members and string values.
- `test_db`: `session` commits on success and rolls back on exception, using a
  **temp-file** db (a `:memory:` db cannot be used because `session` closes the
  connection each time) with a scratch table; `row_factory` yields `sqlite3.Row`;
  PRAGMA `foreign_keys` is effective.

## Out of scope / deferred

- Canonical-table Pydantic models (accounts, instruments, transactions, dividends,
  fx_conversions, opening_inventory, prices, fx_rates, insights).
- LLM/LiteLLM settings.
- DB schema DDL / migrations (created when the first persisted model lands).
- Display formatting beyond minor-unit quantization (thousands separators are a
  `web_ui/` presentation concern).
- FX rate lookup / `fx_rates` access (belongs near `pricing/`).

## Follow-ups flagged in final review (for downstream layers)

Non-blocking; revisit when the owning module lands:
- **`money.from_db` is not symmetric with `to_db`.** `to_db` rejects non-finite
  Decimals, but `from_db` is a bare `Decimal(text)`. The write-side guard prevents
  non-finite values from ever being stored, so this is defense-in-depth only. When the
  repository/persistence layer is built, decide whether to re-run `is_finite()` on read
  to harden against hand-edited / corrupted rows.
- **`fx.convert` rate-direction contract sits on the caller.** `convert` quantizes to
  `to_currency` but cannot verify the supplied rate actually targets that currency (pure
  function, by design). `forex/` and `pricing/` own the correctness of which
  rate/direction is passed.
- **`quantize_amount(rounding=...)` is a public override.** The project mandates
  ROUND_HALF_UP uniformly; the override exists only to match the stdlib signature.
  Future callers should not pass a different mode without recording the decision.
