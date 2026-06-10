# Dashboard Combiner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One entry point `portfolio.dashboard.build_dashboard(conn, *, now, reporting)` that reads SQLite (ledgers + prices + FX), calls the existing calculation cores, and returns one complete `DashboardData` Pydantic model — the contract `web_ui` (and later `llm_insight`) binds to.

**Architecture:** The combiner lives in `portfolio/` (calculation core top). New one-way edge `portfolio → forex` (forex imports only `shared`; no cycle). Three new files in `portfolio/` (`dashboard_models.py`, `timeseries.py`, `dashboard.py`) plus two small read helpers in `pricing/store.py` and one in `data_ingestion/store.py`. Spec: `docs/superpowers/specs/2026-06-10-dashboard-combiner-design.md`.

**Tech Stack:** Python 3.12, Pydantic v2, sqlite3, Decimal (never float for money), pytest, mypy --strict, ruff.

**CRITICAL — interpreter:** All gates MUST run via the repo venv: `./.venv/Scripts/python.exe -m pytest|mypy|ruff` (bash) — bare `python` resolves to a deps-less interpreter and produces spurious ModuleNotFoundError/missing-stub errors.

**Branch:** work on `feat/dashboard-combiner` (already created; spec committed).

---

## Existing API surface you will call (verified signatures — do not re-derive)

- `portfolio_dash.portfolio.cost_basis.build_book(transactions, dividends, opening, instruments) -> Book` — `Book.holdings: list[Holding]`, `Book.realized: RealizedPnL`, `Book.gross_invested: dict[Currency, Decimal]`.
- `portfolio_dash.portfolio.pnl.value_holdings(holdings, price_map: dict[str, Decimal]) -> list[Holding]` — missing price → market fields `None` + `price_stale=True`.
- `portfolio_dash.portfolio.returns.total_return(book, valued_holdings, current_fx, reporting) -> ReturnSummary` — `current_fx: Callable[[Currency, Currency], Decimal]`, may raise `KeyError` through the callable.
- `portfolio_dash.portfolio.returns.xirr_reporting(transactions, dividends, opening, holdings, instruments, fx_at, current_prices, current_fx, as_of, reporting) -> Decimal | None` — `fx_at: Callable[[date, Currency, Currency], Decimal]`; returns `None` on missing held price / no sign change / non-convergence; `KeyError` from `fx_at` propagates.
- `portfolio_dash.portfolio.allocation.sector_allocation(valued_holdings, instruments, current_fx, reporting) -> SectorAllocation`; `combined_view(valued_holdings, current_fx, reporting) -> CombinedView`.
- `portfolio_dash.forex.fx_pnl.compute_fx_summary(accounts, instruments, transactions, dividends, fx_conversions, foreign_exposure, current_spot, reporting) -> FXSummary` — `foreign_exposure: dict[str, tuple[Currency, Decimal]]`; foreign→home spot `KeyError` is caught internally (degrades), but the home→reporting rate is called **unconditionally per exposed account** and may raise `KeyError` out.
- `portfolio_dash.pricing.store`: `get_latest_price(conn, instrument, *, now, max_age_days=4) -> PriceRead | None` (`PriceRead(value, as_of, source, stale)`); `get_price_history(conn, instrument, start, end) -> list[PriceRead]`; `get_fx(conn, base, quote, *, now, max_age_days=4) -> FxRead | None`; `get_dividend_events(conn, instrument) -> list[DividendEvent]` (`DividendEvent(instrument, market, ex_date, pay_date, cash_amount, stock_amount, currency, source)`); `upsert_prices(conn, rows, *, fetched_at)`, `upsert_fx(...)`, `upsert_dividend_events(...)` for test seeding.
- `portfolio_dash.data_ingestion.store`: `list_transactions(conn) -> list[StoredTransaction]` (fields: `id, account_id, symbol, side: Side, quantity, price, fees, tax, trade_date, fee_rule_snapshot, note`); `list_dividends(conn) -> list[StoredDividend]` (`type` is **str**); `list_fx_conversions(conn) -> list[StoredFxConversion]` (`from_ccy/to_ccy` are **Currency**); `list_opening(conn) -> list[StoredOpening]`; `list_instruments(conn) -> list[Instrument]`; `upsert_instrument`, `insert_transaction`, `insert_dividend`, `insert_fx_conversion`, `upsert_opening` for test seeding.
- `portfolio_dash.data_ingestion.config_seed.seed_accounts(conn)` + `DEFAULT_ACCOUNTS` (ids: `tw_broker`, `schwab`, `moomoo_my_us`, `moomoo_my_my`).
- `portfolio_dash.shared.fx.convert(amount, rate, *, to_currency=None)` — full precision unless `to_currency` given.
- `portfolio_dash.shared.models.ledger`: `Transaction(account_id, symbol, side, quantity, price, fees, tax, trade_date)`, `Dividend(account_id, symbol, date, type: DividendType, gross, withholding, net, reinvest_shares=None, reinvest_price=None)`, `FXConversion(account_id, date, from_ccy, from_amount, to_ccy, to_amount)`, `OpeningInventory(account_id, symbol, shares, original_avg_cost, original_cost_total, build_date)`.
- `portfolio_dash.shared.models.assets`: `Account(account_id, name, broker, settlement_ccy, funding_ccy)`, `Instrument(symbol, market, quote_ccy, sector, name, board="")`.
- Enums: `Currency.TWD/USD/MYR`, `Market.US/TW/MY`, `Side.BUY/SELL`, `DividendType.CASH/STOCK/DRIP`.
- DB fixtures convention: in-memory sqlite3 with `row_factory = sqlite3.Row`; ledger tables via `portfolio_dash.bootstrap.bootstrap_db(conn)`; pricing tables via `portfolio_dash.pricing.schema.create_tables(conn)`.

---

### Task 1: FX point-in-time + history reads (`pricing/store.py`)

**Files:**
- Modify: `portfolio_dash/pricing/store.py` (append after `get_fx`)
- Test: `tests/pricing/test_store.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/pricing/test_store.py` (extend the existing `from portfolio_dash.pricing.store import (...)` block with `get_fx_history, get_fx_on`):

```python
def _fx(rate: str, d: date) -> FxRow:
    return FxRow(base=Currency.USD, quote=Currency.TWD, as_of=d,
                 rate=Decimal(rate), source="test")


def test_get_fx_on_exact_and_carry_forward(conn: sqlite3.Connection) -> None:
    upsert_fx(conn, [_fx("32.1", date(2026, 6, 1)), _fx("32.5", date(2026, 6, 5))],
              fetched_at=_NOW)
    exact = get_fx_on(conn, Currency.USD, Currency.TWD, on=date(2026, 6, 5))
    assert exact is not None and exact.rate == Decimal("32.5")
    carry = get_fx_on(conn, Currency.USD, Currency.TWD, on=date(2026, 6, 3))
    assert carry is not None and carry.rate == Decimal("32.1")
    assert carry.as_of == date(2026, 6, 1) and carry.stale is False


def test_get_fx_on_none_before_first_rate(conn: sqlite3.Connection) -> None:
    upsert_fx(conn, [_fx("32.1", date(2026, 6, 1))], fetched_at=_NOW)
    assert get_fx_on(conn, Currency.USD, Currency.TWD, on=date(2026, 5, 31)) is None
    assert get_fx_on(conn, Currency.MYR, Currency.TWD, on=date(2026, 6, 5)) is None


def test_get_fx_history_bounds_and_order(conn: sqlite3.Connection) -> None:
    upsert_fx(conn, [_fx("32.1", date(2026, 6, 1)), _fx("32.3", date(2026, 6, 3)),
                     _fx("32.5", date(2026, 6, 5))], fetched_at=_NOW)
    rows = get_fx_history(conn, Currency.USD, Currency.TWD,
                          date(2026, 6, 1), date(2026, 6, 3))
    assert [r.rate for r in rows] == [Decimal("32.1"), Decimal("32.3")]
    assert [r.as_of for r in rows] == [date(2026, 6, 1), date(2026, 6, 3)]
    assert all(r.stale is False for r in rows)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/pricing/test_store.py -v`
Expected: FAIL — `ImportError: cannot import name 'get_fx_on'`

- [ ] **Step 3: Implement**

Append to `portfolio_dash/pricing/store.py` (after `get_fx`):

```python
def get_fx_on(
    conn: sqlite3.Connection, base: Currency, quote: Currency, *, on: date,
) -> FxRead | None:
    """Return the most recent stored rate with ``as_of_date <= on``, or ``None``.

    Point-in-time read for trade-date conversion: never a later rate ("never guess
    backwards"). ``stale`` is always False — staleness is a latest-quote concern
    (same convention as ``get_price_history``).
    """
    row = conn.execute(
        "SELECT rate, as_of_date, source FROM fx_rates WHERE base=? AND quote=? "
        "AND as_of_date<=? ORDER BY as_of_date DESC LIMIT 1",
        (base.value, quote.value, on.isoformat()),
    ).fetchone()
    if row is None:
        return None
    return FxRead(rate=from_db(row["rate"]), as_of=date.fromisoformat(row["as_of_date"]),
                  source=row["source"], stale=False)


def get_fx_history(
    conn: sqlite3.Connection, base: Currency, quote: Currency, start: date, end: date,
) -> list[FxRead]:
    """Return stored FX rates for ``base``/``quote`` within ``[start, end]``, ascending."""
    rows = conn.execute(
        "SELECT rate, as_of_date, source FROM fx_rates WHERE base=? AND quote=? "
        "AND as_of_date BETWEEN ? AND ? ORDER BY as_of_date ASC",
        (base.value, quote.value, start.isoformat(), end.isoformat()),
    ).fetchall()
    return [
        FxRead(rate=from_db(r["rate"]), as_of=date.fromisoformat(r["as_of_date"]),
               source=r["source"], stale=False)
        for r in rows
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/pricing/test_store.py -v`
Expected: all PASS

- [ ] **Step 5: Gates + commit**

```bash
./.venv/Scripts/python.exe -m mypy portfolio_dash --strict
./.venv/Scripts/python.exe -m ruff check portfolio_dash tests
git add portfolio_dash/pricing/store.py tests/pricing/test_store.py
git commit -m "feat(pricing): get_fx_on (on-or-before) + get_fx_history reads"
```

---

### Task 2: `list_accounts` read (`data_ingestion/store.py`)

**Files:**
- Modify: `portfolio_dash/data_ingestion/store.py` (extend the assets import; append after `list_instruments`)
- Test: `tests/data_ingestion/test_accounts.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/data_ingestion/test_accounts.py` (the shared `conn` fixture in `tests/data_ingestion/conftest.py` already runs `bootstrap_db`, which creates the `accounts` table):

```python
import sqlite3

from portfolio_dash.data_ingestion.config_seed import DEFAULT_ACCOUNTS, seed_accounts
from portfolio_dash.data_ingestion.store import list_accounts
from portfolio_dash.shared.enums import Currency


def test_list_accounts_empty(conn: sqlite3.Connection) -> None:
    assert list_accounts(conn) == []


def test_list_accounts_round_trips_seed(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    accounts = list_accounts(conn)
    assert {a.account_id for a in accounts} == {ac.account_id for ac in DEFAULT_ACCOUNTS}
    schwab = next(a for a in accounts if a.account_id == "schwab")
    assert schwab.name == "Charles Schwab"
    assert schwab.broker == "Schwab"
    assert schwab.settlement_ccy is Currency.USD
    assert schwab.funding_ccy is Currency.TWD
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/data_ingestion/test_accounts.py -v`
Expected: FAIL — `ImportError: cannot import name 'list_accounts'`

- [ ] **Step 3: Implement**

In `portfolio_dash/data_ingestion/store.py`: change the assets import to `from portfolio_dash.shared.models.assets import Account, Instrument`, then append after `list_instruments`:

```python
def list_accounts(conn: sqlite3.Connection) -> list[Account]:
    """Return all broker accounts (seeded by ``config_seed.seed_accounts``)."""
    rows = conn.execute(
        "SELECT account_id, name, broker, settlement_ccy, funding_ccy "
        "FROM accounts ORDER BY account_id"
    ).fetchall()
    return [
        Account(
            account_id=r["account_id"], name=r["name"], broker=r["broker"],
            settlement_ccy=Currency(r["settlement_ccy"]),
            funding_ccy=Currency(r["funding_ccy"]),
        )
        for r in rows
    ]
```

(`Currency` is already imported in this module; verify, and add the import if not.)

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/data_ingestion/test_accounts.py -v`
Expected: 2 PASS

- [ ] **Step 5: Gates + commit**

```bash
./.venv/Scripts/python.exe -m mypy portfolio_dash --strict
./.venv/Scripts/python.exe -m ruff check portfolio_dash tests
git add portfolio_dash/data_ingestion/store.py tests/data_ingestion/test_accounts.py
git commit -m "feat(data_ingestion): list_accounts read API"
```

---

### Task 3: Contract models (`portfolio/dashboard_models.py`)

**Files:**
- Create: `portfolio_dash/portfolio/dashboard_models.py`
- Test: `tests/portfolio/test_dashboard_models.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/portfolio/test_dashboard_models.py`:

```python
from datetime import datetime
from decimal import Decimal

from portfolio_dash.portfolio.dashboard_models import (
    DashboardData,
    DividendSummary,
    FreshnessReport,
    HoldingRow,
    KpiSummary,
    TrendSeries,
)
from portfolio_dash.portfolio.results import Holding, RealizedPnL
from portfolio_dash.shared.enums import Currency, Market


def _minimal_dashboard() -> DashboardData:
    return DashboardData(
        as_of=datetime(2026, 6, 10, 12, 0),
        reporting_currency=Currency.TWD,
        kpis=KpiSummary(reporting_currency=Currency.TWD,
                        total_market_value=Decimal("639600")),
        holdings=[],
        realized=RealizedPnL(rows=[], by_currency={}),
        returns=None,
        allocation=None,
        currency_view=None,
        fx=None,
        dividends=DividendSummary(by_year=[], total_by_currency={}),
        ex_dividend_calendar=[],
        trend=TrendSeries(points=[], reporting_currency=Currency.TWD, available=False),
        freshness=FreshnessReport(prices=[], fx=[], any_stale=False,
                                  missing_prices=[], missing_fx=[]),
    )


def test_dashboard_data_round_trips_and_preserves_decimal() -> None:
    data = _minimal_dashboard()
    dumped = data.model_dump()
    assert dumped["kpis"]["total_market_value"] == Decimal("639600")
    assert isinstance(dumped["kpis"]["total_market_value"], Decimal)
    assert DashboardData.model_validate(dumped) == data
    assert data.insights == []  # placeholder defaults empty


def test_holding_row_builds_from_holding_dump_plus_enrichment() -> None:
    h = Holding(account_id="schwab", symbol="AAPL", quote_ccy=Currency.USD,
                shares=Decimal("10"), original_avg=Decimal("100"),
                adjusted_avg=Decimal("100"), original_cost_total=Decimal("1000"),
                adjusted_cost_total=Decimal("1000"), dividend_portion=Decimal("0"),
                payback_ratio=Decimal("0"))
    data = h.model_dump()
    data.update(account_name="Charles Schwab", name="Apple", market=Market.US,
                sector="Tech", board="", price_as_of=None, weight=None)
    row = HoldingRow(**data)
    assert row.symbol == "AAPL"
    assert row.account_name == "Charles Schwab"
    assert row.market_value is None and row.weight is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/portfolio/test_dashboard_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'portfolio_dash.portfolio.dashboard_models'`

- [ ] **Step 3: Implement**

Create `portfolio_dash/portfolio/dashboard_models.py`:

```python
"""Dashboard contract models — the data shape web_ui (and later llm_insight) binds to.

All money/quantity/rate fields are Decimal at full precision; display formatting
(thousands separators, decimal places) is a template concern, never done here.
"""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from portfolio_dash.forex.results import FXSummary
from portfolio_dash.portfolio.results import (
    CombinedView,
    RealizedPnL,
    ReturnSummary,
    SectorAllocation,
)
from portfolio_dash.shared.enums import Currency, Market


class HoldingRow(BaseModel):
    """Flattened holding row: all ``Holding`` fields + instrument/account enrichment."""

    account_id: str
    account_name: str
    symbol: str
    name: str
    market: Market
    sector: str
    board: str
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
    price_as_of: date | None = None
    weight: Decimal | None = None


class KpiSummary(BaseModel):
    """Blended reporting-currency KPIs; every figure Optional (honest degradation).

    XIRR is surfaced only here; ``ReturnSummary.xirr`` stays None (single-sourced).
    """

    reporting_currency: Currency
    total_market_value: Decimal | None = None
    total_return: Decimal | None = None
    total_return_rate: Decimal | None = None
    realized_total: Decimal | None = None
    unrealized_total: Decimal | None = None
    xirr: Decimal | None = None
    fx_realized: Decimal | None = None
    fx_unrealized: Decimal | None = None


class DividendYearRow(BaseModel):
    year: int
    by_currency: dict[Currency, Decimal]


class DividendSummary(BaseModel):
    """Native-currency net dividend totals (no FX conversion — exact)."""

    by_year: list[DividendYearRow]
    total_by_currency: dict[Currency, Decimal]


class ExDividendItem(BaseModel):
    """An upcoming dividend event for a held symbol (from pricing's reference data)."""

    symbol: str
    name: str
    ex_date: date
    pay_date: date | None = None
    cash_amount: Decimal | None = None
    stock_amount: Decimal | None = None
    currency: Currency | None = None
    source: str


class TrendPoint(BaseModel):
    date: date
    total_value: Decimal
    net_invested: Decimal
    incomplete: bool = False


class TrendSeries(BaseModel):
    """Daily replay series; ``available=False`` means points is empty + reason in freshness."""

    points: list[TrendPoint]
    reporting_currency: Currency
    available: bool = True


class PriceFreshness(BaseModel):
    symbol: str
    as_of: date | None  # None = no stored price at all
    stale: bool


class FxFreshness(BaseModel):
    base: Currency
    quote: Currency
    as_of: date | None  # None = pair never stored
    stale: bool


class FreshnessReport(BaseModel):
    prices: list[PriceFreshness]
    fx: list[FxFreshness]
    any_stale: bool
    missing_prices: list[str]
    missing_fx: list[str]
    xirr_unavailable_reason: str | None = None
    trend_unavailable_reason: str | None = None


class InsightCardStub(BaseModel):
    """Placeholder card shape (llm_insight not built yet; the combiner returns [])."""

    id: str
    title: str
    body: str
    generated_at: datetime


class DashboardData(BaseModel):
    """One complete dashboard data model — the contract the UI binds to."""

    as_of: datetime
    reporting_currency: Currency
    kpis: KpiSummary
    holdings: list[HoldingRow]
    realized: RealizedPnL
    returns: ReturnSummary | None
    allocation: SectorAllocation | None
    currency_view: CombinedView | None
    fx: FXSummary | None
    dividends: DividendSummary
    ex_dividend_calendar: list[ExDividendItem]
    trend: TrendSeries
    freshness: FreshnessReport
    insights: list[InsightCardStub] = Field(default_factory=list)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/portfolio/test_dashboard_models.py -v`
Expected: 2 PASS

- [ ] **Step 5: Gates + commit**

```bash
./.venv/Scripts/python.exe -m mypy portfolio_dash --strict
./.venv/Scripts/python.exe -m ruff check portfolio_dash tests
git add portfolio_dash/portfolio/dashboard_models.py tests/portfolio/test_dashboard_models.py
git commit -m "feat(portfolio): dashboard contract models (DashboardData)"
```

---

### Task 4: Daily replay trend (`portfolio/timeseries.py`)

**Files:**
- Create: `portfolio_dash/portfolio/timeseries.py`
- Test: `tests/portfolio/test_timeseries.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/portfolio/test_timeseries.py`:

```python
from datetime import date
from decimal import Decimal

from portfolio_dash.portfolio.timeseries import daily_value_series
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import Dividend, OpeningInventory, Transaction

USD = Currency.USD
TWD = Currency.TWD

INSTRUMENTS = {
    "AAA": Instrument(symbol="AAA", market=Market.US, quote_ccy=USD,
                      sector="Tech", name="AAA Corp"),
    "BBB": Instrument(symbol="BBB", market=Market.TW, quote_ccy=TWD,
                      sector="Semis", name="BBB Corp", board="TWSE"),
}


def _tx(day: date, side: Side, qty: str, price: str, fees: str = "1",
        symbol: str = "AAA") -> Transaction:
    return Transaction(account_id="schwab", symbol=symbol, side=side,
                       quantity=Decimal(qty), price=Decimal(price),
                       fees=Decimal(fees), tax=Decimal("0"), trade_date=day)


def test_carry_forward_values_and_net_invested() -> None:
    txs = [_tx(date(2026, 6, 1), Side.BUY, "10", "100")]
    prices = {"AAA": [(date(2026, 6, 1), Decimal("100")),
                      (date(2026, 6, 3), Decimal("110"))]}
    fx = {(USD, TWD): [(date(2026, 6, 1), Decimal("30"))]}
    series = daily_value_series(txs, [], [], INSTRUMENTS, prices, fx, TWD,
                                end=date(2026, 6, 4))
    assert series.available is True
    assert [p.date for p in series.points] == [
        date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3), date(2026, 6, 4)]
    assert [p.total_value for p in series.points] == [
        Decimal("30000"), Decimal("30000"), Decimal("33000"), Decimal("33000")]
    # net invested = (10*100 + 1 fee) * 30 on every day after the buy
    assert all(p.net_invested == Decimal("30030") for p in series.points)
    assert all(p.incomplete is False for p in series.points)


def test_missing_early_price_flags_incomplete() -> None:
    txs = [_tx(date(2026, 6, 1), Side.BUY, "10", "100")]
    prices = {"AAA": [(date(2026, 6, 2), Decimal("100"))]}  # nothing on day 1
    fx = {(USD, TWD): [(date(2026, 6, 1), Decimal("30"))]}
    series = daily_value_series(txs, [], [], INSTRUMENTS, prices, fx, TWD,
                                end=date(2026, 6, 2))
    assert series.points[0].incomplete is True
    assert series.points[0].total_value == Decimal("0")
    assert series.points[1].incomplete is False
    assert series.points[1].total_value == Decimal("30000")


def test_inverse_pair_fallback() -> None:
    txs = [_tx(date(2026, 6, 1), Side.BUY, "10", "100", fees="0")]
    prices = {"AAA": [(date(2026, 6, 1), Decimal("100"))]}
    fx = {(TWD, USD): [(date(2026, 6, 1), Decimal("0.03125"))]}  # 1/0.03125 = 32
    series = daily_value_series(txs, [], [], INSTRUMENTS, prices, fx, TWD,
                                end=date(2026, 6, 1))
    assert series.available is True
    assert series.points[0].total_value == Decimal("32000")
    assert series.points[0].net_invested == Decimal("32000")


def test_dividend_and_sell_reduce_net_invested() -> None:
    txs = [_tx(date(2026, 6, 1), Side.BUY, "10", "100"),
           _tx(date(2026, 6, 3), Side.SELL, "5", "120")]
    divs = [Dividend(account_id="schwab", symbol="AAA", date=date(2026, 6, 2),
                     type=DividendType.CASH, gross=Decimal("50"),
                     withholding=Decimal("0"), net=Decimal("50"))]
    prices = {"AAA": [(date(2026, 6, 1), Decimal("100")),
                      (date(2026, 6, 3), Decimal("120"))]}
    fx = {(USD, TWD): [(date(2026, 6, 1), Decimal("30"))]}
    series = daily_value_series(txs, divs, [], INSTRUMENTS, prices, fx, TWD,
                                end=date(2026, 6, 3))
    # day1: +1001*30 = 30030 ; day2: -50*30 -> 28530 ; day3: -(600-1)*30 -> 10560
    assert [p.net_invested for p in series.points] == [
        Decimal("30030"), Decimal("28530"), Decimal("10560")]
    assert series.points[2].total_value == Decimal("18000")  # 5 sh * 120 * 30


def test_opening_inventory_counts_as_invested() -> None:
    opening = [OpeningInventory(account_id="tw_broker", symbol="BBB",
                                shares=Decimal("10"), original_avg_cost=Decimal("90"),
                                original_cost_total=Decimal("900"),
                                build_date=date(2026, 6, 1))]
    prices = {"BBB": [(date(2026, 6, 1), Decimal("100"))]}
    series = daily_value_series([], [], opening, INSTRUMENTS, prices, {}, TWD,
                                end=date(2026, 6, 1))
    assert series.available is True  # TWD->TWD needs no FX rows
    assert series.points[0].total_value == Decimal("1000")
    assert series.points[0].net_invested == Decimal("900")


def test_missing_flow_fx_makes_series_unavailable() -> None:
    txs = [_tx(date(2026, 6, 1), Side.BUY, "10", "100")]
    prices = {"AAA": [(date(2026, 6, 1), Decimal("100"))]}
    series = daily_value_series(txs, [], [], INSTRUMENTS, prices, {}, TWD,
                                end=date(2026, 6, 2))
    assert series.available is False
    assert series.points == []


def test_empty_ledgers_unavailable() -> None:
    series = daily_value_series([], [], [], INSTRUMENTS, {}, {}, TWD,
                                end=date(2026, 6, 1))
    assert series.available is False
    assert series.points == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/portfolio/test_timeseries.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'portfolio_dash.portfolio.timeseries'`

- [ ] **Step 3: Implement**

Create `portfolio_dash/portfolio/timeseries.py`:

```python
"""Daily portfolio-value replay: market value + cumulative net invested per day.

Pure function over in-memory inputs (no DB handle): the combiner bulk-loads price/FX
history once and passes it in. Valuation uses the carry-forward convention (latest
stored value on-or-before the day); a day a held symbol has no price at all is
flagged ``incomplete`` (never guessed). Any ledger flow whose date has no
on-or-before FX makes the whole series unavailable (consistent with the XIRR rule).
"""

from bisect import bisect_right
from datetime import date, timedelta
from decimal import Decimal

from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.portfolio.dashboard_models import TrendPoint, TrendSeries
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.fx import convert
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import Dividend, OpeningInventory, Transaction

_ZERO = Decimal("0")
_ONE = Decimal("1")

# Ascending (date, value) series, as bulk-loaded by the combiner.
PriceHistory = dict[str, list[tuple[date, Decimal]]]
FxHistory = dict[tuple[Currency, Currency], list[tuple[date, Decimal]]]


def _at_or_before(series: list[tuple[date, Decimal]], on: date) -> Decimal | None:
    """Latest value at-or-before ``on`` over an ascending series, else None."""
    idx = bisect_right(series, on, key=lambda item: item[0])
    if idx == 0:
        return None
    return series[idx - 1][1]


def _fx_at(history: FxHistory, on: date, base: Currency, quote: Currency) -> Decimal | None:
    """Carry-forward rate: identity -> direct pair -> inverted pair -> None."""
    if base == quote:
        return _ONE
    direct = history.get((base, quote))
    if direct is not None:
        rate = _at_or_before(direct, on)
        if rate is not None:
            return rate
    inverse = history.get((quote, base))
    if inverse is not None:
        rate = _at_or_before(inverse, on)
        if rate is not None:
            return _ONE / rate
    return None


def daily_value_series(
    transactions: list[Transaction],
    dividends: list[Dividend],
    opening: list[OpeningInventory],
    instruments: dict[str, Instrument],
    price_history: PriceHistory,
    fx_history: FxHistory,
    reporting: Currency,
    *,
    end: date,
) -> TrendSeries:
    """Replay the ledgers day by day from the first event to ``end``.

    Returns ``available=False`` (empty points) when there are no ledger events, or
    when any flow date lacks an on-or-before FX rate for its needed pair.
    """
    event_dates = (
        [t.trade_date for t in transactions]
        + [d.date for d in dividends]
        + [o.build_date for o in opening]
    )
    if not event_dates:
        return TrendSeries(points=[], reporting_currency=reporting, available=False)
    start = min(event_dates)

    def quote_ccy(symbol: str) -> Currency:
        inst = instruments.get(symbol)
        if inst is None:
            raise KeyError(f"unknown instrument: {symbol}")
        return inst.quote_ccy

    # Net-invested flow deltas (signs mirror the XIRR conventions, negated):
    # opening +cost, buy +gross(incl. fees+tax), sell -net, cash dividend -net.
    flows: list[tuple[date, Currency, Decimal]] = []
    for o in opening:
        flows.append((o.build_date, quote_ccy(o.symbol), o.original_cost_total))
    for t in transactions:
        gross = t.quantity * t.price
        if t.side is Side.BUY:
            flows.append((t.trade_date, quote_ccy(t.symbol), gross + t.fees + t.tax))
        else:
            flows.append((t.trade_date, quote_ccy(t.symbol), -(gross - t.fees - t.tax)))
    for dv in dividends:
        if dv.type is DividendType.CASH:
            flows.append((dv.date, quote_ccy(dv.symbol), -dv.net))

    # Convert each flow at its own date's carry-forward FX; bail honestly if any
    # flow cannot be converted (no on-or-before rate).
    converted: list[tuple[date, Decimal]] = []
    for d, ccy, amount in flows:
        rate = _fx_at(fx_history, d, ccy, reporting)
        if rate is None:
            return TrendSeries(points=[], reporting_currency=reporting, available=False)
        converted.append((d, convert(amount, rate)))

    points: list[TrendPoint] = []
    day = start
    while day <= end:
        book = build_book(
            [t for t in transactions if t.trade_date <= day],
            [d for d in dividends if d.date <= day],
            [o for o in opening if o.build_date <= day],
            instruments,
        )
        total = _ZERO
        incomplete = False
        for h in book.holdings:
            if h.shares == _ZERO:
                continue
            price = _at_or_before(price_history.get(h.symbol, []), day)
            if price is None:
                incomplete = True
                continue
            rate = _fx_at(fx_history, day, h.quote_ccy, reporting)
            if rate is None:
                incomplete = True
                continue
            total += convert(price * h.shares, rate)
        net_invested = _ZERO
        for d, amt in converted:
            if d <= day:
                net_invested += amt
        points.append(TrendPoint(date=day, total_value=total,
                                 net_invested=net_invested, incomplete=incomplete))
        day += timedelta(days=1)

    return TrendSeries(points=points, reporting_currency=reporting, available=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/portfolio/test_timeseries.py -v`
Expected: 7 PASS

- [ ] **Step 5: Gates + commit**

```bash
./.venv/Scripts/python.exe -m mypy portfolio_dash --strict
./.venv/Scripts/python.exe -m ruff check portfolio_dash tests
git add portfolio_dash/portfolio/timeseries.py tests/portfolio/test_timeseries.py
git commit -m "feat(portfolio): daily_value_series (ledger replay trend, pure)"
```

---

### Task 5: The combiner (`portfolio/dashboard.py`) + happy-path integration test

**Files:**
- Create: `portfolio_dash/portfolio/dashboard.py`
- Test: `tests/portfolio/test_dashboard.py` (create)

- [ ] **Step 1: Write the failing happy-path test**

Create `tests/portfolio/test_dashboard.py`. The seed builds a realistic two-account portfolio; all expected numbers below are exact Decimal arithmetic (verified by hand in the spec phase):

```python
import sqlite3
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_dividend,
    insert_fx_conversion,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.pricing.results import DividendEvent, FxRow, PriceRow
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.pricing.store import upsert_dividend_events, upsert_fx, upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

NOW = datetime(2026, 6, 10, 12, 0)
TWD = Currency.TWD
USD = Currency.USD
MYR = Currency.MYR


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    create_pricing_tables(c)
    seed_accounts(c)
    yield c
    c.close()


def _seed_full(conn: sqlite3.Connection) -> None:
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=TWD,
                                       sector="Semiconductors", name="TSMC", board="TWSE"))
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US, quote_ccy=USD,
                                       sector="Tech", name="Apple"))
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("1000"), price=Decimal("500"),
                       fees=Decimal("0"), tax=Decimal("0"),
                       trade_date=date(2026, 1, 5))
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("10"), price=Decimal("100"),
                       fees=Decimal("0"), tax=Decimal("0"),
                       trade_date=date(2026, 1, 10))
    insert_dividend(conn, account_id="tw_broker", symbol="2330",
                    div_date=date(2026, 3, 1), div_type="CASH",
                    gross=Decimal("5000"), withholding=Decimal("0"),
                    net=Decimal("5000"))
    insert_fx_conversion(conn, account_id="schwab", date=date(2026, 1, 8),
                         from_ccy=TWD, from_amount=Decimal("32000"),
                         to_ccy=USD, to_amount=Decimal("1000"))
    upsert_prices(conn, [
        PriceRow(instrument="2330", market=Market.TW, as_of=date(2026, 1, 5),
                 close=Decimal("500"), source="test"),
        PriceRow(instrument="2330", market=Market.TW, as_of=date(2026, 6, 9),
                 close=Decimal("600"), source="test"),
        PriceRow(instrument="AAPL", market=Market.US, as_of=date(2026, 1, 10),
                 close=Decimal("100"), source="test"),
        PriceRow(instrument="AAPL", market=Market.US, as_of=date(2026, 6, 9),
                 close=Decimal("120"), source="test"),
    ], fetched_at=NOW)
    upsert_fx(conn, [
        FxRow(base=USD, quote=TWD, as_of=date(2026, 1, 8), rate=Decimal("32"),
              source="test"),
        FxRow(base=USD, quote=TWD, as_of=date(2026, 6, 9), rate=Decimal("33"),
              source="test"),
        FxRow(base=MYR, quote=TWD, as_of=date(2026, 6, 9), rate=Decimal("7"),
              source="test"),
        FxRow(base=USD, quote=MYR, as_of=date(2026, 6, 9), rate=Decimal("4.4"),
              source="test"),
    ], fetched_at=NOW)
    upsert_dividend_events(conn, [
        DividendEvent(instrument="2330", market=Market.TW, ex_date=date(2026, 6, 20),
                      cash_amount=Decimal("5"), currency=TWD, source="test"),
        DividendEvent(instrument="2330", market=Market.TW, ex_date=date(2026, 5, 1),
                      cash_amount=Decimal("5"), currency=TWD, source="test"),
    ], fetched_at=NOW)


def test_build_dashboard_happy_path(conn: sqlite3.Connection) -> None:
    _seed_full(conn)
    data = build_dashboard(conn, now=NOW, reporting=TWD)

    # KPIs: 2330 mv 600k TWD; AAPL mv 1200 USD @33 -> 39600 TWD.
    assert data.kpis.total_market_value == Decimal("639600")
    # unrealized: 2330 (600-495)*1000 = 105000 (cash div reduced adjusted avg to 495);
    # AAPL (120-100)*10*33 = 6600 -> total return 111600.
    assert data.kpis.total_return == Decimal("111600")
    assert data.kpis.realized_total == Decimal("0")
    assert data.kpis.unrealized_total == Decimal("111600")
    # rate = 111600 / (500000 + 1000*33)
    assert data.kpis.total_return_rate == Decimal("111600") / Decimal("533000")
    assert data.kpis.xirr is not None
    assert data.kpis.fx_realized == Decimal("0")
    assert data.kpis.fx_unrealized == Decimal("1200")  # 1200 USD stock * (33-32)

    # Holdings enrichment.
    by_symbol = {h.symbol: h for h in data.holdings}
    tsmc = by_symbol["2330"]
    assert tsmc.name == "TSMC" and tsmc.sector == "Semiconductors"
    assert tsmc.board == "TWSE" and tsmc.account_name == "TW Broker"
    assert tsmc.market_value == Decimal("600000")
    assert tsmc.unrealized_pnl == Decimal("105000")
    assert tsmc.price_as_of == date(2026, 6, 9) and tsmc.price_stale is False
    aapl = by_symbol["AAPL"]
    assert aapl.weight == Decimal("39600") / Decimal("639600")
    weights = sum(h.weight for h in data.holdings if h.weight is not None)
    assert abs(weights - Decimal("1")) < Decimal("1e-20")

    # Sections.
    assert data.returns is not None
    assert data.returns.by_currency[TWD].unrealized == Decimal("105000")
    assert data.allocation is not None
    assert data.allocation.by_sector["Semiconductors"] == Decimal("600000")
    assert data.currency_view is not None
    assert data.currency_view.by_currency_value == {TWD: Decimal("600000"),
                                                    USD: Decimal("1200")}
    assert data.fx is not None
    schwab_fx = data.fx.by_account["schwab"]
    assert schwab_fx.avg_rate == Decimal("32") and schwab_fx.current_spot == Decimal("33")
    assert schwab_fx.foreign_cash == Decimal("0")  # 1000 converted - 1000 spent

    # Dividends + calendar.
    assert data.dividends.total_by_currency == {TWD: Decimal("5000")}
    assert data.dividends.by_year[0].year == 2026
    assert [e.ex_date for e in data.ex_dividend_calendar] == [date(2026, 6, 20)]
    assert data.ex_dividend_calendar[0].name == "TSMC"

    # Trend: first point = buy day at cost; last point = today's full value.
    assert data.trend.available is True
    assert data.trend.points[0].date == date(2026, 1, 5)
    assert data.trend.points[0].total_value == Decimal("500000")
    assert data.trend.points[0].incomplete is False
    last = data.trend.points[-1]
    assert last.date == date(2026, 6, 10)
    assert last.total_value == Decimal("639600")
    # net invested: 500000 + 1000 USD @32 - 5000 dividend = 527000
    assert last.net_invested == Decimal("527000")

    # Freshness: everything present and fresh.
    assert data.freshness.missing_prices == []
    assert data.freshness.missing_fx == []
    assert data.freshness.any_stale is False
    assert data.freshness.xirr_unavailable_reason is None
    assert data.freshness.trend_unavailable_reason is None
    assert data.insights == []
```

**Note:** `insert_fx_conversion`'s date keyword is literally `date=` (verified against `portfolio_dash/data_ingestion/store.py:322`); the call above is correct as written.

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/portfolio/test_dashboard.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'portfolio_dash.portfolio.dashboard'`

- [ ] **Step 3: Implement**

Create `portfolio_dash/portfolio/dashboard.py`:

```python
"""The orchestration combiner: assemble one complete DashboardData from SQLite.

Read-only — never fetches. Reads ledgers (data_ingestion), prices/FX (pricing),
calls the calculation cores (portfolio, forex), and assembles the dashboard
contract. Degrades honestly: blended figures become None with freshness reasons;
it never fabricates and never raises on missing market data.

This module introduces the one-way edge ``portfolio -> forex`` (recorded in the
2026-06-10 dashboard-combiner spec); forex imports only ``shared``, so no cycle.
"""

import sqlite3
from datetime import date, datetime
from decimal import Decimal

from portfolio_dash.data_ingestion.store import (
    list_accounts,
    list_dividends,
    list_fx_conversions,
    list_instruments,
    list_opening,
    list_transactions,
)
from portfolio_dash.forex.fx_pnl import compute_fx_summary
from portfolio_dash.forex.results import FXSummary
from portfolio_dash.portfolio.allocation import combined_view, sector_allocation
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.portfolio.dashboard_models import (
    DashboardData,
    DividendSummary,
    DividendYearRow,
    ExDividendItem,
    FreshnessReport,
    FxFreshness,
    HoldingRow,
    KpiSummary,
    PriceFreshness,
    TrendSeries,
)
from portfolio_dash.portfolio.pnl import value_holdings
from portfolio_dash.portfolio.results import CombinedView, ReturnSummary, SectorAllocation
from portfolio_dash.portfolio.returns import total_return, xirr_reporting
from portfolio_dash.portfolio.timeseries import FxHistory, PriceHistory, daily_value_series
from portfolio_dash.pricing.results import FxRead, PriceRead
from portfolio_dash.pricing.store import (
    get_dividend_events,
    get_fx,
    get_fx_history,
    get_fx_on,
    get_latest_price,
    get_price_history,
)
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.fx import convert
from portfolio_dash.shared.models.enums import DividendType
from portfolio_dash.shared.models.ledger import (
    Dividend,
    FXConversion,
    OpeningInventory,
    Transaction,
)

_ZERO = Decimal("0")
_ONE = Decimal("1")
# History reads start here: stored prices may predate the first ledger event.
_EPOCH = date(1900, 1, 1)


class _RateResolver:
    """Current-FX lookup: identity -> direct pair -> inverted pair -> KeyError.

    Records every requested pair (found or not) for the freshness report.
    """

    def __init__(self, conn: sqlite3.Connection, *, now: datetime) -> None:
        self._conn = conn
        self._now = now
        self.reads: dict[tuple[Currency, Currency], FxRead | None] = {}

    def _read(self, base: Currency, quote: Currency) -> FxRead | None:
        direct = get_fx(self._conn, base, quote, now=self._now)
        if direct is not None:
            return direct
        inverse = get_fx(self._conn, quote, base, now=self._now)
        if inverse is not None:
            return FxRead(rate=_ONE / inverse.rate, as_of=inverse.as_of,
                          source=inverse.source, stale=inverse.stale)
        return None

    def rate(self, base: Currency, quote: Currency) -> Decimal:
        if base == quote:
            return _ONE
        key = (base, quote)
        if key not in self.reads:
            self.reads[key] = self._read(base, quote)
        read = self.reads[key]
        if read is None:
            raise KeyError(f"no FX rate stored for {base.value}/{quote.value}")
        return read.rate


def build_dashboard(
    conn: sqlite3.Connection, *, now: datetime, reporting: Currency
) -> DashboardData:
    """Assemble the complete dashboard data model from SQLite (read-only)."""
    as_of = now.date()

    # 1. Ledgers and reference data (Stored* rows -> ledger models).
    txs = [
        Transaction(account_id=s.account_id, symbol=s.symbol, side=s.side,
                    quantity=s.quantity, price=s.price, fees=s.fees, tax=s.tax,
                    trade_date=s.trade_date)
        for s in list_transactions(conn)
    ]
    divs = [
        Dividend(account_id=s.account_id, symbol=s.symbol, date=s.date,
                 type=DividendType(s.type), gross=s.gross, withholding=s.withholding,
                 net=s.net, reinvest_shares=s.reinvest_shares,
                 reinvest_price=s.reinvest_price)
        for s in list_dividends(conn)
    ]
    convs = [
        FXConversion(account_id=s.account_id, date=s.date, from_ccy=s.from_ccy,
                     from_amount=s.from_amount, to_ccy=s.to_ccy, to_amount=s.to_amount)
        for s in list_fx_conversions(conn)
    ]
    opening = [
        OpeningInventory(account_id=s.account_id, symbol=s.symbol, shares=s.shares,
                         original_avg_cost=s.original_avg_cost,
                         original_cost_total=s.original_cost_total,
                         build_date=s.build_date)
        for s in list_opening(conn)
    ]
    instruments = {i.symbol: i for i in list_instruments(conn)}
    accounts = {a.account_id: a for a in list_accounts(conn)}

    # 2. Book and valuation.
    book = build_book(txs, divs, opening, instruments)
    held_symbols = sorted({h.symbol for h in book.holdings})
    price_reads: dict[str, PriceRead | None] = {
        sym: get_latest_price(conn, sym, now=now) for sym in held_symbols
    }
    price_map = {sym: pr.value for sym, pr in price_reads.items() if pr is not None}
    valued = value_holdings(book.holdings, price_map)

    resolver = _RateResolver(conn, now=now)

    # 3. Core summaries — each degrades to None on a missing current rate.
    returns: ReturnSummary | None
    try:
        returns = total_return(book, valued, resolver.rate, reporting)
    except KeyError:
        returns = None
    allocation: SectorAllocation | None
    try:
        allocation = sector_allocation(valued, instruments, resolver.rate, reporting)
    except KeyError:
        allocation = None
    view: CombinedView | None
    try:
        view = combined_view(valued, resolver.rate, reporting)
    except KeyError:
        view = None

    # 4. XIRR — on-or-before trade-date FX; degrades to None with a reason.
    def fx_at(d: date, base: Currency, quote: Currency) -> Decimal:
        if base == quote:
            return _ONE
        direct = get_fx_on(conn, base, quote, on=d)
        if direct is not None:
            return direct.rate
        inverse = get_fx_on(conn, quote, base, on=d)
        if inverse is not None:
            return _ONE / inverse.rate
        raise KeyError(
            f"no FX rate stored on or before {d.isoformat()} "
            f"for {base.value}/{quote.value}"
        )

    xirr_value: Decimal | None = None
    xirr_reason: str | None = None
    try:
        xirr_value = xirr_reporting(txs, divs, opening, valued, instruments, fx_at,
                                    price_map, resolver.rate, as_of, reporting)
    except KeyError as exc:
        xirr_reason = str(exc).strip("'\"")
    if xirr_value is None and xirr_reason is None:
        xirr_reason = ("not computable (missing current price, no sign change, "
                       "or non-convergence)")

    # 5. FX P&L — settlement != funding accounts; cold-start KeyError -> None.
    exposure: dict[str, tuple[Currency, Decimal]] = {}
    for acct in accounts.values():
        if acct.settlement_ccy == acct.funding_ccy:
            continue
        stock_value = _ZERO
        for h in valued:
            if h.account_id == acct.account_id and h.market_value is not None:
                stock_value += h.market_value
        exposure[acct.account_id] = (acct.settlement_ccy, stock_value)
    fx_summary: FXSummary | None
    try:
        fx_summary = compute_fx_summary(accounts, instruments, txs, divs, convs,
                                        exposure, resolver.rate, reporting)
    except KeyError:
        fx_summary = None

    # 6. Dividend summary — cash actually received (incl. DRIP net), native ccy.
    year_ccy: dict[int, dict[Currency, Decimal]] = {}
    total_ccy: dict[Currency, Decimal] = {}
    for dv in divs:
        if dv.type is DividendType.STOCK:
            continue  # 配股 adds shares, not cash
        ccy = instruments[dv.symbol].quote_ccy
        per_year = year_ccy.setdefault(dv.date.year, {})
        per_year[ccy] = per_year.get(ccy, _ZERO) + dv.net
        total_ccy[ccy] = total_ccy.get(ccy, _ZERO) + dv.net
    dividend_summary = DividendSummary(
        by_year=[DividendYearRow(year=y, by_currency=year_ccy[y])
                 for y in sorted(year_ccy)],
        total_by_currency=total_ccy,
    )

    # 7. Ex-dividend calendar — held symbols, upcoming only.
    calendar: list[ExDividendItem] = []
    for sym in held_symbols:
        inst = instruments[sym]
        for ev in get_dividend_events(conn, sym):
            if ev.ex_date >= as_of:
                calendar.append(ExDividendItem(
                    symbol=sym, name=inst.name, ex_date=ev.ex_date,
                    pay_date=ev.pay_date, cash_amount=ev.cash_amount,
                    stock_amount=ev.stock_amount, currency=ev.currency,
                    source=ev.source))
    calendar.sort(key=lambda e: e.ex_date)

    # 8. Holding rows — enrichment + weight; age-based staleness overrides
    # value_holdings' presence-based flag.
    total_value = view.reporting_total_value if view is not None else None
    holding_rows: list[HoldingRow] = []
    for h in valued:
        inst = instruments[h.symbol]
        acct = accounts[h.account_id]
        pr = price_reads.get(h.symbol)
        weight: Decimal | None = None
        if total_value is not None and total_value != _ZERO and h.market_value is not None:
            try:
                weight = (convert(h.market_value, resolver.rate(h.quote_ccy, reporting))
                          / total_value)
            except KeyError:
                weight = None
        data = h.model_dump()
        data.update(
            account_name=acct.name, name=inst.name, market=inst.market,
            sector=inst.sector, board=inst.board,
            price_as_of=pr.as_of if pr is not None else None,
            price_stale=pr.stale if pr is not None else True,
            weight=weight,
        )
        holding_rows.append(HoldingRow(**data))

    # 9. Trend — bulk-load histories, then the pure daily replay.
    trend_reason: str | None = None
    if txs or divs or opening:
        ledger_symbols = sorted({t.symbol for t in txs} | {d.symbol for d in divs}
                                | {o.symbol for o in opening})
        price_history: PriceHistory = {
            sym: [(p.as_of, p.value) for p in get_price_history(conn, sym, _EPOCH, as_of)]
            for sym in ledger_symbols
        }
        fx_history: FxHistory = {}
        for ccy in {instruments[sym].quote_ccy for sym in ledger_symbols}:
            if ccy == reporting:
                continue
            for base, quote in ((ccy, reporting), (reporting, ccy)):
                rows = get_fx_history(conn, base, quote, _EPOCH, as_of)
                if rows:
                    fx_history[(base, quote)] = [(r.as_of, r.rate) for r in rows]
        trend = daily_value_series(txs, divs, opening, instruments, price_history,
                                   fx_history, reporting, end=as_of)
        if not trend.available:
            trend_reason = "missing FX history for a ledger flow date"
    else:
        trend = TrendSeries(points=[], reporting_currency=reporting, available=False)
        trend_reason = "no ledger events"

    # 10. KPIs — blended; None whenever the blend cannot be formed honestly.
    total_return_blended: Decimal | None = None
    total_return_rate: Decimal | None = None
    realized_total: Decimal | None = None
    unrealized_total: Decimal | None = None
    if returns is not None:
        total_return_blended = returns.reporting_total_return
        gross_rep = _ZERO
        realized_rep = _ZERO
        unrealized_rep = _ZERO
        for ccy, cr in returns.by_currency.items():
            rate = resolver.rate(ccy, reporting)  # cached: already resolved above
            gross_rep += convert(cr.gross_invested, rate)
            realized_rep += convert(cr.realized, rate)
            unrealized_rep += convert(cr.unrealized, rate)
        realized_total = realized_rep
        unrealized_total = unrealized_rep
        if gross_rep != _ZERO:
            total_return_rate = total_return_blended / gross_rep
    kpis = KpiSummary(
        reporting_currency=reporting,
        total_market_value=total_value,
        total_return=total_return_blended,
        total_return_rate=total_return_rate,
        realized_total=realized_total,
        unrealized_total=unrealized_total,
        xirr=xirr_value,
        fx_realized=fx_summary.reporting_realized_fx if fx_summary is not None else None,
        fx_unrealized=(fx_summary.reporting_unrealized_fx
                       if fx_summary is not None else None),
    )

    # 11. Freshness.
    price_fresh = [
        PriceFreshness(symbol=sym,
                       as_of=pr.as_of if pr is not None else None,
                       stale=pr.stale if pr is not None else True)
        for sym, pr in price_reads.items()
    ]
    fx_fresh = [
        FxFreshness(base=base, quote=quote,
                    as_of=read.as_of if read is not None else None,
                    stale=read.stale if read is not None else True)
        for (base, quote), read in resolver.reads.items()
    ]
    freshness = FreshnessReport(
        prices=price_fresh,
        fx=fx_fresh,
        any_stale=any(p.stale for p in price_fresh) or any(f.stale for f in fx_fresh),
        missing_prices=[sym for sym, pr in price_reads.items() if pr is None],
        missing_fx=[f"{base.value}/{quote.value}"
                    for (base, quote), read in resolver.reads.items() if read is None],
        xirr_unavailable_reason=xirr_reason,
        trend_unavailable_reason=trend_reason,
    )

    return DashboardData(
        as_of=now,
        reporting_currency=reporting,
        kpis=kpis,
        holdings=holding_rows,
        realized=book.realized,
        returns=returns,
        allocation=allocation,
        currency_view=view,
        fx=fx_summary,
        dividends=dividend_summary,
        ex_dividend_calendar=calendar,
        trend=trend,
        freshness=freshness,
        insights=[],
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/portfolio/test_dashboard.py -v`
Expected: 1 PASS. If an assertion fails, debug the **expected value derivation** first (the seed numbers were hand-verified; a mismatch most likely means a wiring bug in `dashboard.py`, not a wrong expectation). Do not weaken assertions to make them pass.

- [ ] **Step 5: Gates + commit**

```bash
./.venv/Scripts/python.exe -m mypy portfolio_dash --strict
./.venv/Scripts/python.exe -m ruff check portfolio_dash tests
git add portfolio_dash/portfolio/dashboard.py tests/portfolio/test_dashboard.py
git commit -m "feat(portfolio): build_dashboard combiner (one DashboardData from SQLite)"
```

---

### Task 6: Degradation-path tests + CHANGELOG + full gates

**Files:**
- Test: `tests/portfolio/test_dashboard.py` (append)
- Modify: `CHANGELOG.md` (`[Unreleased]` → `### Added`)

- [ ] **Step 1: Write the degradation tests**

Append to `tests/portfolio/test_dashboard.py`:

```python
def _seed_usd_only(conn: sqlite3.Connection) -> None:
    """One schwab USD holding; FX/price seeding varies per test."""
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US, quote_ccy=USD,
                                       sector="Tech", name="Apple"))
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("10"), price=Decimal("100"),
                       fees=Decimal("0"), tax=Decimal("0"),
                       trade_date=date(2026, 1, 10))


def test_cold_start_missing_fx_degrades_blends(conn: sqlite3.Connection) -> None:
    _seed_usd_only(conn)
    upsert_prices(conn, [PriceRow(instrument="AAPL", market=Market.US,
                                  as_of=date(2026, 6, 9), close=Decimal("120"),
                                  source="test")], fetched_at=NOW)
    # No fx_rates rows at all.
    data = build_dashboard(conn, now=NOW, reporting=TWD)
    assert data.returns is None
    assert data.allocation is None
    assert data.currency_view is None
    assert data.fx is None
    assert data.kpis.total_market_value is None
    assert data.kpis.total_return is None
    assert "USD/TWD" in data.freshness.missing_fx
    # Per-position data still renders (no FX needed in quote ccy).
    assert data.holdings[0].market_value == Decimal("1200")
    assert data.holdings[0].weight is None
    assert data.kpis.xirr is None
    assert data.freshness.xirr_unavailable_reason is not None
    assert data.trend.available is False
    assert data.freshness.trend_unavailable_reason is not None


def test_no_prices_renders_at_cost_with_flags(conn: sqlite3.Connection) -> None:
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=TWD,
                                       sector="Semiconductors", name="TSMC",
                                       board="TWSE"))
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("1000"), price=Decimal("500"),
                       fees=Decimal("0"), tax=Decimal("0"),
                       trade_date=date(2026, 1, 5))
    data = build_dashboard(conn, now=NOW, reporting=TWD)
    h = data.holdings[0]
    assert h.market_value is None and h.unrealized_pnl is None
    assert h.price_stale is True and h.price_as_of is None
    assert data.freshness.missing_prices == ["2330"]
    # TWD-only: blends work via the identity rate; valued total is 0 (nothing valued).
    assert data.kpis.total_market_value == Decimal("0")
    assert data.returns is not None
    assert data.returns.by_currency[TWD].total_return == Decimal("0")
    assert data.kpis.xirr is None  # terminal value cannot be formed
    assert data.freshness.xirr_unavailable_reason is not None


def test_stale_price_used_and_flagged(conn: sqlite3.Connection) -> None:
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=TWD,
                                       sector="Semiconductors", name="TSMC",
                                       board="TWSE"))
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("1000"), price=Decimal("500"),
                       fees=Decimal("0"), tax=Decimal("0"),
                       trade_date=date(2026, 1, 5))
    upsert_prices(conn, [PriceRow(instrument="2330", market=Market.TW,
                                  as_of=date(2026, 4, 11), close=Decimal("600"),
                                  source="test")], fetched_at=NOW)  # 60 days old
    data = build_dashboard(conn, now=NOW, reporting=TWD)
    h = data.holdings[0]
    assert h.market_value == Decimal("600000")  # last-known value IS used
    assert h.price_stale is True                # ...but flagged
    assert h.price_as_of == date(2026, 4, 11)
    assert data.freshness.any_stale is True
    assert data.freshness.missing_prices == []


def test_xirr_flow_predates_fx_history(conn: sqlite3.Connection) -> None:
    _seed_usd_only(conn)
    upsert_prices(conn, [PriceRow(instrument="AAPL", market=Market.US,
                                  as_of=date(2026, 6, 9), close=Decimal("120"),
                                  source="test")], fetched_at=NOW)
    # Current FX exists, but nothing on/before the 2026-01-10 buy.
    upsert_fx(conn, [FxRow(base=USD, quote=TWD, as_of=date(2026, 6, 9),
                           rate=Decimal("33"), source="test"),
                     FxRow(base=MYR, quote=TWD, as_of=date(2026, 6, 9),
                           rate=Decimal("7"), source="test"),
                     FxRow(base=USD, quote=MYR, as_of=date(2026, 6, 9),
                           rate=Decimal("4.4"), source="test")], fetched_at=NOW)
    data = build_dashboard(conn, now=NOW, reporting=TWD)
    assert data.returns is not None          # current rates fine
    assert data.kpis.xirr is None            # historical rate missing
    reason = data.freshness.xirr_unavailable_reason
    assert reason is not None and "USD/TWD" in reason and "2026-01-10" in reason
    assert data.trend.available is False     # same missing flow-date FX
```

- [ ] **Step 2: Run the new tests**

Run: `./.venv/Scripts/python.exe -m pytest tests/portfolio/test_dashboard.py -v`
Expected: all PASS (5 total in the file). If a degradation test fails, fix `dashboard.py`'s degradation wiring — do not weaken the test.

- [ ] **Step 3: CHANGELOG entry**

In `CHANGELOG.md` under `## [Unreleased]` → `### Added`, append (rewrite the bounded section, not surgical string edits):

```markdown
- `portfolio/dashboard.py` — the orchestration combiner: `build_dashboard(conn, now,
  reporting)` assembles one complete `DashboardData` (KPIs, enriched holdings, realized
  P&L, returns, sector allocation, currency view, FX P&L, dividend summary, ex-dividend
  calendar, daily-replay trend series, freshness report, insight placeholders) from the
  ledgers + stored prices/FX; the contract `web_ui` (and later `llm_insight`) binds to.
  Introduces the one-way dependency edge `portfolio -> forex` (spec
  2026-06-10-dashboard-combiner-design).
- `portfolio/timeseries.py` — pure daily ledger-replay valuation series (market value
  vs cumulative net invested, carry-forward prices/FX, honest `incomplete`/unavailable
  flags).
- `pricing/store.py` — `get_fx_on` (on-or-before point-in-time rate) and
  `get_fx_history` reads; `data_ingestion/store.py` — `list_accounts` read.
```

Then verify integrity: `grep -c "^## \[v" CHANGELOG.md` — count must equal the number of shipped versions (currently 1).

- [ ] **Step 4: Full gates**

```bash
./.venv/Scripts/python.exe -m pytest > pytest_out.txt 2>&1; tail -5 pytest_out.txt
./.venv/Scripts/python.exe -m mypy portfolio_dash --strict
./.venv/Scripts/python.exe -m ruff check portfolio_dash tests
```

Expected: pytest summary line shows **all passed** (≈265+ passed, 3 skipped — read the `N passed` summary line from the file, NOT the progress bar); mypy `Success: no issues found`; ruff `All checks passed!`. Delete `pytest_out.txt` afterwards (`rm pytest_out.txt`).

- [ ] **Step 5: Commit**

```bash
git add tests/portfolio/test_dashboard.py CHANGELOG.md
git commit -m "test(portfolio): combiner degradation paths; changelog for dashboard combiner"
```

---

## Plan self-review notes (already applied)

- Spec coverage: contract models (Task 3), new reads (Tasks 1–2), trend (Task 4), assembly + happy path (Task 5), degradation + CHANGELOG (Task 6). The `portfolio → forex` edge is documented in `dashboard.py`'s docstring + CHANGELOG.
- `insert_fx_conversion(..., date=...)` keyword verified against `store.py:322`.
- Type consistency: `PriceHistory`/`FxHistory` aliases defined in Task 4 and imported in Task 5; `TrendSeries.available` defaults True but is set explicitly everywhere it matters.
