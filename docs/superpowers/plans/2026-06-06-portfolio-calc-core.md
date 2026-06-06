# Portfolio Calculation Core — Implementation Plan (sub-project ①)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the canonical domain models (`shared/models/`) and the pure-function calculation core (`portfolio/`) — cost basis, realized/unrealized P&L (adjusted-cost model), total return + rate, reporting-currency XIRR, sector allocation, and a combined multi-currency view — fully unit-tested over fixtures.

**Architecture:** A chronological ledger replay (`build_book`) produces open holdings (cost basis) and realized P&L; valuation, returns, XIRR, and allocation are layered pure functions over its output plus passed-in prices/FX. Money is `Decimal` end to end; no pandas. The adjusted-cost accounting model folds cash dividends into cost (no separate dividend line); `original_cost` is retained for the rate denominator and the capital-gain-vs-dividend split.

**Tech Stack:** Python 3.12, pydantic v2, `decimal`, `pyxirr` (new), pytest, mypy strict, ruff. Builds on the shipped `shared/` foundation (`enums`, `money`, `fx`, `config`, `db`).

**Spec:** `docs/superpowers/specs/2026-06-06-portfolio-calc-core-design.md`

---

## File Structure

| File | Responsibility |
| --- | --- |
| `pyproject.toml` | add `pyxirr` dependency |
| `.claude/rules/domain-ledger.md` | update P&L section to the adjusted-cost model (locked-decision override) |
| `CHANGELOG.md` | `[Unreleased]`: record decision change + this work |
| `portfolio_dash/shared/models/__init__.py` | models subpackage marker |
| `portfolio_dash/shared/models/types.py` | `Money` finite-`Decimal` annotated type |
| `portfolio_dash/shared/models/enums.py` | `Side`, `DividendType` |
| `portfolio_dash/shared/models/assets.py` | `Account`, `Instrument` |
| `portfolio_dash/shared/models/ledger.py` | `Transaction`, `Dividend`, `FXConversion`, `OpeningInventory` |
| `portfolio_dash/portfolio/__init__.py` | package marker |
| `portfolio_dash/portfolio/results.py` | result models (`Holding`, `RealizedRow`, `RealizedPnL`, `Book`, `CurrencyReturn`, `ReturnSummary`, `SectorAllocation`, `CombinedView`) |
| `portfolio_dash/portfolio/cost_basis.py` | `build_book()` + `OversellError` |
| `portfolio_dash/portfolio/pnl.py` | `value_holdings()` |
| `portfolio_dash/portfolio/returns.py` | `total_return()`, `xirr_reporting()` |
| `portfolio_dash/portfolio/allocation.py` | `sector_allocation()`, `combined_view()` |
| `tests/shared/models/…`, `tests/portfolio/…` | tests |

All tooling runs via the venv interpreter: `.\.venv\Scripts\python.exe -m pytest`, `... -m mypy`, `... -m ruff check .`. Work on a feature branch (`git checkout -b feat/portfolio-calc-core`). Commit messages end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

## Task 1: Decision record, dependency, and `Money` type

**Files:**
- Modify: `pyproject.toml`
- Modify: `.claude/rules/domain-ledger.md`
- Modify: `CHANGELOG.md`
- Create: `portfolio_dash/shared/models/__init__.py`
- Create: `portfolio_dash/shared/models/types.py`
- Test: `tests/shared/models/__init__.py`, `tests/shared/models/test_types.py`

- [ ] **Step 1: Add `pyxirr` to dependencies and install**

Edit `pyproject.toml` `[project] dependencies` to add `pyxirr`:
```toml
dependencies = [
    "pydantic>=2.7",
    "pydantic-settings>=2.3",
    "pyxirr>=0.10",
]
```
Run: `.\.venv\Scripts\pip.exe install -e ".[dev]"`
Expected: installs `pyxirr` successfully.

- [ ] **Step 2: Write the failing `Money` test**

Create `tests/shared/models/__init__.py` (empty) and `tests/shared/models/test_types.py`:
```python
from decimal import Decimal

import pytest
from pydantic import TypeAdapter, ValidationError

from portfolio_dash.shared.models.types import Money


def test_money_accepts_finite() -> None:
    assert TypeAdapter(Money).validate_python(Decimal("1.50")) == Decimal("1.50")


def test_money_rejects_nan() -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(Money).validate_python(Decimal("NaN"))


def test_money_rejects_infinity() -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(Money).validate_python(Decimal("Infinity"))
```

- [ ] **Step 3: Run it to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/shared/models/test_types.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'portfolio_dash.shared.models'`.

- [ ] **Step 4: Implement the models package + `Money`**

Create `portfolio_dash/shared/models/__init__.py`:
```python
"""shared.models — canonical cross-layer domain models."""
```

Create `portfolio_dash/shared/models/types.py`:
```python
"""Shared annotated types for domain models."""

from decimal import Decimal
from typing import Annotated

from pydantic import AfterValidator


def _ensure_finite(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise ValueError(f"value must be finite, got {value!r}")
    return value


# A Decimal that rejects NaN / Infinity at the model boundary.
Money = Annotated[Decimal, AfterValidator(_ensure_finite)]
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/shared/models/test_types.py -v`
Expected: PASS (3 passed).

- [ ] **Step 6: Update `domain-ledger.md` (locked-decision override)**

In `.claude/rules/domain-ledger.md`, replace the bullet list under **"## P&L and returns — single source of truth, NO double counting"** (the bullets from "Realized P&L" through "Total return rate denominator") with this adjusted-cost model (bounded-section rewrite):
```markdown
- **Accounting model = adjusted cost (decided 2026-06-06, supersedes the original-cost
  model below).** P&L is computed against `adjusted_cost`; cash dividends are folded into
  cost (NOT a separate income line). `original_cost` is never overwritten and is retained
  for the return-rate denominator and the capital-gain-vs-dividend split.
  - `adjusted_total = original_total − cumulative cash dividends`; `adjusted_avg =
    adjusted_total / shares`; **may be ≤ 0** (high-yield payback) — never floored.
  - **Realized P&L** (on sell) = net proceeds (after fees+tax) − `adjusted_avg × shares_sold`.
  - **Unrealized P&L** = (market − `adjusted_avg`) × shares.
  - **Total return** = realized + unrealized (both vs adjusted), incl. realized from
    closed positions. Dividends enter exactly once (via cost reduction); **no separate
    dividend line** (the old double-count trap).
  - **Total return rate** = total return / **original invested cost** (cumulative, not
    annualized). **XIRR** is the annualized, money-weighted, FX-aware decision metric.
  - **Cost basis is all-in:** buy-side fees + tax are part of `original_total` (and thus
    adjusted), so every transaction cost is captured.
- **Dividend treatment:** TW/MY cash → reduce `adjusted_total` by net received. US DRIP →
  net reinvested as $0-cost shares (does NOT reduce `adjusted_total`). 配股 → add shares,
  no cost change.
- **XIRR** (primary): buy − (gross incl. fees+tax), sell + (net), cash dividend + (net),
  DRIP neutral, opening inventory − (`original_cost_total` at build date), final value +;
  every flow at trade-date FX, single reporting currency. Display-only: 回本進度 /
  股利回收率 = cumulative cash dividends / original_total.
```

- [ ] **Step 7: Record the decision + work in `CHANGELOG.md`**

Replace the `## [Unreleased]` block (down to, but not including, `## [v0.0.0]`) with this bounded rewrite (**preserve the existing `shared/` Added entries** — only add the Changed section, the new portfolio Added bullets, and drop the now-in-progress `portfolio/` Planned line):
```markdown
## [Unreleased]

### Changed
- **Accounting model decision (2026-06-06, human sign-off):** P&L now uses the
  adjusted-cost model — cash dividends fold into cost (no separate dividend-income line),
  realized/unrealized computed vs `adjusted_cost`; `original_cost` retained for the
  return-rate denominator and the capital-gain-vs-dividend split. Supersedes the prior
  original-cost-plus-separate-dividend rule in `domain-ledger.md`. The no-double-count
  principle is preserved (dividends still counted exactly once). Return-rate denominator
  stays original invested cost; cost basis is all-in (incl. buy fees+tax).

### Added
- `shared/` foundation layer: `Currency`/`Market` enums; `Decimal` money primitives
  (canonical TEXT persistence via `to_db`/`from_db`, per-currency `quantize_amount`
  with ROUND_HALF_UP, float + non-finite guards); single pure `fx.convert` helper
  (rejects non-positive / non-finite rates); env-driven `Settings` + cached
  `get_settings`; stdlib `sqlite3` `get_connection`/`session` (WAL, foreign keys on).
- Package + tooling bootstrap: `pyproject.toml` (pydantic, pydantic-settings; dev:
  mypy strict, ruff, pytest, pytest-asyncio; strict `asyncio_mode`); `portfolio_dash/`
  package with `py.typed`; `tests/` layout.
- `portfolio/` calculation core: chronological ledger replay (`build_book`) →
  holdings + realized P&L; `value_holdings` (unrealized vs adjusted, capital-gain vs
  original, stale-price flagging); `total_return` (per-currency + reporting blended);
  reporting-currency `xirr_reporting` (pyxirr); `sector_allocation`; `combined_view`.
- `shared/models/`: canonical domain models (`Account`, `Instrument`, `Transaction`,
  `Dividend`, `FXConversion`, `OpeningInventory`) + `Money` finite-Decimal type.
- Dependency: `pyxirr` (irregular-cashflow XIRR).

### Planned
- `forex/` currency-exchange ledger + realized/unrealized FX P&L (attribution).
- Data-source availability probe: US / TW / MY quotes; USD/TWD, USD/MYR, MYR/TWD FX;
  ex-dividend calendar.
```

- [ ] **Step 8: Verify CHANGELOG integrity + tooling**

Run (via Bash tool / git-bash): `grep -c "^## \[v" CHANGELOG.md` → Expected: `1`.
Run: `.\.venv\Scripts\python.exe -m pytest -q` → all pass.
Run: `.\.venv\Scripts\python.exe -m mypy` → `Success`.
Run: `.\.venv\Scripts\python.exe -m ruff check .` → clean.

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml .claude/rules/domain-ledger.md CHANGELOG.md portfolio_dash/shared/models tests/shared/models
git commit -m "chore(portfolio): record adjusted-cost decision, add pyxirr + Money type" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Domain enums (`Side`, `DividendType`)

**Files:**
- Create: `portfolio_dash/shared/models/enums.py`
- Test: `tests/shared/models/test_enums.py`

- [ ] **Step 1: Write the failing test**

Create `tests/shared/models/test_enums.py`:
```python
from portfolio_dash.shared.models.enums import DividendType, Side


def test_side_members() -> None:
    assert {s.value for s in Side} == {"BUY", "SELL"}


def test_dividend_type_members() -> None:
    assert {d.value for d in DividendType} == {"CASH", "STOCK", "DRIP"}


def test_enums_are_str() -> None:
    assert Side.BUY == "BUY"
    assert DividendType.DRIP == "DRIP"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/shared/models/test_enums.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'portfolio_dash.shared.models.enums'`.

- [ ] **Step 3: Implement**

Create `portfolio_dash/shared/models/enums.py`:
```python
"""Domain enums for ledger entries."""

from enum import StrEnum


class Side(StrEnum):
    """Transaction side."""

    BUY = "BUY"
    SELL = "SELL"


class DividendType(StrEnum):
    """Dividend mechanism: cash payout, stock dividend (配股), or DRIP reinvest."""

    CASH = "CASH"
    STOCK = "STOCK"
    DRIP = "DRIP"
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/shared/models/test_enums.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add portfolio_dash/shared/models/enums.py tests/shared/models/test_enums.py
git commit -m "feat(models): add Side and DividendType enums" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Asset models (`Account`, `Instrument`)

**Files:**
- Create: `portfolio_dash/shared/models/assets.py`
- Test: `tests/shared/models/test_assets.py`

- [ ] **Step 1: Write the failing test**

Create `tests/shared/models/test_assets.py`:
```python
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Account, Instrument


def test_account_construction() -> None:
    acc = Account(
        account_id="schwab",
        name="Charles Schwab",
        broker="Schwab",
        settlement_ccy=Currency.USD,
        funding_ccy=Currency.TWD,
    )
    assert acc.settlement_ccy is Currency.USD
    assert acc.funding_ccy is Currency.TWD


def test_instrument_construction() -> None:
    inst = Instrument(
        symbol="AAPL",
        market=Market.US,
        quote_ccy=Currency.USD,
        sector="Technology",
        name="Apple Inc.",
    )
    assert inst.market is Market.US
    assert inst.quote_ccy is Currency.USD
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/shared/models/test_assets.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'portfolio_dash.shared.models.assets'`.

- [ ] **Step 3: Implement**

Create `portfolio_dash/shared/models/assets.py`:
```python
"""Account and Instrument models."""

from pydantic import BaseModel

from portfolio_dash.shared.enums import Currency, Market


class Account(BaseModel):
    """A broker account (first-class entity; fee/dividend rules bind here)."""

    account_id: str
    name: str
    broker: str
    settlement_ccy: Currency
    funding_ccy: Currency


class Instrument(BaseModel):
    """A tradable instrument; knows its market and quote currency."""

    symbol: str
    market: Market
    quote_ccy: Currency
    sector: str
    name: str
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/shared/models/test_assets.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add portfolio_dash/shared/models/assets.py tests/shared/models/test_assets.py
git commit -m "feat(models): add Account and Instrument" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Ledger models

**Files:**
- Create: `portfolio_dash/shared/models/ledger.py`
- Test: `tests/shared/models/test_ledger.py`

- [ ] **Step 1: Write the failing test**

Create `tests/shared/models/test_ledger.py`:
```python
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import (
    Dividend,
    FXConversion,
    OpeningInventory,
    Transaction,
)


def test_transaction_construction() -> None:
    tx = Transaction(
        account_id="tw",
        symbol="2330.TW",
        side=Side.BUY,
        quantity=Decimal("1000"),
        price=Decimal("600"),
        fees=Decimal("85"),
        tax=Decimal("0"),
        trade_date=date(2025, 1, 2),
    )
    assert tx.side is Side.BUY
    assert tx.quantity == Decimal("1000")


def test_transaction_rejects_nan_price() -> None:
    with pytest.raises(ValidationError):
        Transaction(
            account_id="tw",
            symbol="2330.TW",
            side=Side.BUY,
            quantity=Decimal("1000"),
            price=Decimal("NaN"),
            fees=Decimal("0"),
            tax=Decimal("0"),
            trade_date=date(2025, 1, 2),
        )


def test_dividend_drip_optional_fields() -> None:
    dv = Dividend(
        account_id="schwab",
        symbol="AAPL",
        date=date(2025, 2, 1),
        type=DividendType.DRIP,
        gross=Decimal("100"),
        withholding=Decimal("30"),
        net=Decimal("70"),
        reinvest_shares=Decimal("0.5"),
        reinvest_price=Decimal("140"),
    )
    assert dv.type is DividendType.DRIP
    assert dv.reinvest_shares == Decimal("0.5")


def test_fx_conversion_construction() -> None:
    fx = FXConversion(
        account_id="schwab",
        date=date(2025, 1, 1),
        from_ccy=Currency.TWD,
        from_amount=Decimal("320000"),
        to_ccy=Currency.USD,
        to_amount=Decimal("10000"),
    )
    assert fx.from_ccy is Currency.TWD


def test_opening_inventory_construction() -> None:
    oi = OpeningInventory(
        account_id="tw",
        symbol="2330.TW",
        shares=Decimal("2000"),
        original_avg_cost=Decimal("500"),
        original_cost_total=Decimal("1000000"),
        build_date=date(2024, 12, 31),
    )
    assert oi.original_cost_total == Decimal("1000000")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/shared/models/test_ledger.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'portfolio_dash.shared.models.ledger'`.

- [ ] **Step 3: Implement**

Create `portfolio_dash/shared/models/ledger.py`:
```python
"""Source-of-truth ledger models: transactions, dividends, FX, opening inventory."""

from datetime import date

from pydantic import BaseModel

from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.types import Money


class Transaction(BaseModel):
    """A buy or sell. Fees/tax are the snapshot taken at entry; stored, never recomputed."""

    account_id: str
    symbol: str
    side: Side
    quantity: Money
    price: Money
    fees: Money
    tax: Money
    trade_date: date


class Dividend(BaseModel):
    """A dividend event. `net` is what reduces adjusted cost (cash) or was reinvested."""

    account_id: str
    symbol: str
    date: date
    type: DividendType
    gross: Money
    withholding: Money
    net: Money
    reinvest_shares: Money | None = None
    reinvest_price: Money | None = None


class FXConversion(BaseModel):
    """An actual currency conversion (primarily consumed by sub-project ② forex)."""

    account_id: str
    date: date
    from_ccy: Currency
    from_amount: Money
    to_ccy: Currency
    to_amount: Money


class OpeningInventory(BaseModel):
    """A pre-existing position seeded at a build date (not a trade flow; feeds XIRR)."""

    account_id: str
    symbol: str
    shares: Money
    original_avg_cost: Money
    original_cost_total: Money
    build_date: date
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/shared/models/test_ledger.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add portfolio_dash/shared/models/ledger.py tests/shared/models/test_ledger.py
git commit -m "feat(models): add Transaction, Dividend, FXConversion, OpeningInventory" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Result models (`portfolio/results.py`)

**Files:**
- Create: `portfolio_dash/portfolio/__init__.py`
- Create: `portfolio_dash/portfolio/results.py`
- Test: `tests/portfolio/__init__.py`, `tests/portfolio/test_results.py`

- [ ] **Step 1: Write the failing test**

Create `tests/portfolio/__init__.py` (empty) and `tests/portfolio/test_results.py`:
```python
from decimal import Decimal

from portfolio_dash.shared.enums import Currency
from portfolio_dash.portfolio.results import (
    Book,
    CombinedView,
    CurrencyReturn,
    Holding,
    RealizedPnL,
    RealizedRow,
    ReturnSummary,
    SectorAllocation,
)


def test_holding_defaults_market_fields_none() -> None:
    h = Holding(
        account_id="tw",
        symbol="2330.TW",
        quote_ccy=Currency.TWD,
        shares=Decimal("1000"),
        original_avg=Decimal("600"),
        adjusted_avg=Decimal("580"),
        original_cost_total=Decimal("600000"),
        adjusted_cost_total=Decimal("580000"),
        dividend_portion=Decimal("20000"),
        payback_ratio=Decimal("0.0333"),
    )
    assert h.market_price is None
    assert h.price_stale is False


def test_book_holds_components() -> None:
    book = Book(
        holdings=[],
        realized=RealizedPnL(rows=[], by_currency={}),
        gross_invested={Currency.TWD: Decimal("0")},
    )
    assert book.gross_invested[Currency.TWD] == Decimal("0")


def test_return_summary_optional_xirr() -> None:
    rs = ReturnSummary(
        by_currency={
            Currency.USD: CurrencyReturn(
                realized=Decimal("0"),
                unrealized=Decimal("100"),
                total_return=Decimal("100"),
                gross_invested=Decimal("1000"),
                rate=Decimal("0.1"),
            )
        },
        reporting_currency=Currency.TWD,
        reporting_total_return=Decimal("3200"),
    )
    assert rs.xirr is None
    assert rs.by_currency[Currency.USD].rate == Decimal("0.1")


def test_realized_row_and_allocation_models() -> None:
    row = RealizedRow(
        account_id="tw",
        symbol="2330.TW",
        quote_ccy=Currency.TWD,
        shares_sold=Decimal("500"),
        proceeds_net=Decimal("310000"),
        adjusted_cost_removed=Decimal("290000"),
        realized=Decimal("20000"),
    )
    assert row.realized == Decimal("20000")
    sa = SectorAllocation(
        by_sector={"Tech": Decimal("100")},
        weights={"Tech": Decimal("1")},
        reporting_currency=Currency.TWD,
    )
    cv = CombinedView(
        by_currency_value={Currency.TWD: Decimal("100")},
        reporting_total_value=Decimal("100"),
        reporting_currency=Currency.TWD,
    )
    assert sa.weights["Tech"] == Decimal("1")
    assert cv.reporting_total_value == Decimal("100")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/portfolio/test_results.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'portfolio_dash.portfolio'`.

- [ ] **Step 3: Implement**

Create `portfolio_dash/portfolio/__init__.py`:
```python
"""portfolio — calculation core: cost basis, P&L, returns, allocation."""
```

Create `portfolio_dash/portfolio/results.py`:
```python
"""Computed result models produced by the calculation core."""

from decimal import Decimal

from pydantic import BaseModel

from portfolio_dash.shared.enums import Currency


class Holding(BaseModel):
    """An open position with cost basis and (once valued) market fields."""

    account_id: str
    symbol: str
    quote_ccy: Currency
    shares: Decimal
    original_avg: Decimal
    adjusted_avg: Decimal
    original_cost_total: Decimal
    adjusted_cost_total: Decimal
    dividend_portion: Decimal
    payback_ratio: Decimal
    market_price: Decimal | None = None
    market_value: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    capital_gain: Decimal | None = None
    price_stale: bool = False


class RealizedRow(BaseModel):
    """One realized event from a sell."""

    account_id: str
    symbol: str
    quote_ccy: Currency
    shares_sold: Decimal
    proceeds_net: Decimal
    adjusted_cost_removed: Decimal
    realized: Decimal


class RealizedPnL(BaseModel):
    """All realized rows plus per-currency totals."""

    rows: list[RealizedRow]
    by_currency: dict[Currency, Decimal]


class Book(BaseModel):
    """Output of the ledger replay: open holdings, realized, gross capital deployed."""

    holdings: list[Holding]
    realized: RealizedPnL
    gross_invested: dict[Currency, Decimal]


class CurrencyReturn(BaseModel):
    """Per-currency return breakdown."""

    realized: Decimal
    unrealized: Decimal
    total_return: Decimal
    gross_invested: Decimal
    rate: Decimal | None


class ReturnSummary(BaseModel):
    """Per-currency returns + blended reporting-currency total + XIRR."""

    by_currency: dict[Currency, CurrencyReturn]
    reporting_currency: Currency
    reporting_total_return: Decimal
    xirr: Decimal | None = None


class SectorAllocation(BaseModel):
    """Reporting-currency value and weight per sector."""

    by_sector: dict[str, Decimal]
    weights: dict[str, Decimal]
    reporting_currency: Currency


class CombinedView(BaseModel):
    """Per-currency market value + blended reporting-currency total."""

    by_currency_value: dict[Currency, Decimal]
    reporting_total_value: Decimal
    reporting_currency: Currency
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/portfolio/test_results.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add portfolio_dash/portfolio/__init__.py portfolio_dash/portfolio/results.py tests/portfolio/__init__.py tests/portfolio/test_results.py
git commit -m "feat(portfolio): add computed result models" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: `build_book` — chronological ledger replay

This is the heart of the calc core: one chronological pass over opening inventory + transactions + dividends producing open holdings (cost basis) and realized P&L.

**Files:**
- Create: `portfolio_dash/portfolio/cost_basis.py`
- Test: `tests/portfolio/test_cost_basis.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/portfolio/test_cost_basis.py`:
```python
from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import (
    Dividend,
    OpeningInventory,
    Transaction,
)
from portfolio_dash.portfolio.cost_basis import OversellError, build_book

TW = Instrument(symbol="2330.TW", market=Market.TW, quote_ccy=Currency.TWD,
                sector="Tech", name="TSMC")
US = Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                sector="Tech", name="Apple")
INSTR = {"2330.TW": TW, "AAPL": US}


def _buy(sym: str, qty: str, price: str, d: date, fees: str = "0", acc: str = "a") -> Transaction:
    return Transaction(account_id=acc, symbol=sym, side=Side.BUY, quantity=Decimal(qty),
                       price=Decimal(price), fees=Decimal(fees), tax=Decimal("0"), trade_date=d)


def _sell(sym: str, qty: str, price: str, d: date, fees: str = "0", tax: str = "0", acc: str = "a") -> Transaction:
    return Transaction(account_id=acc, symbol=sym, side=Side.SELL, quantity=Decimal(qty),
                       price=Decimal(price), fees=Decimal(fees), tax=Decimal(tax), trade_date=d)


def test_buys_weighted_average_includes_fees() -> None:
    txs = [_buy("AAPL", "10", "100", date(2025, 1, 1), fees="5"),
           _buy("AAPL", "10", "120", date(2025, 1, 2), fees="5")]
    book = build_book(txs, [], [], INSTR)
    h = book.holdings[0]
    # original_total = 10*100+5 + 10*120+5 = 1005 + 1205 = 2210 over 20 shares
    assert h.shares == Decimal("20")
    assert h.original_cost_total == Decimal("2210")
    assert h.original_avg == Decimal("110.5")
    assert h.adjusted_avg == Decimal("110.5")  # no dividends yet
    assert book.gross_invested[Currency.USD] == Decimal("2210")


def test_opening_inventory_seeds_position() -> None:
    oi = OpeningInventory(account_id="a", symbol="2330.TW", shares=Decimal("1000"),
                          original_avg_cost=Decimal("500"), original_cost_total=Decimal("500000"),
                          build_date=date(2024, 12, 31))
    book = build_book([], [], [oi], INSTR)
    h = book.holdings[0]
    assert h.shares == Decimal("1000")
    assert h.original_cost_total == Decimal("500000")
    assert book.gross_invested[Currency.TWD] == Decimal("500000")


def test_sell_realized_vs_adjusted_and_reduces_shares() -> None:
    txs = [_buy("AAPL", "10", "100", date(2025, 1, 1)),
           _sell("AAPL", "4", "150", date(2025, 1, 3), fees="2")]
    book = build_book(txs, [], [], INSTR)
    # adjusted_avg before sell = 100; realized = (4*150 - 2) - 100*4 = 598 - 400 = 198
    assert book.realized.rows[0].realized == Decimal("198")
    assert book.realized.by_currency[Currency.USD] == Decimal("198")
    h = book.holdings[0]
    assert h.shares == Decimal("6")
    assert h.original_cost_total == Decimal("600")  # 1000 * (6/10)


def test_oversell_raises() -> None:
    txs = [_buy("AAPL", "5", "100", date(2025, 1, 1)),
           _sell("AAPL", "6", "150", date(2025, 1, 2))]
    with pytest.raises(OversellError):
        build_book(txs, [], [], INSTR)


def test_cash_dividend_reduces_adjusted_and_split() -> None:
    txs = [_buy("2330.TW", "1000", "100", date(2025, 1, 1))]
    divs = [Dividend(account_id="a", symbol="2330.TW", date=date(2025, 6, 1),
                     type=DividendType.CASH, gross=Decimal("20000"),
                     withholding=Decimal("0"), net=Decimal("20000"))]
    book = build_book(txs, divs, [], INSTR)
    h = book.holdings[0]
    assert h.original_cost_total == Decimal("100000")  # never reduced
    assert h.adjusted_cost_total == Decimal("80000")   # 100000 - 20000
    assert h.adjusted_avg == Decimal("80")
    assert h.dividend_portion == Decimal("20000")
    assert h.payback_ratio == Decimal("0.2")


def test_adjusted_cost_may_go_negative() -> None:
    txs = [_buy("2330.TW", "1000", "10", date(2025, 1, 1))]  # cost 10000
    divs = [Dividend(account_id="a", symbol="2330.TW", date=date(2025, 6, 1),
                     type=DividendType.CASH, gross=Decimal("12000"),
                     withholding=Decimal("0"), net=Decimal("12000"))]
    book = build_book(txs, divs, [], INSTR)
    assert book.holdings[0].adjusted_cost_total == Decimal("-2000")  # not floored


def test_drip_adds_zero_cost_shares() -> None:
    txs = [_buy("AAPL", "10", "100", date(2025, 1, 1))]
    divs = [Dividend(account_id="a", symbol="AAPL", date=date(2025, 6, 1),
                     type=DividendType.DRIP, gross=Decimal("100"), withholding=Decimal("30"),
                     net=Decimal("70"), reinvest_shares=Decimal("0.5"), reinvest_price=Decimal("140"))]
    book = build_book(txs, divs, [], INSTR)
    h = book.holdings[0]
    assert h.shares == Decimal("10.5")
    assert h.original_cost_total == Decimal("1000")   # unchanged (zero-cost shares)
    assert h.adjusted_cost_total == Decimal("1000")   # DRIP does NOT reduce adjusted


def test_stock_dividend_adds_shares_no_cost_change() -> None:
    txs = [_buy("2330.TW", "1000", "100", date(2025, 1, 1))]
    divs = [Dividend(account_id="a", symbol="2330.TW", date=date(2025, 6, 1),
                     type=DividendType.STOCK, gross=Decimal("0"), withholding=Decimal("0"),
                     net=Decimal("0"), reinvest_shares=Decimal("100"))]
    book = build_book(txs, divs, [], INSTR)
    h = book.holdings[0]
    assert h.shares == Decimal("1100")
    assert h.original_cost_total == Decimal("100000")


def test_fully_sold_position_excluded_from_holdings() -> None:
    txs = [_buy("AAPL", "10", "100", date(2025, 1, 1)),
           _sell("AAPL", "10", "150", date(2025, 1, 2))]
    book = build_book(txs, [], [], INSTR)
    assert book.holdings == []
    assert book.realized.by_currency[Currency.USD] == Decimal("500")


def test_equivalence_adjusted_total_equals_original_plus_dividends() -> None:
    # Invariant: (price - adjusted_avg)*sh == (price - original_avg)*sh + cumulative_div
    txs = [_buy("2330.TW", "1000", "100", date(2025, 1, 1))]
    divs = [Dividend(account_id="a", symbol="2330.TW", date=date(2025, 6, 1),
                     type=DividendType.CASH, gross=Decimal("20000"),
                     withholding=Decimal("0"), net=Decimal("20000"))]
    book = build_book(txs, divs, [], INSTR)
    h = book.holdings[0]
    price = Decimal("110")
    adj_model = (price - h.adjusted_avg) * h.shares
    orig_model = (price - h.original_avg) * h.shares + Decimal("20000")
    assert adj_model == orig_model
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/portfolio/test_cost_basis.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'portfolio_dash.portfolio.cost_basis'`.

- [ ] **Step 3: Implement**

Create `portfolio_dash/portfolio/cost_basis.py`:
```python
"""Chronological ledger replay → open holdings (cost basis) + realized P&L."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import Dividend, OpeningInventory, Transaction
from portfolio_dash.portfolio.results import Book, Holding, RealizedPnL, RealizedRow

_ZERO = Decimal("0")


class OversellError(Exception):
    """A sell quantity exceeds held shares (input error vs short sale — require confirm)."""


@dataclass
class _Position:
    quote_ccy: Currency
    shares: Decimal = field(default_factory=lambda: Decimal("0"))
    original_total: Decimal = field(default_factory=lambda: Decimal("0"))
    adjusted_total: Decimal = field(default_factory=lambda: Decimal("0"))


def build_book(
    transactions: list[Transaction],
    dividends: list[Dividend],
    opening: list[OpeningInventory],
    instruments: dict[str, Instrument],
) -> Book:
    """Replay the ledger in date order; return open holdings, realized P&L, gross invested.

    Same-day ordering: opening (0) -> buy (1) -> sell (2) -> dividend (3).
    """

    def quote_ccy(symbol: str) -> Currency:
        inst = instruments.get(symbol)
        if inst is None:
            raise KeyError(f"unknown instrument: {symbol}")
        return inst.quote_ccy

    positions: dict[tuple[str, str], _Position] = {}
    realized_rows: list[RealizedRow] = []
    gross: dict[Currency, Decimal] = defaultdict(lambda: Decimal("0"))

    events: list[tuple[date, int, str, object]] = []
    for oi in opening:
        events.append((oi.build_date, 0, "open", oi))
    for tx in transactions:
        events.append((tx.trade_date, 1 if tx.side is Side.BUY else 2, "tx", tx))
    for dv in dividends:
        events.append((dv.date, 3, "div", dv))
    events.sort(key=lambda e: (e[0], e[1]))

    for _d, _p, kind, ev in events:
        if kind == "open":
            assert isinstance(ev, OpeningInventory)
            key = (ev.account_id, ev.symbol)
            pos = positions.setdefault(key, _Position(quote_ccy(ev.symbol)))
            pos.shares += ev.shares
            pos.original_total += ev.original_cost_total
            pos.adjusted_total += ev.original_cost_total
            gross[pos.quote_ccy] += ev.original_cost_total
        elif kind == "tx":
            assert isinstance(ev, Transaction)
            ccy = quote_ccy(ev.symbol)
            key = (ev.account_id, ev.symbol)
            pos = positions.setdefault(key, _Position(ccy))
            if ev.side is Side.BUY:
                cost = ev.quantity * ev.price + ev.fees + ev.tax
                pos.shares += ev.quantity
                pos.original_total += cost
                pos.adjusted_total += cost
                gross[ccy] += cost
            else:
                if ev.quantity > pos.shares:
                    raise OversellError(
                        f"sell {ev.quantity} > held {pos.shares} for {ev.symbol}"
                    )
                frac = ev.quantity / pos.shares
                original_removed = pos.original_total * frac
                adjusted_removed = pos.adjusted_total * frac
                proceeds_net = ev.quantity * ev.price - ev.fees - ev.tax
                realized_rows.append(
                    RealizedRow(
                        account_id=ev.account_id,
                        symbol=ev.symbol,
                        quote_ccy=ccy,
                        shares_sold=ev.quantity,
                        proceeds_net=proceeds_net,
                        adjusted_cost_removed=adjusted_removed,
                        realized=proceeds_net - adjusted_removed,
                    )
                )
                pos.shares -= ev.quantity
                pos.original_total -= original_removed
                pos.adjusted_total -= adjusted_removed
        else:  # dividend
            assert isinstance(ev, Dividend)
            key = (ev.account_id, ev.symbol)
            pos = positions.setdefault(key, _Position(quote_ccy(ev.symbol)))
            if ev.type is DividendType.CASH:
                pos.adjusted_total -= ev.net
            else:  # DRIP / STOCK add shares at zero cost
                pos.shares += ev.reinvest_shares or _ZERO

    holdings: list[Holding] = []
    for (account_id, symbol), pos in positions.items():
        if pos.shares == _ZERO:
            continue
        original_avg = pos.original_total / pos.shares
        adjusted_avg = pos.adjusted_total / pos.shares
        dividend_portion = pos.original_total - pos.adjusted_total
        payback = dividend_portion / pos.original_total if pos.original_total != _ZERO else _ZERO
        holdings.append(
            Holding(
                account_id=account_id,
                symbol=symbol,
                quote_ccy=pos.quote_ccy,
                shares=pos.shares,
                original_avg=original_avg,
                adjusted_avg=adjusted_avg,
                original_cost_total=pos.original_total,
                adjusted_cost_total=pos.adjusted_total,
                dividend_portion=dividend_portion,
                payback_ratio=payback,
            )
        )

    realized_by_ccy: dict[Currency, Decimal] = defaultdict(lambda: Decimal("0"))
    for r in realized_rows:
        realized_by_ccy[r.quote_ccy] += r.realized

    return Book(
        holdings=holdings,
        realized=RealizedPnL(rows=realized_rows, by_currency=dict(realized_by_ccy)),
        gross_invested=dict(gross),
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/portfolio/test_cost_basis.py -v`
Expected: PASS (10 passed).
Run: `.\.venv\Scripts\python.exe -m mypy` → `Success`. `... -m ruff check .` → clean.

- [ ] **Step 5: Commit**

```bash
git add portfolio_dash/portfolio/cost_basis.py tests/portfolio/test_cost_basis.py
git commit -m "feat(portfolio): add build_book ledger replay (cost basis + realized)" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: `value_holdings` — valuation & unrealized P&L

**Files:**
- Create: `portfolio_dash/portfolio/pnl.py`
- Test: `tests/portfolio/test_pnl.py`

- [ ] **Step 1: Write the failing test**

Create `tests/portfolio/test_pnl.py`:
```python
from decimal import Decimal

from portfolio_dash.shared.enums import Currency
from portfolio_dash.portfolio.pnl import value_holdings
from portfolio_dash.portfolio.results import Holding


def _holding(symbol: str, shares: str, orig: str, adj: str) -> Holding:
    return Holding(
        account_id="a", symbol=symbol, quote_ccy=Currency.USD, shares=Decimal(shares),
        original_avg=Decimal(orig), adjusted_avg=Decimal(adj),
        original_cost_total=Decimal(shares) * Decimal(orig),
        adjusted_cost_total=Decimal(shares) * Decimal(adj),
        dividend_portion=Decimal("0"), payback_ratio=Decimal("0"),
    )


def test_value_holdings_unrealized_and_capital_gain() -> None:
    h = _holding("AAPL", "10", "100", "90")
    [valued] = value_holdings([h], {"AAPL": Decimal("120")})
    assert valued.market_value == Decimal("1200")
    assert valued.unrealized_pnl == Decimal("300")   # (120-90)*10
    assert valued.capital_gain == Decimal("200")      # (120-100)*10
    assert valued.price_stale is False


def test_value_holdings_missing_price_marks_stale() -> None:
    h = _holding("AAPL", "10", "100", "90")
    [valued] = value_holdings([h], {})
    assert valued.market_price is None
    assert valued.market_value is None
    assert valued.unrealized_pnl is None
    assert valued.capital_gain is None
    assert valued.price_stale is True
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/portfolio/test_pnl.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'portfolio_dash.portfolio.pnl'`.

- [ ] **Step 3: Implement**

Create `portfolio_dash/portfolio/pnl.py`:
```python
"""Valuation: fill market fields and unrealized P&L from a current-price map."""

from decimal import Decimal

from portfolio_dash.portfolio.results import Holding


def value_holdings(holdings: list[Holding], price_map: dict[str, Decimal]) -> list[Holding]:
    """Return new Holdings with market fields filled. Missing price -> stale, never faked."""
    out: list[Holding] = []
    for h in holdings:
        price = price_map.get(h.symbol)
        if price is None:
            out.append(
                h.model_copy(
                    update={
                        "market_price": None,
                        "market_value": None,
                        "unrealized_pnl": None,
                        "capital_gain": None,
                        "price_stale": True,
                    }
                )
            )
        else:
            out.append(
                h.model_copy(
                    update={
                        "market_price": price,
                        "market_value": price * h.shares,
                        "unrealized_pnl": (price - h.adjusted_avg) * h.shares,
                        "capital_gain": (price - h.original_avg) * h.shares,
                        "price_stale": False,
                    }
                )
            )
    return out
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/portfolio/test_pnl.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add portfolio_dash/portfolio/pnl.py tests/portfolio/test_pnl.py
git commit -m "feat(portfolio): add value_holdings (unrealized + stale handling)" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: `total_return` — per-currency returns + blended reporting total

**Files:**
- Create: `portfolio_dash/portfolio/returns.py`
- Test: `tests/portfolio/test_total_return.py`

- [ ] **Step 1: Write the failing test**

Create `tests/portfolio/test_total_return.py`:
```python
from decimal import Decimal

from portfolio_dash.shared.enums import Currency
from portfolio_dash.portfolio.results import Book, Holding, RealizedPnL
from portfolio_dash.portfolio.returns import total_return


def _fx(frm: Currency, to: Currency) -> Decimal:
    if frm is to:
        return Decimal("1")
    rates = {(Currency.USD, Currency.TWD): Decimal("32")}
    return rates[(frm, to)]


def _valued(symbol: str, ccy: Currency, shares: str, adj: str, price: str) -> Holding:
    sh, a, p = Decimal(shares), Decimal(adj), Decimal(price)
    return Holding(
        account_id="a", symbol=symbol, quote_ccy=ccy, shares=sh,
        original_avg=a, adjusted_avg=a, original_cost_total=sh * a,
        adjusted_cost_total=sh * a, dividend_portion=Decimal("0"),
        payback_ratio=Decimal("0"), market_price=p, market_value=p * sh,
        unrealized_pnl=(p - a) * sh, capital_gain=(p - a) * sh, price_stale=False,
    )


def test_total_return_per_currency_and_blended() -> None:
    book = Book(
        holdings=[],
        realized=RealizedPnL(rows=[], by_currency={Currency.USD: Decimal("100")}),
        gross_invested={Currency.USD: Decimal("1000")},
    )
    valued = [_valued("AAPL", Currency.USD, "10", "100", "120")]  # unrealized 200
    rs = total_return(book, valued, _fx, Currency.TWD)
    usd = rs.by_currency[Currency.USD]
    assert usd.realized == Decimal("100")
    assert usd.unrealized == Decimal("200")
    assert usd.total_return == Decimal("300")
    assert usd.rate == Decimal("0.3")  # 300 / 1000
    assert rs.reporting_total_return == Decimal("9600")  # 300 USD * 32
    assert rs.reporting_currency is Currency.TWD


def test_total_return_zero_gross_rate_none() -> None:
    book = Book(holdings=[], realized=RealizedPnL(rows=[], by_currency={}),
               gross_invested={Currency.USD: Decimal("0")})
    rs = total_return(book, [], _fx, Currency.USD)
    assert rs.by_currency[Currency.USD].rate is None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/portfolio/test_total_return.py -v`
Expected: FAIL — `ImportError: cannot import name 'total_return' from 'portfolio_dash.portfolio.returns'` (module/function absent).

- [ ] **Step 3: Implement**

Create `portfolio_dash/portfolio/returns.py`:
```python
"""Returns: per-currency total return + blended reporting total, and reporting XIRR."""

from collections import defaultdict
from collections.abc import Callable
from decimal import Decimal

from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.fx import convert
from portfolio_dash.portfolio.results import (
    Book,
    CurrencyReturn,
    Holding,
    ReturnSummary,
)

_ZERO = Decimal("0")
FxRate = Callable[[Currency, Currency], Decimal]


def total_return(
    book: Book,
    valued_holdings: list[Holding],
    current_fx: FxRate,
    reporting: Currency,
) -> ReturnSummary:
    """Per-currency realized+unrealized and rate (vs gross invested); blended at spot."""
    unrealized: dict[Currency, Decimal] = defaultdict(lambda: Decimal("0"))
    for h in valued_holdings:
        if h.unrealized_pnl is not None:
            unrealized[h.quote_ccy] += h.unrealized_pnl

    ccys = set(book.gross_invested) | set(book.realized.by_currency) | set(unrealized)
    by_ccy: dict[Currency, CurrencyReturn] = {}
    reporting_total = _ZERO
    for ccy in ccys:
        realized_c = book.realized.by_currency.get(ccy, _ZERO)
        unreal_c = unrealized.get(ccy, _ZERO)
        gross_c = book.gross_invested.get(ccy, _ZERO)
        total_c = realized_c + unreal_c
        by_ccy[ccy] = CurrencyReturn(
            realized=realized_c,
            unrealized=unreal_c,
            total_return=total_c,
            gross_invested=gross_c,
            rate=(total_c / gross_c) if gross_c != _ZERO else None,
        )
        reporting_total += convert(total_c, current_fx(ccy, reporting))

    return ReturnSummary(
        by_currency=by_ccy,
        reporting_currency=reporting,
        reporting_total_return=reporting_total,
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/portfolio/test_total_return.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add portfolio_dash/portfolio/returns.py tests/portfolio/test_total_return.py
git commit -m "feat(portfolio): add total_return (per-currency + blended reporting)" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 9: `xirr_reporting` — reporting-currency money-weighted XIRR

**Files:**
- Modify: `portfolio_dash/portfolio/returns.py`
- Test: `tests/portfolio/test_xirr.py`

- [ ] **Step 1: Write the failing test**

Create `tests/portfolio/test_xirr.py`:
```python
from datetime import date
from decimal import Decimal

from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import Dividend, OpeningInventory, Transaction
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.portfolio.returns import xirr_reporting

US = Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD, sector="Tech", name="Apple")
INSTR = {"AAPL": US}


def _fx_one(_d: date, frm: Currency, to: Currency) -> Decimal:
    return Decimal("1")  # USD reporting, single currency


def _spot_one(frm: Currency, to: Currency) -> Decimal:
    return Decimal("1")


def test_xirr_simple_doubling_in_one_year() -> None:
    # Buy 1 share for 100 on day 0; worth 110 one year later -> ~10%.
    txs = [Transaction(account_id="a", symbol="AAPL", side=Side.BUY, quantity=Decimal("1"),
                       price=Decimal("100"), fees=Decimal("0"), tax=Decimal("0"),
                       trade_date=date(2024, 1, 1))]
    book = build_book(txs, [], [], INSTR)
    rate = xirr_reporting(txs, [], [], book.holdings, INSTR, _fx_one,
                          {"AAPL": Decimal("110")}, _spot_one, date(2025, 1, 1), Currency.USD)
    assert rate is not None
    assert Decimal("0.09") < rate < Decimal("0.11")


def test_xirr_cash_dividend_counts_as_inflow() -> None:
    txs = [Transaction(account_id="a", symbol="AAPL", side=Side.BUY, quantity=Decimal("1"),
                       price=Decimal("100"), fees=Decimal("0"), tax=Decimal("0"),
                       trade_date=date(2024, 1, 1))]
    divs = [Dividend(account_id="a", symbol="AAPL", date=date(2024, 7, 1),
                     type=DividendType.CASH, gross=Decimal("5"), withholding=Decimal("0"),
                     net=Decimal("5"))]
    book = build_book(txs, divs, [], INSTR)
    rate = xirr_reporting(txs, divs, [], book.holdings, INSTR, _fx_one,
                          {"AAPL": Decimal("100")}, _spot_one, date(2025, 1, 1), Currency.USD)
    assert rate is not None
    assert rate > Decimal("0")  # flat price but a dividend -> positive return


def test_xirr_missing_price_returns_none() -> None:
    txs = [Transaction(account_id="a", symbol="AAPL", side=Side.BUY, quantity=Decimal("1"),
                       price=Decimal("100"), fees=Decimal("0"), tax=Decimal("0"),
                       trade_date=date(2024, 1, 1))]
    book = build_book(txs, [], [], INSTR)
    rate = xirr_reporting(txs, [], [], book.holdings, INSTR, _fx_one,
                          {}, _spot_one, date(2025, 1, 1), Currency.USD)
    assert rate is None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/portfolio/test_xirr.py -v`
Expected: FAIL — `ImportError: cannot import name 'xirr_reporting' from 'portfolio_dash.portfolio.returns'`.

- [ ] **Step 3: Implement (append to `returns.py`)**

Add these imports at the top of `portfolio_dash/portfolio/returns.py` (merge with existing import block, keeping ruff ordering):
```python
from datetime import date

from pyxirr import xirr as _xirr

from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import Dividend, OpeningInventory, Transaction
```

Append to `portfolio_dash/portfolio/returns.py`:
```python
DateFxRate = Callable[[date, Currency, Currency], Decimal]


def xirr_reporting(
    transactions: list[Transaction],
    dividends: list[Dividend],
    opening: list[OpeningInventory],
    holdings: list[Holding],
    instruments: dict[str, Instrument],
    fx_at: DateFxRate,
    current_prices: dict[str, Decimal],
    current_fx: FxRate,
    as_of: date,
    reporting: Currency,
) -> Decimal | None:
    """Reporting-currency money-weighted XIRR. Returns None if it cannot be computed.

    Flows: buy - (gross incl. fees+tax), sell + (net), cash dividend + (net), DRIP/stock
    neutral, opening - (original_cost_total at build_date), final market value + at as_of.
    Each flow converted at its trade-date FX; final value at current spot.
    """

    def ccy_of(symbol: str) -> Currency:
        inst = instruments.get(symbol)
        if inst is None:
            raise KeyError(f"unknown instrument: {symbol}")
        return inst.quote_ccy

    dates: list[date] = []
    amounts: list[float] = []

    def add(d: date, ccy: Currency, native: Decimal) -> None:
        dates.append(d)
        amounts.append(float(convert(native, fx_at(d, ccy, reporting))))

    for oi in opening:
        add(oi.build_date, ccy_of(oi.symbol), -oi.original_cost_total)
    for tx in transactions:
        ccy = ccy_of(tx.symbol)
        if tx.side is Side.BUY:
            add(tx.trade_date, ccy, -(tx.quantity * tx.price + tx.fees + tx.tax))
        else:
            add(tx.trade_date, ccy, tx.quantity * tx.price - tx.fees - tx.tax)
    for dv in dividends:
        if dv.type is DividendType.CASH:
            add(dv.date, ccy_of(dv.symbol), dv.net)
        # DRIP / STOCK are neutral (no external cashflow)

    # Final value of open holdings at current spot; missing any price -> cannot compute.
    final = Decimal("0")
    for h in holdings:
        price = current_prices.get(h.symbol)
        if price is None:
            return None
        final += convert(price * h.shares, current_fx(h.quote_ccy, reporting))
    if final != _ZERO:
        dates.append(as_of)
        amounts.append(float(final))

    try:
        rate = _xirr(dates, amounts)
    except Exception:
        return None
    if rate is None:
        return None
    return Decimal(str(rate))
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/portfolio/test_xirr.py -v`
Expected: PASS (3 passed).
Run mypy + ruff; both clean. (`from pyxirr import xirr` is untyped; if mypy reports `import-untyped`, add `# type: ignore[import-untyped]` on that import line only.)

- [ ] **Step 5: Commit**

```bash
git add portfolio_dash/portfolio/returns.py tests/portfolio/test_xirr.py
git commit -m "feat(portfolio): add reporting-currency XIRR via pyxirr" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 10: `sector_allocation` and `combined_view`

**Files:**
- Create: `portfolio_dash/portfolio/allocation.py`
- Test: `tests/portfolio/test_allocation.py`

- [ ] **Step 1: Write the failing test**

Create `tests/portfolio/test_allocation.py`:
```python
from decimal import Decimal

from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.portfolio.allocation import combined_view, sector_allocation
from portfolio_dash.portfolio.results import Holding

INSTR = {
    "AAPL": Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD, sector="Tech", name="Apple"),
    "JPM": Instrument(symbol="JPM", market=Market.US, quote_ccy=Currency.USD, sector="Financials", name="JPMorgan"),
}


def _spot(frm: Currency, to: Currency) -> Decimal:
    return Decimal("1") if frm is to else Decimal("32")


def _valued(symbol: str, ccy: Currency, value: str) -> Holding:
    v = Decimal(value)
    return Holding(
        account_id="a", symbol=symbol, quote_ccy=ccy, shares=Decimal("1"),
        original_avg=v, adjusted_avg=v, original_cost_total=v, adjusted_cost_total=v,
        dividend_portion=Decimal("0"), payback_ratio=Decimal("0"),
        market_price=v, market_value=v, unrealized_pnl=Decimal("0"),
        capital_gain=Decimal("0"), price_stale=False,
    )


def test_sector_allocation_weights() -> None:
    valued = [_valued("AAPL", Currency.USD, "300"), _valued("JPM", Currency.USD, "100")]
    sa = sector_allocation(valued, INSTR, _spot, Currency.USD)
    assert sa.by_sector["Tech"] == Decimal("300")
    assert sa.weights["Tech"] == Decimal("0.75")
    assert sa.weights["Financials"] == Decimal("0.25")


def test_combined_view_per_currency_and_reporting() -> None:
    valued = [_valued("AAPL", Currency.USD, "100")]
    cv = combined_view(valued, _spot, Currency.TWD)
    assert cv.by_currency_value[Currency.USD] == Decimal("100")
    assert cv.reporting_total_value == Decimal("3200")  # 100 * 32


def test_allocation_skips_stale_holdings() -> None:
    stale = _valued("AAPL", Currency.USD, "100").model_copy(
        update={"market_value": None, "price_stale": True}
    )
    sa = sector_allocation([stale], INSTR, _spot, Currency.USD)
    assert sa.by_sector == {}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/portfolio/test_allocation.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'portfolio_dash.portfolio.allocation'`.

- [ ] **Step 3: Implement**

Create `portfolio_dash/portfolio/allocation.py`:
```python
"""Sector allocation and combined multi-currency value views (reporting currency)."""

from collections import defaultdict
from collections.abc import Callable
from decimal import Decimal

from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.fx import convert
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.portfolio.results import CombinedView, Holding, SectorAllocation

_ZERO = Decimal("0")
FxRate = Callable[[Currency, Currency], Decimal]


def sector_allocation(
    valued_holdings: list[Holding],
    instruments: dict[str, Instrument],
    current_fx: FxRate,
    reporting: Currency,
) -> SectorAllocation:
    """Reporting-currency value and weight per sector. Stale (unpriced) holdings skipped."""
    by_sector: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    total = _ZERO
    for h in valued_holdings:
        if h.market_value is None:
            continue
        inst = instruments.get(h.symbol)
        if inst is None:
            raise KeyError(f"unknown instrument: {h.symbol}")
        value = convert(h.market_value, current_fx(h.quote_ccy, reporting))
        by_sector[inst.sector] += value
        total += value
    weights = {
        sector: (value / total if total != _ZERO else _ZERO)
        for sector, value in by_sector.items()
    }
    return SectorAllocation(
        by_sector=dict(by_sector), weights=weights, reporting_currency=reporting
    )


def combined_view(
    valued_holdings: list[Holding],
    current_fx: FxRate,
    reporting: Currency,
) -> CombinedView:
    """Per-currency market value plus a blended reporting-currency total."""
    by_ccy: dict[Currency, Decimal] = defaultdict(lambda: Decimal("0"))
    reporting_total = _ZERO
    for h in valued_holdings:
        if h.market_value is None:
            continue
        by_ccy[h.quote_ccy] += h.market_value
        reporting_total += convert(h.market_value, current_fx(h.quote_ccy, reporting))
    return CombinedView(
        by_currency_value=dict(by_ccy),
        reporting_total_value=reporting_total,
        reporting_currency=reporting,
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/portfolio/test_allocation.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add portfolio_dash/portfolio/allocation.py tests/portfolio/test_allocation.py
git commit -m "feat(portfolio): add sector_allocation and combined_view" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 11: Full verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full suite**

Run: `.\.venv\Scripts\python.exe -m pytest`
Expected: all pass (≈ 38 foundation + ~30 new = ~68 passed).

- [ ] **Step 2: mypy strict**

Run: `.\.venv\Scripts\python.exe -m mypy`
Expected: `Success: no issues found`.

- [ ] **Step 3: ruff**

Run: `.\.venv\Scripts\python.exe -m ruff check .`
Expected: `All checks passed!` (run `--fix` then re-check if import order is flagged).

- [ ] **Step 4: CHANGELOG integrity**

Run (via Bash tool / git-bash): `grep -c "^## \[v" CHANGELOG.md`
Expected: `1`.

- [ ] **Step 5: Confirm green, then stop for review.**

No commit needed (Task 1 already added the CHANGELOG entry). Report the final test count.

---

## Notes for the executor

- Run everything inside the activated `.venv` (or via `.\.venv\Scripts\python.exe`).
- TDD discipline: observe each test fail for the stated reason before implementing.
- **No floats for money.** All money literals are `Decimal("…")`. XIRR is the only place
  `float` appears, and only for the rate solve (the result is a rate, not money) — it is
  immediately converted back to `Decimal`.
- Keep test functions annotated (`-> None`) and fixtures typed for mypy strict.
- If `ruff check` reports import ordering, run `.\.venv\Scripts\python.exe -m ruff check --fix .`
  and re-verify before committing (first-party `portfolio_dash.portfolio.*` sorts before
  `portfolio_dash.shared.*`).
- `shared.fx.convert(amount, rate)` is called WITHOUT `to_currency` for aggregation, so
  full precision is preserved; quantization is a later (display) concern.
