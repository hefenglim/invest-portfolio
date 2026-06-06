# `shared/` Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `portfolio_dash/shared/` — the dependency-free foundation layer: settings, SQLite session helper, `Decimal`/money primitives, the single FX-conversion helper, and the `Currency`/`Market` enums — plus the package and tooling bootstrap.

**Architecture:** A flat Python package `portfolio_dash/` with a `shared/` subpackage of small, single-responsibility modules. Money is `Decimal` end-to-end, persisted as canonical TEXT. The FX helper is a pure function (caller supplies the rate). SQLite is accessed via stdlib `sqlite3` (no ORM). All calculation lives in Python, never SQL.

**Tech Stack:** Python 3.12, pydantic v2 + pydantic-settings, stdlib `sqlite3` + `decimal`, pytest, mypy (strict), ruff.

**Spec:** `docs/superpowers/specs/2026-06-06-shared-foundation-design.md`

---

## File Structure

| File | Responsibility |
| --- | --- |
| `pyproject.toml` | project metadata, deps, mypy/ruff/pytest config |
| `portfolio_dash/__init__.py` | package marker |
| `portfolio_dash/py.typed` | PEP 561 typed marker |
| `portfolio_dash/shared/__init__.py` | subpackage marker |
| `portfolio_dash/shared/enums.py` | `Currency`, `Market` str-enums |
| `portfolio_dash/shared/money.py` | `to_db`/`from_db`, `quantize_amount`, `MINOR_UNITS`, float guard |
| `portfolio_dash/shared/fx.py` | `convert()` pure FX conversion |
| `portfolio_dash/shared/config.py` | `Settings`, `get_settings()` singleton |
| `portfolio_dash/shared/db.py` | `get_connection()`, `session()` context manager |
| `tests/__init__.py` | test package marker (keeps mypy module names consistent) |
| `tests/shared/__init__.py` | test subpackage marker |
| `tests/test_smoke.py` | package-importable smoke test |
| `tests/shared/test_enums.py` | enum tests |
| `tests/shared/test_money.py` | money primitive tests |
| `tests/shared/test_fx.py` | FX conversion tests |
| `tests/shared/test_config.py` | settings tests |
| `tests/shared/test_db.py` | DB session tests |

Build order is dependency-driven: bootstrap → enums → money → fx → config → db, then final verification + CHANGELOG.

---

## Task 1: Project & tooling bootstrap

**Files:**
- Create: `pyproject.toml`
- Create: `portfolio_dash/__init__.py`
- Create: `portfolio_dash/py.typed`
- Create: `portfolio_dash/shared/__init__.py`
- Create: `tests/__init__.py`
- Create: `tests/shared/__init__.py`
- Test: `tests/test_smoke.py`

- [ ] **Step 1: Create a feature branch** (we are on `main`, the default branch)

Run:
```bash
git checkout -b feat/shared-foundation
```

- [ ] **Step 2: Write the failing smoke test**

Create `tests/test_smoke.py`:
```python
import portfolio_dash


def test_package_importable() -> None:
    assert portfolio_dash.__name__ == "portfolio_dash"
```

- [ ] **Step 3: Run it to verify it fails**

Run: `python -m pytest tests/test_smoke.py -v`
Expected: collection/import error — `ModuleNotFoundError: No module named 'portfolio_dash'`.

- [ ] **Step 4: Create `pyproject.toml`**

```toml
[project]
name = "portfolio-dash"
version = "0.0.0"
description = "Personal multi-currency, multi-account stock portfolio dashboard"
requires-python = ">=3.12"
dependencies = [
    "pydantic>=2.7",
    "pydantic-settings>=2.3",
]

[project.optional-dependencies]
dev = [
    "mypy>=1.10",
    "ruff>=0.5",
    "pytest>=8.2",
    "pytest-asyncio>=0.23",
]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["portfolio_dash*"]

[tool.mypy]
python_version = "3.12"
strict = true
warn_unused_configs = true
files = ["portfolio_dash", "tests"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
```

- [ ] **Step 5: Create the package files**

Create `portfolio_dash/__init__.py`:
```python
"""portfolio_dash — personal multi-currency stock portfolio dashboard."""
```

Create `portfolio_dash/py.typed` (empty file).

Create `portfolio_dash/shared/__init__.py`:
```python
"""shared — foundation layer: config, db, money/FX helpers, enums."""
```

Create `tests/__init__.py` (empty file) and `tests/shared/__init__.py` (empty file).
These keep mypy's module names consistent across the `tests/` tree under strict mode.

- [ ] **Step 6: Create the virtualenv and install the package (editable, with dev extras)**

Run (PowerShell):
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install -e ".[dev]"
```

- [ ] **Step 7: Run the smoke test to verify it passes**

Run: `python -m pytest tests/test_smoke.py -v`
Expected: PASS (1 passed).

- [ ] **Step 8: Verify mypy and ruff run clean on the skeleton**

Run: `python -m mypy`
Expected: `Success: no issues found`.
Run: `python -m ruff check .`
Expected: `All checks passed!`

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml portfolio_dash tests
git commit -m "chore: bootstrap portfolio_dash package and tooling" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: `enums.py` — Currency & Market

**Files:**
- Create: `portfolio_dash/shared/enums.py`
- Test: `tests/shared/test_enums.py`

- [ ] **Step 1: Write the failing test**

Create `tests/shared/test_enums.py`:
```python
from portfolio_dash.shared.enums import Currency, Market


def test_currency_members_and_values() -> None:
    assert {c.value for c in Currency} == {"TWD", "USD", "MYR"}
    assert Currency.TWD.value == "TWD"


def test_currency_is_str_enum() -> None:
    assert Currency.USD == "USD"
    assert isinstance(Currency.USD, str)


def test_market_members_and_values() -> None:
    assert {m.value for m in Market} == {"US", "TW", "MY"}
    assert Market.US.value == "US"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/shared/test_enums.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'portfolio_dash.shared.enums'`.

- [ ] **Step 3: Write the implementation**

Create `portfolio_dash/shared/enums.py`:
```python
"""Stable cross-cutting enums shared across all layers."""

from enum import Enum


class Currency(str, Enum):
    """Quote / settlement currencies handled by the system."""

    TWD = "TWD"
    USD = "USD"
    MYR = "MYR"


class Market(str, Enum):
    """Exchanges/markets where instruments trade."""

    US = "US"
    TW = "TW"
    MY = "MY"
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/shared/test_enums.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add portfolio_dash/shared/enums.py tests/shared/test_enums.py
git commit -m "feat(shared): add Currency and Market enums" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: `money.py` — Decimal persistence & quantization

**Files:**
- Create: `portfolio_dash/shared/money.py`
- Test: `tests/shared/test_money.py`

- [ ] **Step 1: Write the failing test**

Create `tests/shared/test_money.py`:
```python
from decimal import Decimal, InvalidOperation

import pytest

from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.money import MINOR_UNITS, from_db, quantize_amount, to_db


def test_to_db_preserves_trailing_zeros() -> None:
    assert to_db(Decimal("38.50")) == "38.50"


def test_to_db_three_dp_my_price() -> None:
    assert to_db(Decimal("0.005")) == "0.005"


def test_to_db_no_scientific_notation() -> None:
    assert to_db(Decimal("1E+2")) == "100"


def test_to_db_rejects_float() -> None:
    with pytest.raises(TypeError):
        to_db(38.50)  # type: ignore[arg-type]


def test_roundtrip_high_precision_fx() -> None:
    rate = Decimal("4.512345")
    assert from_db(to_db(rate)) == rate


def test_from_db_invalid_raises() -> None:
    with pytest.raises(InvalidOperation):
        from_db("not-a-number")


def test_quantize_twd_zero_dp_half_up() -> None:
    assert quantize_amount(Decimal("1234.5"), Currency.TWD) == Decimal("1235")


def test_quantize_usd_two_dp_half_up() -> None:
    assert quantize_amount(Decimal("1.005"), Currency.USD) == Decimal("1.01")


def test_quantize_myr_two_dp() -> None:
    assert quantize_amount(Decimal("2.345"), Currency.MYR) == Decimal("2.35")


def test_minor_units_mapping() -> None:
    assert MINOR_UNITS == {Currency.TWD: 0, Currency.USD: 2, Currency.MYR: 2}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/shared/test_money.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'portfolio_dash.shared.money'`.

- [ ] **Step 3: Write the implementation**

Create `portfolio_dash/shared/money.py`:
```python
"""Decimal money primitives: TEXT persistence and per-currency quantization.

Money is never ``float``. Decimals are stored at full source precision as canonical
fixed-point strings and quantized to a currency's minor unit only at settlement/display.
"""

from decimal import ROUND_HALF_UP, Decimal

from .enums import Currency

# Minor-unit decimal places per currency (settlement precision).
MINOR_UNITS: dict[Currency, int] = {
    Currency.TWD: 0,  # whole NT$
    Currency.USD: 2,  # cent
    Currency.MYR: 2,  # sen
}


def to_db(value: Decimal) -> str:
    """Serialize a Decimal to a canonical fixed-point TEXT string.

    Rejects ``float`` to enforce the no-float-money invariant. Preserves significant
    trailing zeros and never emits scientific notation, so the value round-trips
    losslessly via :func:`from_db`.
    """
    if isinstance(value, float):
        raise TypeError("money must be Decimal, not float")
    if not isinstance(value, Decimal):
        raise TypeError(f"expected Decimal, got {type(value).__name__}")
    return format(value, "f")


def from_db(text: str) -> Decimal:
    """Parse a TEXT-stored Decimal. Raises on an invalid string (no silent coercion)."""
    return Decimal(text)


def quantize_amount(
    value: Decimal, currency: Currency, rounding: str = ROUND_HALF_UP
) -> Decimal:
    """Quantize an amount to ``currency``'s minor unit (settlement precision).

    TWD -> 0 dp, USD/MYR -> 2 dp, using ROUND_HALF_UP (四捨五入). Call only at
    settlement/display — prices and FX rates are stored at full precision.
    """
    try:
        minor = MINOR_UNITS[currency]
    except KeyError as exc:
        raise ValueError(f"unknown currency: {currency!r}") from exc
    exponent = Decimal(1).scaleb(-minor)
    return value.quantize(exponent, rounding=rounding)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/shared/test_money.py -v`
Expected: PASS (10 passed).

- [ ] **Step 5: Commit**

```bash
git add portfolio_dash/shared/money.py tests/shared/test_money.py
git commit -m "feat(shared): add Decimal money primitives (TEXT persistence, quantize)" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: `fx.py` — pure FX conversion

**Files:**
- Create: `portfolio_dash/shared/fx.py`
- Test: `tests/shared/test_fx.py`

- [ ] **Step 1: Write the failing test**

Create `tests/shared/test_fx.py`:
```python
from decimal import Decimal

import pytest

from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.fx import convert


def test_convert_full_precision_no_quantize() -> None:
    # 100 USD at 32.125 TWD per USD; no target currency -> full precision kept.
    assert convert(Decimal("100"), Decimal("32.125")) == Decimal("3212.5")


def test_convert_quantizes_to_target_currency() -> None:
    # to TWD -> 0 dp, ROUND_HALF_UP (3212.5 -> 3213).
    assert convert(Decimal("100"), Decimal("32.125"), to_currency=Currency.TWD) == Decimal("3213")


def test_convert_quantizes_usd_two_dp() -> None:
    assert convert(Decimal("10"), Decimal("0.03125"), to_currency=Currency.USD) == Decimal("0.31")


def test_convert_negative_amount_allowed() -> None:
    assert convert(Decimal("-100"), Decimal("32")) == Decimal("-3200")


def test_convert_rejects_zero_rate() -> None:
    with pytest.raises(ValueError):
        convert(Decimal("100"), Decimal("0"))


def test_convert_rejects_negative_rate() -> None:
    with pytest.raises(ValueError):
        convert(Decimal("100"), Decimal("-1"))
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/shared/test_fx.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'portfolio_dash.shared.fx'`.

- [ ] **Step 3: Write the implementation**

Create `portfolio_dash/shared/fx.py`:
```python
"""The single FX-conversion helper. All currency conversion goes through here.

This is a pure function: the caller supplies the rate (rate selection by date/source is
a domain concern, not a shared concern). ``rate`` is expressed as: 1 unit of the source
currency = ``rate`` units of the target currency.
"""

from decimal import Decimal

from .enums import Currency
from .money import quantize_amount


def convert(
    amount: Decimal, rate: Decimal, *, to_currency: Currency | None = None
) -> Decimal:
    """Convert ``amount`` by ``rate``.

    Returns ``amount * rate`` at full precision. When ``to_currency`` is given, the
    result is quantized to that currency's minor unit (settlement). ``amount`` may be
    negative (cashflow signs); ``rate`` must be positive.
    """
    if rate <= 0:
        raise ValueError(f"FX rate must be positive, got {rate}")
    result = amount * rate
    if to_currency is not None:
        return quantize_amount(result, to_currency)
    return result
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/shared/test_fx.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add portfolio_dash/shared/fx.py tests/shared/test_fx.py
git commit -m "feat(shared): add pure FX-conversion helper" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: `config.py` — Settings & get_settings

**Files:**
- Create: `portfolio_dash/shared/config.py`
- Test: `tests/shared/test_config.py`

- [ ] **Step 1: Write the failing test**

Create `tests/shared/test_config.py`:
```python
from pathlib import Path

import pytest
from pydantic import ValidationError

from portfolio_dash.shared.config import Settings, get_settings
from portfolio_dash.shared.enums import Currency


def test_defaults() -> None:
    # _env_file=None isolates the test from any local .env on the dev box.
    s = Settings(_env_file=None)
    assert s.app_env == "dev"
    assert s.tz_display == "Asia/Taipei"
    assert s.reporting_currency == Currency.TWD
    assert isinstance(s.db_path, Path)


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("REPORTING_CURRENCY", "USD")
    s = Settings()
    assert s.app_env == "prod"
    assert s.reporting_currency == Currency.USD


def test_invalid_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    with pytest.raises(ValidationError):
        Settings()


def test_get_settings_is_cached() -> None:
    get_settings.cache_clear()
    assert get_settings() is get_settings()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/shared/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'portfolio_dash.shared.config'`.

- [ ] **Step 3: Write the implementation**

Create `portfolio_dash/shared/config.py`:
```python
"""Env-driven application settings (pydantic-settings)."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

from .enums import Currency


class Settings(BaseSettings):
    """Application settings, loaded from environment and ``.env``.

    Foundation fields only; LLM/LiteLLM settings arrive with ``llm_insight/``.
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    db_path: Path = Path("data/portfolio.db")
    app_env: Literal["dev", "prod"] = "dev"
    tz_display: str = "Asia/Taipei"  # display tz; storage is always UTC
    reporting_currency: Currency = Currency.TWD


@lru_cache
def get_settings() -> Settings:
    """Return the cached process-wide Settings singleton."""
    return Settings()
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/shared/test_config.py -v`
Expected: PASS (4 passed).

> If mypy flags `_env_file` as an unexpected argument in the test, append
> `  # type: ignore[call-arg]` to that line. Verify with `python -m mypy` before commit.

- [ ] **Step 5: Commit**

```bash
git add portfolio_dash/shared/config.py tests/shared/test_config.py
git commit -m "feat(shared): add env-driven Settings with cached singleton" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: `db.py` — SQLite connection & session

**Files:**
- Create: `portfolio_dash/shared/db.py`
- Test: `tests/shared/test_db.py`

- [ ] **Step 1: Write the failing test**

Create `tests/shared/test_db.py`:
```python
from collections.abc import Iterator
from pathlib import Path

import pytest

from portfolio_dash.shared import db
from portfolio_dash.shared.config import get_settings


@pytest.fixture
def tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(path))
    get_settings.cache_clear()
    yield path
    get_settings.cache_clear()


def test_get_connection_row_factory(tmp_db: Path) -> None:
    conn = db.get_connection()
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO t (name) VALUES ('x')")
        row = conn.execute("SELECT id, name FROM t").fetchone()
        assert row["name"] == "x"
    finally:
        conn.close()


def test_foreign_keys_pragma_on(tmp_db: Path) -> None:
    conn = db.get_connection()
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_session_commits_on_success(tmp_db: Path) -> None:
    with db.session() as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO t (id) VALUES (1)")
    conn2 = db.get_connection()
    try:
        assert conn2.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    finally:
        conn2.close()


def test_session_rolls_back_on_exception(tmp_db: Path) -> None:
    with db.session() as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    with pytest.raises(RuntimeError):
        with db.session() as conn:
            conn.execute("INSERT INTO t (id) VALUES (1)")
            raise RuntimeError("boom")
    conn2 = db.get_connection()
    try:
        assert conn2.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    finally:
        conn2.close()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/shared/test_db.py -v`
Expected: FAIL — `ImportError: cannot import name 'db' from 'portfolio_dash.shared'` (the submodule does not exist yet).

- [ ] **Step 3: Write the implementation**

Create `portfolio_dash/shared/db.py`:
```python
"""SQLite connection and session helpers (stdlib sqlite3, no ORM)."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from .config import get_settings


def get_connection() -> sqlite3.Connection:
    """Open a SQLite connection to the configured db file.

    Ensures the parent directory exists, sets ``Row`` row factory, and enables
    foreign-key enforcement and WAL journaling.
    """
    settings = get_settings()
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def session() -> Iterator[sqlite3.Connection]:
    """Yield a connection that commits on success, rolls back on error, always closes."""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/shared/test_db.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add portfolio_dash/shared/db.py tests/shared/test_db.py
git commit -m "feat(shared): add SQLite connection and session helpers" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Full verification & CHANGELOG

**Files:**
- Modify: `CHANGELOG.md` (`[Unreleased]` section only)

- [ ] **Step 1: Run the full test suite**

Run: `python -m pytest`
Expected: PASS (all tests, e.g. `28 passed`).

- [ ] **Step 2: Run mypy strict over everything**

Run: `python -m mypy`
Expected: `Success: no issues found`.

- [ ] **Step 3: Run ruff**

Run: `python -m ruff check .`
Expected: `All checks passed!`
(If import-ordering is flagged, run `python -m ruff check --fix .`, then re-run.)

- [ ] **Step 4: Update the CHANGELOG `[Unreleased]` section**

Replace the existing `## [Unreleased]` block (down to, but not including, `## [v0.0.0]`) with this bounded rewrite (per the engineering-process rule, prefer bounded-section rewrites over surgical edits):
```markdown
## [Unreleased]

### Added
- `shared/` foundation layer: `Currency`/`Market` enums; `Decimal` money primitives
  (canonical TEXT persistence via `to_db`/`from_db`, per-currency `quantize_amount`
  with ROUND_HALF_UP, float guard); single pure `fx.convert` helper; env-driven
  `Settings` + cached `get_settings`; stdlib `sqlite3` `get_connection`/`session`
  (WAL, foreign keys on).
- Package + tooling bootstrap: `pyproject.toml` (pydantic, pydantic-settings; dev:
  mypy strict, ruff, pytest, pytest-asyncio); `portfolio_dash/` package with
  `py.typed`; `tests/` layout.

### Planned
- `portfolio/` cost-basis & return core (weighted-average cost, original vs adjusted,
  realized / unrealized P&L, total return without double-counting dividends, XIRR).
- `forex/` currency-exchange ledger + realized/unrealized FX P&L (attribution).
- Data-source availability probe: US / TW / MY quotes; USD/TWD, USD/MYR, MYR/TWD FX;
  ex-dividend calendar.
```

- [ ] **Step 5: Verify CHANGELOG integrity**

Run (via the Bash tool / git-bash): `grep -c "^## \[v" CHANGELOG.md`
Expected: `1` (only `## [v0.0.0]`; `## [Unreleased]` is intentionally uncounted).

- [ ] **Step 6: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs: record shared/ foundation in CHANGELOG [Unreleased]" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Notes for the executor

- **Run all commands inside the activated `.venv`** (Task 1, Step 6). On a fresh shell,
  re-activate with `.\.venv\Scripts\Activate.ps1`.
- **TDD discipline:** never write implementation before its failing test is observed
  failing for the stated reason.
- **No floats for money.** If a test or impl ever needs a money literal, use
  `Decimal("…")`, never a float literal.
- `mypy` runs on both `portfolio_dash` and `tests`; keep test functions annotated
  (`-> None`) and fixtures typed.
