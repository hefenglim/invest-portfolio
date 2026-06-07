# `forex/` FX P&L — Implementation Plan (sub-project ②)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `portfolio_dash/forex/` — per-account realized + unrealized FX (換匯) P&L over the whole foreign exposure (cash + stocks), as an attribution decomposition of ①'s reporting return (never additive), with a reporting-currency rollup.

**Architecture:** Pure functions over the shared ledgers plus a passed-in per-account foreign stock value (from ①). Each FX-exposed account gets a foreign-currency pool: weighted-avg acquisition rate from its home→foreign conversions, a reconstructed foreign cash balance, realized FX on reconversions, and unrealized FX (stocks + cash) marked to current spot. No `portfolio/` import; money is `Decimal`; FX via `shared.fx.convert`.

**Tech Stack:** Python 3.12, pydantic v2, `decimal`, pytest, mypy strict, ruff. Builds on `shared/` and ① `portfolio/`.

**Spec:** `docs/superpowers/specs/2026-06-07-forex-fx-pnl-design.md`

---

## File Structure

| File | Responsibility |
| --- | --- |
| `portfolio_dash/forex/__init__.py` | package marker |
| `portfolio_dash/forex/results.py` | `AccountFXResult`, `FXSummary` |
| `portfolio_dash/forex/pools.py` | `average_acquisition_rate`, `foreign_cash_balance` |
| `portfolio_dash/forex/fx_pnl.py` | `compute_account_fx`, `compute_fx_summary` |
| `tests/forex/…` | tests |
| `CHANGELOG.md` | `[Unreleased]` Added note |

All tooling via the venv interpreter: `.\.venv\Scripts\python.exe -m pytest`, `... -m mypy`, `... -m ruff check .`. Work on branch `feat/forex-fx-pnl`. Commit trailers end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Run `ruff check --fix .` if import order is flagged (first-party `portfolio_dash.forex.*` sorts before `portfolio_dash.shared.*`).

---

## Task 1: Result models

**Files:**
- Create: `portfolio_dash/forex/__init__.py`
- Create: `portfolio_dash/forex/results.py`
- Test: `tests/forex/__init__.py`, `tests/forex/test_results.py`

- [ ] **Step 1: Write the failing test**

Create `tests/forex/__init__.py` (empty) and `tests/forex/test_results.py`:
```python
from decimal import Decimal

from portfolio_dash.shared.enums import Currency
from portfolio_dash.forex.results import AccountFXResult, FXSummary


def test_account_fx_result_optional_fields() -> None:
    r = AccountFXResult(
        account_id="schwab",
        home_ccy=Currency.TWD,
        foreign_ccy=Currency.USD,
        avg_rate=None,
        current_spot=None,
        foreign_cash=Decimal("0"),
        foreign_stock_value=Decimal("0"),
        realized_fx=None,
        unrealized_fx_stocks=None,
        unrealized_fx_cash=None,
    )
    assert r.avg_rate is None
    assert r.realized_fx is None


def test_fx_summary_holds_accounts() -> None:
    r = AccountFXResult(
        account_id="schwab",
        home_ccy=Currency.TWD,
        foreign_ccy=Currency.USD,
        avg_rate=Decimal("32"),
        current_spot=Decimal("33"),
        foreign_cash=Decimal("1000"),
        foreign_stock_value=Decimal("10800"),
        realized_fx=Decimal("0"),
        unrealized_fx_stocks=Decimal("10800"),
        unrealized_fx_cash=Decimal("1000"),
    )
    s = FXSummary(
        by_account={"schwab": r},
        reporting_currency=Currency.TWD,
        reporting_realized_fx=Decimal("0"),
        reporting_unrealized_fx=Decimal("11800"),
    )
    assert s.by_account["schwab"].unrealized_fx_stocks == Decimal("10800")
    assert s.reporting_unrealized_fx == Decimal("11800")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/forex/test_results.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'portfolio_dash.forex'`.

- [ ] **Step 3: Implement**

Create `portfolio_dash/forex/__init__.py`:
```python
"""forex — currency-exchange ledger and realized/unrealized FX P&L (attribution)."""
```

Create `portfolio_dash/forex/results.py`:
```python
"""Computed FX (換匯) P&L result models."""

from decimal import Decimal

from pydantic import BaseModel

from portfolio_dash.shared.enums import Currency


class AccountFXResult(BaseModel):
    """Per-account FX P&L. Money figures (realized/unrealized) are in ``home_ccy``;
    ``foreign_cash`` and ``foreign_stock_value`` are in ``foreign_ccy``."""

    account_id: str
    home_ccy: Currency
    foreign_ccy: Currency
    avg_rate: Decimal | None
    current_spot: Decimal | None
    foreign_cash: Decimal
    foreign_stock_value: Decimal
    realized_fx: Decimal | None
    unrealized_fx_stocks: Decimal | None
    unrealized_fx_cash: Decimal | None


class FXSummary(BaseModel):
    """All per-account results plus a reporting-currency rollup."""

    by_account: dict[str, AccountFXResult]
    reporting_currency: Currency
    reporting_realized_fx: Decimal
    reporting_unrealized_fx: Decimal
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/forex/test_results.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add portfolio_dash/forex/__init__.py portfolio_dash/forex/results.py tests/forex/__init__.py tests/forex/test_results.py
git commit -m "feat(forex): add FX P&L result models" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Pool primitives — average rate & foreign cash

**Files:**
- Create: `portfolio_dash/forex/pools.py`
- Test: `tests/forex/test_pools.py`

- [ ] **Step 1: Write the failing test**

Create `tests/forex/test_pools.py`:
```python
from datetime import date
from decimal import Decimal

from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import Dividend, FXConversion, Transaction
from portfolio_dash.forex.pools import average_acquisition_rate, foreign_cash_balance

AAPL = Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                  sector="Tech", name="Apple")
INSTR = {"AAPL": AAPL}


def _conv(frm: Currency, famt: str, to: Currency, tamt: str, d: date) -> FXConversion:
    return FXConversion(account_id="schwab", date=d, from_ccy=frm, from_amount=Decimal(famt),
                        to_ccy=to, to_amount=Decimal(tamt))


def test_average_acquisition_rate_weighted() -> None:
    convs = [_conv(Currency.TWD, "320000", Currency.USD, "10000", date(2025, 1, 1)),
             _conv(Currency.TWD, "330000", Currency.USD, "10000", date(2025, 2, 1))]
    # (320000 + 330000) / (10000 + 10000) = 32.5
    assert average_acquisition_rate(convs, Currency.TWD, Currency.USD) == Decimal("32.5")


def test_average_acquisition_rate_none_when_no_conversions() -> None:
    assert average_acquisition_rate([], Currency.TWD, Currency.USD) is None


def test_foreign_cash_balance_reconstruction() -> None:
    convs = [_conv(Currency.TWD, "320000", Currency.USD, "10000", date(2025, 1, 1)),
             _conv(Currency.USD, "1000", Currency.TWD, "33000", date(2025, 6, 1))]  # reconvert out
    txs = [
        Transaction(account_id="schwab", symbol="AAPL", side=Side.BUY, quantity=Decimal("90"),
                    price=Decimal("100"), fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2025, 1, 2)),
        Transaction(account_id="schwab", symbol="AAPL", side=Side.SELL, quantity=Decimal("10"),
                    price=Decimal("110"), fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2025, 5, 1)),
    ]
    divs = [Dividend(account_id="schwab", symbol="AAPL", date=date(2025, 3, 1),
                     type=DividendType.CASH, gross=Decimal("50"), withholding=Decimal("0"),
                     net=Decimal("50"))]
    # +10000 (conv in) -9000 (buy) +50 (div) +1100 (sell) -1000 (reconvert) = 1150
    assert foreign_cash_balance(txs, divs, convs, INSTR, Currency.USD) == Decimal("1150")


def test_foreign_cash_ignores_drip_dividends() -> None:
    convs = [_conv(Currency.TWD, "320000", Currency.USD, "10000", date(2025, 1, 1))]
    divs = [Dividend(account_id="schwab", symbol="AAPL", date=date(2025, 3, 1),
                     type=DividendType.DRIP, gross=Decimal("100"), withholding=Decimal("30"),
                     net=Decimal("70"), reinvest_shares=Decimal("0.5"), reinvest_price=Decimal("140"))]
    # DRIP touches no cash (paid then reinvested) -> balance is just the conversion
    assert foreign_cash_balance([], divs, convs, INSTR, Currency.USD) == Decimal("10000")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/forex/test_pools.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'portfolio_dash.forex.pools'`.

- [ ] **Step 3: Implement**

Create `portfolio_dash/forex/pools.py`:
```python
"""Per-account FX pool: weighted-avg acquisition rate and foreign cash reconstruction.

Inputs are already scoped to a single account (the caller filters by account_id).
"""

from decimal import Decimal

from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import Dividend, FXConversion, Transaction

_ZERO = Decimal("0")


def average_acquisition_rate(
    conversions: list[FXConversion], home: Currency, foreign: Currency
) -> Decimal | None:
    """Weighted-average home-per-foreign rate over home->foreign conversions.

    Returns None if the account has no such conversions (no FX cost basis).
    """
    total_home = _ZERO
    total_foreign = _ZERO
    for c in conversions:
        if c.from_ccy == home and c.to_ccy == foreign:
            total_home += c.from_amount
            total_foreign += c.to_amount
    if total_foreign == _ZERO:
        return None
    return total_home / total_foreign


def foreign_cash_balance(
    transactions: list[Transaction],
    dividends: list[Dividend],
    conversions: list[FXConversion],
    instruments: dict[str, Instrument],
    foreign: Currency,
) -> Decimal:
    """Reconstruct the foreign-currency cash balance from the account's ledgers.

    + conversions into foreign, + foreign sale net proceeds, + foreign CASH dividends net,
    - foreign buys (incl. fees+tax), - reconversions out of foreign. DRIP/STOCK dividends
    move no cash (DRIP nets to zero) and are excluded.
    """
    cash = _ZERO
    for c in conversions:
        if c.to_ccy == foreign:
            cash += c.to_amount
        if c.from_ccy == foreign:
            cash -= c.from_amount
    for t in transactions:
        if instruments[t.symbol].quote_ccy != foreign:
            continue
        if t.side is Side.BUY:
            cash -= t.quantity * t.price + t.fees + t.tax
        else:
            cash += t.quantity * t.price - t.fees - t.tax
    for d in dividends:
        if d.type is DividendType.CASH and instruments[d.symbol].quote_ccy == foreign:
            cash += d.net
    return cash
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/forex/test_pools.py -v`
Expected: PASS (4 passed). Then `... -m mypy` clean, `... -m ruff check .` clean.

- [ ] **Step 5: Commit**

```bash
git add portfolio_dash/forex/pools.py tests/forex/test_pools.py
git commit -m "feat(forex): add pool average rate and foreign cash reconstruction" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: `compute_account_fx` — per-account FX P&L

**Files:**
- Create: `portfolio_dash/forex/fx_pnl.py`
- Test: `tests/forex/test_fx_pnl.py`

- [ ] **Step 1: Write the failing test**

Create `tests/forex/test_fx_pnl.py`:
```python
from datetime import date
from decimal import Decimal

from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Account, Instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.models.ledger import FXConversion, Transaction
from portfolio_dash.forex.fx_pnl import compute_account_fx

SCHWAB = Account(account_id="schwab", name="Schwab", broker="Schwab",
                 settlement_ccy=Currency.USD, funding_ccy=Currency.TWD)
AAPL = Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                  sector="Tech", name="Apple")
INSTR = {"AAPL": AAPL}


def _buy(qty: str, price: str, d: date) -> Transaction:
    return Transaction(account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal(qty), price=Decimal(price), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=d)


def test_compute_account_fx_unrealized_split() -> None:
    convs = [FXConversion(account_id="schwab", date=date(2025, 1, 1), from_ccy=Currency.TWD,
                          from_amount=Decimal("320000"), to_ccy=Currency.USD,
                          to_amount=Decimal("10000"))]
    txs = [_buy("90", "100", date(2025, 1, 2))]  # spends 9000 USD -> cash 1000
    r = compute_account_fx(SCHWAB, Currency.USD, Decimal("10800"), txs, [], convs, INSTR,
                           spot=Decimal("33"))
    assert r.avg_rate == Decimal("32")
    assert r.foreign_cash == Decimal("1000")
    assert r.realized_fx == Decimal("0")            # no reconversion
    assert r.unrealized_fx_stocks == Decimal("10800")  # 10800 * (33-32)
    assert r.unrealized_fx_cash == Decimal("1000")     # 1000 * (33-32)


def test_compute_account_fx_realized_on_reconversion() -> None:
    convs = [
        FXConversion(account_id="schwab", date=date(2025, 1, 1), from_ccy=Currency.TWD,
                     from_amount=Decimal("320000"), to_ccy=Currency.USD, to_amount=Decimal("10000")),
        FXConversion(account_id="schwab", date=date(2025, 6, 1), from_ccy=Currency.USD,
                     from_amount=Decimal("5000"), to_ccy=Currency.TWD, to_amount=Decimal("167500")),
    ]
    r = compute_account_fx(SCHWAB, Currency.USD, Decimal("0"), [], [], convs, INSTR,
                           spot=Decimal("33"))
    # realized = 167500 - 5000 * 32 = 167500 - 160000 = 7500
    assert r.realized_fx == Decimal("7500")


def test_compute_account_fx_no_conversions_all_none() -> None:
    r = compute_account_fx(SCHWAB, Currency.USD, Decimal("1000"), [], [], [], INSTR,
                           spot=Decimal("33"))
    assert r.avg_rate is None
    assert r.realized_fx is None
    assert r.unrealized_fx_stocks is None
    assert r.unrealized_fx_cash is None


def test_compute_account_fx_missing_spot_unrealized_none_realized_ok() -> None:
    convs = [FXConversion(account_id="schwab", date=date(2025, 1, 1), from_ccy=Currency.TWD,
                          from_amount=Decimal("320000"), to_ccy=Currency.USD,
                          to_amount=Decimal("10000"))]
    r = compute_account_fx(SCHWAB, Currency.USD, Decimal("10000"), [], [], convs, INSTR,
                           spot=None)
    assert r.realized_fx == Decimal("0")
    assert r.unrealized_fx_stocks is None
    assert r.unrealized_fx_cash is None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/forex/test_fx_pnl.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'portfolio_dash.forex.fx_pnl'`.

- [ ] **Step 3: Implement**

Create `portfolio_dash/forex/fx_pnl.py`:
```python
"""Realized + unrealized FX P&L per account, and the reporting-currency rollup."""

from collections.abc import Callable
from decimal import Decimal

from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.fx import convert
from portfolio_dash.shared.models.assets import Account, Instrument
from portfolio_dash.shared.models.ledger import Dividend, FXConversion, Transaction
from portfolio_dash.forex.pools import average_acquisition_rate, foreign_cash_balance
from portfolio_dash.forex.results import AccountFXResult, FXSummary

_ZERO = Decimal("0")
SpotRate = Callable[[Currency, Currency], Decimal]


def _realized_fx(
    conversions: list[FXConversion], home: Currency, foreign: Currency, avg_rate: Decimal | None
) -> Decimal | None:
    if avg_rate is None:
        return None
    total = _ZERO
    for c in conversions:
        if c.from_ccy == foreign and c.to_ccy == home:
            total += c.to_amount - c.from_amount * avg_rate
    return total


def compute_account_fx(
    account: Account,
    foreign: Currency,
    foreign_stock_value: Decimal,
    transactions: list[Transaction],
    dividends: list[Dividend],
    conversions: list[FXConversion],
    instruments: dict[str, Instrument],
    spot: Decimal | None,
) -> AccountFXResult:
    """FX P&L for one account (ledgers already scoped to it). ``foreign_stock_value`` and
    ``spot`` are supplied by the caller (foreign market value from ①; spot foreign->home).
    """
    home = account.funding_ccy
    avg_rate = average_acquisition_rate(conversions, home, foreign)
    foreign_cash = foreign_cash_balance(transactions, dividends, conversions, instruments, foreign)
    realized = _realized_fx(conversions, home, foreign, avg_rate)
    if avg_rate is None or spot is None:
        unreal_stocks: Decimal | None = None
        unreal_cash: Decimal | None = None
    else:
        unreal_stocks = foreign_stock_value * (spot - avg_rate)
        unreal_cash = foreign_cash * (spot - avg_rate)
    return AccountFXResult(
        account_id=account.account_id,
        home_ccy=home,
        foreign_ccy=foreign,
        avg_rate=avg_rate,
        current_spot=spot,
        foreign_cash=foreign_cash,
        foreign_stock_value=foreign_stock_value,
        realized_fx=realized,
        unrealized_fx_stocks=unreal_stocks,
        unrealized_fx_cash=unreal_cash,
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/forex/test_fx_pnl.py -v`
Expected: PASS (4 passed). Then `... -m mypy` clean, `... -m ruff check .` clean.

- [ ] **Step 5: Commit**

```bash
git add portfolio_dash/forex/fx_pnl.py tests/forex/test_fx_pnl.py
git commit -m "feat(forex): add per-account FX P&L (realized + unrealized split)" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: `compute_fx_summary` — rollup & decomposition

**Files:**
- Modify: `portfolio_dash/forex/fx_pnl.py`
- Test: `tests/forex/test_fx_summary.py`

- [ ] **Step 1: Write the failing test**

Create `tests/forex/test_fx_summary.py`:
```python
from datetime import date
from decimal import Decimal

from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Account, Instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.models.ledger import FXConversion, Transaction
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.portfolio.pnl import value_holdings
from portfolio_dash.portfolio.returns import total_return
from portfolio_dash.forex.fx_pnl import compute_fx_summary

SCHWAB = Account(account_id="schwab", name="Schwab", broker="Schwab",
                 settlement_ccy=Currency.USD, funding_ccy=Currency.TWD)
AAPL = Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                  sector="Tech", name="Apple")
INSTR = {"AAPL": AAPL}
ACCTS = {"schwab": SCHWAB}


def _spot(frm: Currency, to: Currency) -> Decimal:
    if frm is to:
        return Decimal("1")
    rates = {(Currency.USD, Currency.TWD): Decimal("33")}
    return rates[(frm, to)]


def test_fx_summary_rollup_and_worked_example() -> None:
    convs = [FXConversion(account_id="schwab", date=date(2025, 1, 1), from_ccy=Currency.TWD,
                          from_amount=Decimal("320000"), to_ccy=Currency.USD,
                          to_amount=Decimal("10000"))]
    txs = [Transaction(account_id="schwab", symbol="AAPL", side=Side.BUY, quantity=Decimal("90"),
                       price=Decimal("100"), fees=Decimal("0"), tax=Decimal("0"),
                       trade_date=date(2025, 1, 2))]
    foreign_exposure = {"schwab": (Currency.USD, Decimal("10800"))}  # 90 sh @ 120
    summary = compute_fx_summary(ACCTS, INSTR, txs, [], convs, foreign_exposure, _spot, Currency.TWD)
    r = summary.by_account["schwab"]
    assert r.unrealized_fx_stocks == Decimal("10800")
    assert r.unrealized_fx_cash == Decimal("1000")
    assert r.realized_fx == Decimal("0")
    # reporting rollup (home TWD == reporting TWD -> rate 1)
    assert summary.reporting_unrealized_fx == Decimal("11800")
    assert summary.reporting_realized_fx == Decimal("0")


def test_decomposition_identity_no_double_count() -> None:
    # Same fixture run through (1); assert asset + total_FX == grand_total, asset = (1) - stock_FX.
    convs = [FXConversion(account_id="schwab", date=date(2025, 1, 1), from_ccy=Currency.TWD,
                          from_amount=Decimal("320000"), to_ccy=Currency.USD,
                          to_amount=Decimal("10000"))]
    txs = [Transaction(account_id="schwab", symbol="AAPL", side=Side.BUY, quantity=Decimal("90"),
                       price=Decimal("100"), fees=Decimal("0"), tax=Decimal("0"),
                       trade_date=date(2025, 1, 2))]
    book = build_book(txs, [], [], INSTR)
    valued = value_holdings(book.holdings, {"AAPL": Decimal("120")})
    rs = total_return(book, valued, _spot, Currency.TWD)
    one_total = rs.reporting_total_return  # USD unrealized 90*(120-100)=1800 -> *33 = 59400 TWD

    foreign_exposure = {"schwab": (Currency.USD, Decimal("10800"))}
    summary = compute_fx_summary(ACCTS, INSTR, txs, [], convs, foreign_exposure, _spot, Currency.TWD)
    stock_fx = summary.reporting_unrealized_fx - (
        summary.by_account["schwab"].unrealized_fx_cash or Decimal("0")
    )  # stock portion only
    cash_fx = (summary.by_account["schwab"].unrealized_fx_cash or Decimal("0")) + summary.reporting_realized_fx

    asset = one_total - stock_fx
    total_fx = stock_fx + cash_fx
    grand_total = one_total + cash_fx
    assert asset + total_fx == grand_total
    assert asset == one_total - stock_fx
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/forex/test_fx_summary.py -v`
Expected: FAIL — `ImportError: cannot import name 'compute_fx_summary' from 'portfolio_dash.forex.fx_pnl'`.

- [ ] **Step 3: Implement (append to `fx_pnl.py`)**

Append to `portfolio_dash/forex/fx_pnl.py`:
```python
def compute_fx_summary(
    accounts: dict[str, Account],
    instruments: dict[str, Instrument],
    transactions: list[Transaction],
    dividends: list[Dividend],
    fx_conversions: list[FXConversion],
    foreign_exposure: dict[str, tuple[Currency, Decimal]],
    current_spot: SpotRate,
    reporting: Currency,
) -> FXSummary:
    """FX P&L for every FX-exposed account + reporting rollup.

    ``foreign_exposure`` maps account_id -> (foreign_ccy, foreign stock market value in
    that foreign ccy), supplied by the orchestrator from ①'s valued holdings. Only the
    accounts present in ``foreign_exposure`` are processed.
    """
    by_account: dict[str, AccountFXResult] = {}
    rep_realized = _ZERO
    rep_unrealized = _ZERO
    for account_id, (foreign, stock_value) in foreign_exposure.items():
        account = accounts[account_id]
        home = account.funding_ccy
        txs = [t for t in transactions if t.account_id == account_id]
        divs = [d for d in dividends if d.account_id == account_id]
        convs = [c for c in fx_conversions if c.account_id == account_id]
        try:
            spot: Decimal | None = current_spot(foreign, home)
        except KeyError:
            spot = None
        result = compute_account_fx(
            account, foreign, stock_value, txs, divs, convs, instruments, spot
        )
        by_account[account_id] = result
        to_reporting = current_spot(home, reporting)
        if result.realized_fx is not None:
            rep_realized += convert(result.realized_fx, to_reporting)
        if result.unrealized_fx_stocks is not None and result.unrealized_fx_cash is not None:
            rep_unrealized += convert(
                result.unrealized_fx_stocks + result.unrealized_fx_cash, to_reporting
            )
    return FXSummary(
        by_account=by_account,
        reporting_currency=reporting,
        reporting_realized_fx=rep_realized,
        reporting_unrealized_fx=rep_unrealized,
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/forex/test_fx_summary.py -v`
Expected: PASS (2 passed). Then `... -m mypy` clean, `... -m ruff check .` clean.

- [ ] **Step 5: Commit**

```bash
git add portfolio_dash/forex/fx_pnl.py tests/forex/test_fx_summary.py
git commit -m "feat(forex): add compute_fx_summary rollup + decomposition identity test" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Full verification & CHANGELOG

**Files:**
- Modify: `CHANGELOG.md` (`[Unreleased]`)

- [ ] **Step 1: Run the full suite**

Run: `.\.venv\Scripts\python.exe -m pytest`
Expected: all pass (≈ 88 prior + ~12 new = ~100 passed).

- [ ] **Step 2: mypy strict**

Run: `.\.venv\Scripts\python.exe -m mypy`
Expected: `Success: no issues found`.

- [ ] **Step 3: ruff**

Run: `.\.venv\Scripts\python.exe -m ruff check .`
Expected: `All checks passed!` (run `--fix` then re-check if import order flagged).

- [ ] **Step 4: Update CHANGELOG `[Unreleased]`**

Add this bullet to the existing `### Added` list under `## [Unreleased]` (do NOT remove existing bullets; insert after the `portfolio/` calc-core bullet):
```markdown
- `forex/` FX (換匯) P&L: per-account foreign-currency pool (weighted-avg acquisition
  rate from home→foreign conversions), reconstructed foreign cash balance, realized FX on
  reconversions, unrealized FX (stocks + cash) marked to spot; reporting-currency
  `FXSummary` rollup. Presented as an attribution decomposition of the portfolio return
  (asset + FX), never additive.
```

- [ ] **Step 5: Verify CHANGELOG integrity**

Run (via Bash tool / git-bash): `grep -c "^## \[v" CHANGELOG.md`
Expected: `1`.

- [ ] **Step 6: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs: record forex/ FX P&L in CHANGELOG [Unreleased]" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Notes for the executor

- Run everything inside the venv (`.\.venv\Scripts\python.exe ...`).
- TDD: observe each test fail for the stated reason before implementing.
- **No floats for money** — `Decimal("…")` only. `shared.fx.convert(amount, rate)` is
  called WITHOUT `to_currency` (full precision; quantize is a display concern).
- `forex/` imports only `shared/*` and `forex/*` — never `portfolio/` (the foreign stock
  value is passed in). The decomposition vs ① is assembled by the consumer (web layer),
  not here.
- Keep test functions annotated (`-> None`) and fixtures typed for mypy strict.
