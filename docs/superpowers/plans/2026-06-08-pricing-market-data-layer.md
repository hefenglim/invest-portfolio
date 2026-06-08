# `pricing/` Market-Data Layer Implementation Plan (A+B+C, staged A→B→C)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fetch market data into SQLite behind a config-driven, capability-aware provider chain with graceful degradation — (A) latest quotes + FX, (B) historical daily backfill, (C) dividend/ex-div reference data.

**Architecture:** Provider adapters (fetch → normalized in-memory rows, no DB) → registry (config-ordered fallback chain, capability-aware, records provenance) → store/repository (idempotent upsert, the only writer of price/fx/dividend-event rows) → refresh orchestrators (write entrypoints). Reads (`get_latest_price`/`get_fx`/`get_price_history`/`get_dividend_events`) serve last-known + staleness. `pricing/` imports only `shared/*`. Per `architecture.md` it never writes the ledger; dividend events are **reference only** (calc reads only the ledger → no double-count).

**Tech Stack:** Python 3.12, Pydantic v2, stdlib `sqlite3` (via `shared.db`), `Decimal` (canonical TEXT via `shared.money`), `yfinance`/`requests`/`FinMind` (already installed as the `probe` extra; promote to runtime deps in Task A1). Tests use the probe's recorded fixtures under `tests/pricing/fixtures/` — **no live network**.

**Reference material:** the spec `docs/superpowers/specs/2026-06-08-pricing-quotes-fx-design.md`; the probe's working adapters `scripts/probe/adapters/*` (logic to port into typed providers); recorded fixtures `tests/pricing/fixtures/{yfinance,twse,tpex,finmind}`.

**Money discipline:** never float; `Decimal(str(x))` from sources; persist via `shared.money.to_db` (canonical string); FX rates high precision. Parse fixtures' numeric JSON through `Decimal(str(...))`.

---

## File Structure

```
portfolio_dash/pricing/
  __init__.py
  enums.py          # DataType (QUOTE_LATEST, QUOTE_HISTORY, FX, DIVIDEND)
  refs.py           # InstrumentRef, FxPair (inputs)
  results.py        # PriceRow, FxRow, DividendEvent, PriceRead, FxRead, RefreshSummary
  schema.py         # CREATE TABLE IF NOT EXISTS prices, fx_rates, dividend_events
  store.py          # idempotent upserts + read-latest/history/events + staleness
  providers/
    __init__.py
    base.py         # ProviderBase: name, supports(), fetch_* (default NotImplementedError)
    yfinance_provider.py
    twse_provider.py
    tpex_provider.py
    finmind_provider.py
  registry.py       # config-ordered, capability-aware chain + fallback + provenance
  refresh.py        # refresh_quotes / refresh_history / refresh_dividends -> RefreshSummary
tests/pricing/
  __init__.py
  conftest.py       # in-memory sqlite fixture
  test_store.py  test_yfinance_provider.py  test_tw_providers.py
  test_registry.py  test_refresh_quotes.py
  test_history.py  test_finmind_provider.py  test_refresh_dividends.py
```

---

# PHASE A — latest quotes + FX (shared infra)

## Task A1: enums, refs, results, schema; promote runtime deps

**Files:** Create `portfolio_dash/pricing/__init__.py`, `enums.py`, `refs.py`, `results.py`, `schema.py`, `tests/pricing/__init__.py`, `tests/pricing/conftest.py`, `tests/pricing/test_schema.py`. Modify `pyproject.toml`.

- [ ] **Step 1: Promote runtime deps in `pyproject.toml`** — move `yfinance`, `requests`, `FinMind` from the `probe` optional group into the core `dependencies` array (pin as in the probe group). Leave `twstock`/`beautifulsoup4` in `probe` (not used by pricing v1).

- [ ] **Step 2: `enums.py`**
```python
from enum import StrEnum


class DataType(StrEnum):
    QUOTE_LATEST = "quote_latest"
    QUOTE_HISTORY = "quote_history"
    FX = "fx"
    DIVIDEND = "dividend"
```

- [ ] **Step 3: `refs.py`**
```python
from pydantic import BaseModel

from portfolio_dash.shared.enums import Currency, Market


class InstrumentRef(BaseModel, frozen=True):
    symbol: str
    market: Market
    board: str = ""  # "TWSE" | "TPEx" | ".KL" | "" (US)


class FxPair(BaseModel, frozen=True):
    base: Currency
    quote: Currency
```

- [ ] **Step 4: `results.py`**
```python
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from portfolio_dash.shared.enums import Currency, Market

Num = Field(allow_inf_nan=False)


class PriceRow(BaseModel):
    instrument: str
    market: Market
    as_of: date
    close: Decimal = Num
    open: Decimal | None = Field(default=None, allow_inf_nan=False)
    high: Decimal | None = Field(default=None, allow_inf_nan=False)
    low: Decimal | None = Field(default=None, allow_inf_nan=False)
    volume: Decimal | None = Field(default=None, allow_inf_nan=False)
    source: str


class FxRow(BaseModel):
    base: Currency
    quote: Currency
    as_of: date
    rate: Decimal = Num
    source: str


class DividendEvent(BaseModel):
    instrument: str
    market: Market
    ex_date: date
    pay_date: date | None = None
    cash_amount: Decimal | None = Field(default=None, allow_inf_nan=False)
    stock_amount: Decimal | None = Field(default=None, allow_inf_nan=False)
    currency: Currency | None = None
    source: str


class PriceRead(BaseModel):
    value: Decimal = Num
    as_of: date
    source: str
    stale: bool


class FxRead(BaseModel):
    rate: Decimal = Num
    as_of: date
    source: str
    stale: bool


class RefreshSummary(BaseModel):
    ok: dict[str, str] = Field(default_factory=dict)      # key -> winning source
    failed: list[str] = Field(default_factory=list)        # keys with no data
    fetched_at: datetime
```

- [ ] **Step 5: `tests/pricing/conftest.py`** — in-memory DB
```python
import sqlite3
from collections.abc import Iterator

import pytest

from portfolio_dash.pricing.schema import create_tables


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    create_tables(c)
    yield c
    c.close()
```

- [ ] **Step 6: Write failing test `tests/pricing/test_schema.py`**
```python
import sqlite3

from portfolio_dash.pricing.schema import create_tables


def test_create_tables_idempotent() -> None:
    c = sqlite3.connect(":memory:")
    create_tables(c)
    create_tables(c)  # second call must not error
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"prices", "fx_rates", "dividend_events"}.issubset(names)
```

- [ ] **Step 7: Run → fail.** `.\.venv\Scripts\python.exe -m pytest tests/pricing/test_schema.py -v` (ModuleNotFound).

- [ ] **Step 8: `schema.py`**
```python
import sqlite3

_DDL = """
CREATE TABLE IF NOT EXISTS prices (
    instrument TEXT NOT NULL, market TEXT NOT NULL, as_of_date TEXT NOT NULL,
    close TEXT NOT NULL, open TEXT, high TEXT, low TEXT, volume TEXT,
    source TEXT NOT NULL, fetched_at TEXT NOT NULL,
    PRIMARY KEY (instrument, as_of_date)
);
CREATE TABLE IF NOT EXISTS fx_rates (
    base TEXT NOT NULL, quote TEXT NOT NULL, as_of_date TEXT NOT NULL,
    rate TEXT NOT NULL, source TEXT NOT NULL, fetched_at TEXT NOT NULL,
    PRIMARY KEY (base, quote, as_of_date)
);
CREATE TABLE IF NOT EXISTS dividend_events (
    instrument TEXT NOT NULL, market TEXT NOT NULL, ex_date TEXT NOT NULL,
    pay_date TEXT, cash_amount TEXT, stock_amount TEXT, currency TEXT,
    source TEXT NOT NULL, fetched_at TEXT NOT NULL,
    PRIMARY KEY (instrument, ex_date)
);
"""


def create_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    conn.commit()
```

- [ ] **Step 9: Run → pass.** Then `mypy` + `ruff check portfolio_dash/pricing tests/pricing` clean.

- [ ] **Step 10: Commit** (scoped):
```
git add portfolio_dash/pricing/__init__.py portfolio_dash/pricing/enums.py portfolio_dash/pricing/refs.py portfolio_dash/pricing/results.py portfolio_dash/pricing/schema.py tests/pricing/__init__.py tests/pricing/conftest.py tests/pricing/test_schema.py pyproject.toml
git commit -m "feat(pricing): enums/refs/results models + SQLite schema (prices/fx_rates/dividend_events)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

## Task A2: `store.py` — idempotent upsert + read (prices + fx)

**Files:** Create `portfolio_dash/pricing/store.py`, `tests/pricing/test_store.py`.

- [ ] **Step 1: Failing test `tests/pricing/test_store.py`**
```python
from datetime import date, datetime
from decimal import Decimal

from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.pricing.store import (
    get_fx, get_latest_price, upsert_fx, upsert_prices,
)
from portfolio_dash.shared.enums import Currency, Market

_NOW = datetime(2026, 6, 8, 12, 0, 0)


def _price(close: str, d: date, source: str = "yfinance") -> PriceRow:
    return PriceRow(instrument="AAPL", market=Market.US, as_of=d,
                    close=Decimal(close), source=source)


def test_upsert_prices_idempotent(conn) -> None:
    upsert_prices(conn, [_price("100", date(2026, 6, 6))], fetched_at=_NOW)
    upsert_prices(conn, [_price("100", date(2026, 6, 6))], fetched_at=_NOW)  # no dup
    rows = list(conn.execute("SELECT close FROM prices WHERE instrument='AAPL'"))
    assert len(rows) == 1


def test_get_latest_price_returns_max_date(conn) -> None:
    upsert_prices(conn, [_price("100", date(2026, 6, 6)), _price("110", date(2026, 6, 8))],
                  fetched_at=_NOW)
    r = get_latest_price(conn, "AAPL", now=_NOW)
    assert r is not None and r.value == Decimal("110") and r.as_of == date(2026, 6, 8)
    assert r.stale is False


def test_get_latest_price_stale_when_old(conn) -> None:
    upsert_prices(conn, [_price("100", date(2026, 1, 1))], fetched_at=datetime(2026, 1, 1))
    r = get_latest_price(conn, "AAPL", now=_NOW, max_age_days=5)
    assert r is not None and r.stale is True


def test_get_latest_price_none_when_absent(conn) -> None:
    assert get_latest_price(conn, "NOPE", now=_NOW) is None


def test_upsert_and_get_fx(conn) -> None:
    upsert_fx(conn, [FxRow(base=Currency.USD, quote=Currency.TWD, as_of=date(2026, 6, 8),
                           rate=Decimal("31.5"), source="yfinance")], fetched_at=_NOW)
    r = get_fx(conn, Currency.USD, Currency.TWD, now=_NOW)
    assert r is not None and r.rate == Decimal("31.5") and r.stale is False
```

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Implement `store.py`** — use `shared.money.to_db`/`from_db` for Decimal↔TEXT; `INSERT ... ON CONFLICT DO UPDATE`; staleness = `(now.date() - as_of).days > max_age_days`.
```python
import sqlite3
from datetime import date, datetime
from decimal import Decimal

from portfolio_dash.pricing.results import FxRow, FxRead, PriceRead, PriceRow
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.money import from_db, to_db

_DEFAULT_MAX_AGE = 4  # days


def _opt(v: Decimal | None) -> str | None:
    return to_db(v) if v is not None else None


def upsert_prices(conn: sqlite3.Connection, rows: list[PriceRow], *, fetched_at: datetime) -> None:
    conn.executemany(
        """INSERT INTO prices (instrument, market, as_of_date, close, open, high, low,
               volume, source, fetched_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(instrument, as_of_date) DO UPDATE SET
               close=excluded.close, open=excluded.open, high=excluded.high, low=excluded.low,
               volume=excluded.volume, source=excluded.source, fetched_at=excluded.fetched_at""",
        [(r.instrument, r.market.value, r.as_of.isoformat(), to_db(r.close), _opt(r.open),
          _opt(r.high), _opt(r.low), _opt(r.volume), r.source, fetched_at.isoformat())
         for r in rows],
    )
    conn.commit()


def get_latest_price(conn: sqlite3.Connection, instrument: str, *, now: datetime,
                     max_age_days: int = _DEFAULT_MAX_AGE) -> PriceRead | None:
    row = conn.execute(
        "SELECT close, as_of_date, source FROM prices WHERE instrument=? "
        "ORDER BY as_of_date DESC LIMIT 1", (instrument,)).fetchone()
    if row is None:
        return None
    as_of = date.fromisoformat(row["as_of_date"])
    return PriceRead(value=from_db(row["close"]), as_of=as_of, source=row["source"],
                     stale=(now.date() - as_of).days > max_age_days)


def upsert_fx(conn: sqlite3.Connection, rows: list[FxRow], *, fetched_at: datetime) -> None:
    conn.executemany(
        """INSERT INTO fx_rates (base, quote, as_of_date, rate, source, fetched_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(base, quote, as_of_date) DO UPDATE SET
               rate=excluded.rate, source=excluded.source, fetched_at=excluded.fetched_at""",
        [(r.base.value, r.quote.value, r.as_of.isoformat(), to_db(r.rate), r.source,
          fetched_at.isoformat()) for r in rows],
    )
    conn.commit()


def get_fx(conn: sqlite3.Connection, base: Currency, quote: Currency, *, now: datetime,
           max_age_days: int = _DEFAULT_MAX_AGE) -> FxRead | None:
    row = conn.execute(
        "SELECT rate, as_of_date, source FROM fx_rates WHERE base=? AND quote=? "
        "ORDER BY as_of_date DESC LIMIT 1", (base.value, quote.value)).fetchone()
    if row is None:
        return None
    as_of = date.fromisoformat(row["as_of_date"])
    return FxRead(rate=from_db(row["rate"]), as_of=as_of, source=row["source"],
                  stale=(now.date() - as_of).days > max_age_days)
```
(If `shared.money` lacks `to_db`/`from_db` with these names, use the actual canonical-string helpers there — check `portfolio_dash/shared/money.py`.)

- [ ] **Step 4: Run → pass.** mypy + ruff clean.

- [ ] **Step 5: Commit** `portfolio_dash/pricing/store.py tests/pricing/test_store.py` — `"feat(pricing): idempotent price/fx upsert + read with staleness"`.

## Task A3: `providers/base.py` + yfinance provider (quote_latest + fx)

**Files:** Create `portfolio_dash/pricing/providers/__init__.py`, `base.py`, `yfinance_provider.py`, `tests/pricing/test_yfinance_provider.py`.

Port logic from `scripts/probe/adapters/yfinance_src.py`; parse the recorded fixture `tests/pricing/fixtures/yfinance/3182.KL.json`. The provider's `fetch_*` will accept an injected fetch function (or a module-level `_download`) so tests can feed fixture data without network.

- [ ] **Step 1: `base.py`**
```python
from datetime import date

from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.refs import FxPair, InstrumentRef
from portfolio_dash.pricing.results import DividendEvent, FxRow, PriceRow
from portfolio_dash.shared.enums import Market


class ProviderBase:
    name: str = "base"

    def supports(self, data_type: DataType, market: Market | None) -> bool:
        return False

    def fetch_quote_latest(self, instruments: list[InstrumentRef]) -> list[PriceRow]:
        raise NotImplementedError

    def fetch_quote_history(self, instrument: InstrumentRef, start: date) -> list[PriceRow]:
        raise NotImplementedError

    def fetch_fx(self, pairs: list[FxPair]) -> list[FxRow]:
        raise NotImplementedError

    def fetch_dividends(self, instruments: list[InstrumentRef]) -> list[DividendEvent]:
        raise NotImplementedError
```

- [ ] **Step 2: Failing test `tests/pricing/test_yfinance_provider.py`** — test the pure parser against the recorded fixture (load the saved DataFrame JSON) and the suffix mapping.
```python
from decimal import Decimal

from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.yfinance_provider import YFinanceProvider, yf_symbol
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.shared.enums import Market


def test_yf_symbol_suffix() -> None:
    assert yf_symbol(InstrumentRef(symbol="2330", market=Market.TW, board="TWSE")) == "2330.TW"
    assert yf_symbol(InstrumentRef(symbol="8299", market=Market.TW, board="TPEx")) == "8299.TWO"
    assert yf_symbol(InstrumentRef(symbol="3182", market=Market.MY, board=".KL")) == "3182.KL"
    assert yf_symbol(InstrumentRef(symbol="AAPL", market=Market.US)) == "AAPL"


def test_supports() -> None:
    p = YFinanceProvider()
    assert p.supports(DataType.QUOTE_LATEST, Market.US)
    assert p.supports(DataType.FX, None)


def test_parse_history_json_to_pricerows() -> None:
    # parse the recorded fixture into PriceRow list via the provider's pure parser
    import json
    from pathlib import Path
    raw = Path("tests/pricing/fixtures/yfinance/3182.KL.json").read_text("utf-8")
    rows = YFinanceProvider()._parse_history_json(json.loads(raw), instrument="3182",
                                                  market=Market.MY)
    assert rows and all(isinstance(r.close, Decimal) for r in rows)
    assert rows[-1].source == "yfinance"
```

- [ ] **Step 3: Run → fail.**

- [ ] **Step 4: Implement `yfinance_provider.py`** — `yf_symbol()`, `supports()` (QUOTE_LATEST/QUOTE_HISTORY/FX for US/TW/MY; DIVIDEND too as fallback), `_parse_history_json()` (pure: dict→PriceRow via `Decimal(str(close))`), `fetch_quote_latest()`/`fetch_fx()` calling `yfinance` (network) and reusing the parser. Keep network calls in thin methods; parsing pure + tested.
```python
# (network methods call yfinance; pure parsers tested against fixtures)
import json
from datetime import date, datetime
from decimal import Decimal

import yfinance as yf  # type: ignore[import-untyped]

from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.base import ProviderBase
from portfolio_dash.pricing.refs import FxPair, InstrumentRef
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.shared.enums import Currency, Market

_SUFFIX = {Market.US: "", Market.TW: ".TW", Market.MY: ".KL"}


def yf_symbol(ref: InstrumentRef) -> str:
    if ref.market is Market.TW and ref.board == "TPEx":
        return f"{ref.symbol}.TWO"
    return f"{ref.symbol}{_SUFFIX[ref.market]}"


class YFinanceProvider(ProviderBase):
    name = "yfinance"

    def supports(self, data_type: DataType, market: Market | None) -> bool:
        return data_type in {DataType.QUOTE_LATEST, DataType.QUOTE_HISTORY,
                             DataType.FX, DataType.DIVIDEND}

    def _parse_history_json(self, payload: dict, *, instrument: str,
                            market: Market) -> list[PriceRow]:
        # payload is pandas DataFrame.to_json() (columns orient); Close keyed by epoch-ms
        closes = payload.get("Close", {})
        rows: list[PriceRow] = []
        for ts_ms, close in closes.items():
            if close is None:
                continue
            d = datetime.utcfromtimestamp(int(ts_ms) / 1000).date()
            rows.append(PriceRow(instrument=instrument, market=market, as_of=d,
                                 close=Decimal(str(close)), source=self.name))
        rows.sort(key=lambda r: r.as_of)
        return rows

    def fetch_quote_latest(self, instruments: list[InstrumentRef]) -> list[PriceRow]:
        out: list[PriceRow] = []
        for ref in instruments:
            df = yf.Ticker(yf_symbol(ref)).history(period="5d", auto_adjust=False)
            if df is None or df.empty:
                continue
            out.append(PriceRow(instrument=ref.symbol, market=ref.market,
                                as_of=df.index[-1].date(),
                                close=Decimal(str(df["Close"].iloc[-1])), source=self.name))
        return out

    def fetch_fx(self, pairs: list[FxPair]) -> list[FxRow]:
        out: list[FxRow] = []
        for p in pairs:
            sym = f"{p.base.value}{p.quote.value}=X"
            df = yf.Ticker(sym).history(period="5d", auto_adjust=False)
            if df is None or df.empty:
                continue
            out.append(FxRow(base=p.base, quote=p.quote, as_of=df.index[-1].date(),
                             rate=Decimal(str(df["Close"].iloc[-1])), source=self.name))
        return out
```

- [ ] **Step 5: Run → pass** (the parser test uses the fixture; network methods are not called in tests). mypy + ruff clean.

- [ ] **Step 6: Commit** providers/`__init__.py`, `base.py`, `yfinance_provider.py`, test — `"feat(pricing): provider base + yfinance provider (quote/fx)"`.

## Task A4: TWSE + TPEx providers (TW quote_latest)

**Files:** Create `twse_provider.py`, `tpex_provider.py`, `tests/pricing/test_tw_providers.py`. Port `scripts/probe/adapters/tw_gov.py`; parse fixtures `tests/pricing/fixtures/twse/2330.json`, `tests/pricing/fixtures/tpex/daily.json`.

- [ ] **Step 1: Failing test** — parse the TWSE fixture (`parse → PriceRow` with `Decimal`, strip thousands separators e.g. `"2,295.00"`) and the TPEx fixture (find `8299` close). Assert `supports(QUOTE_LATEST, Market.TW)` true, `supports(..., Market.US)` false.
```python
import json
from decimal import Decimal
from pathlib import Path

from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.tpex_provider import TpexProvider
from portfolio_dash.pricing.providers.twse_provider import TwseProvider
from portfolio_dash.shared.enums import Market


def test_twse_parse_close() -> None:
    payload = json.loads(Path("tests/pricing/fixtures/twse/2330.json").read_text("utf-8"))
    r = TwseProvider()._parse(payload, instrument="2330")
    assert r is not None and r.close == Decimal("2295.00") and r.source == "twse"


def test_tpex_parse_close() -> None:
    rows = json.loads(Path("tests/pricing/fixtures/tpex/daily.json").read_text("utf-8"))
    r = TpexProvider()._parse(rows, instrument="8299")
    assert r is not None and r.close == Decimal("2250.00")


def test_supports_tw_only() -> None:
    assert TwseProvider().supports(DataType.QUOTE_LATEST, Market.TW)
    assert not TwseProvider().supports(DataType.QUOTE_LATEST, Market.US)
    assert not TwseProvider().supports(DataType.FX, None)
```

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Implement** both providers. `_parse` strips `,` then `Decimal(str)`. TWSE close = `data[-1][6]`; `as_of` parsed from ROC date `data[-1][0]` (`115/06/08` → 2026-06-08: year = int(parts[0])+1911). TPEx: find row where `SecuritiesCompanyCode == instrument`, `Close`; `as_of` from the payload's date field. `supports`: QUOTE_LATEST + Market.TW only. `fetch_quote_latest` calls `requests` (network), reuses `_parse`. (Port URLs/columns from `scripts/probe/adapters/tw_gov.py`.)

- [ ] **Step 4: Run → pass.** mypy + ruff clean.

- [ ] **Step 5: Commit** — `"feat(pricing): TWSE + TPEx providers (TW quote latest)"`.

## Task A5: `registry.py` — config-ordered, capability-aware fallback chain

**Files:** Create `registry.py`, `tests/pricing/test_registry.py`.

- [ ] **Step 1: Failing test** with fake providers (no network): a chain `[P_unsupported, P_fails, P_ok]` for `(QUOTE_LATEST, US)` must skip the unsupported, try the failing, then succeed on `P_ok`, returning its rows and recording source. If all fail → empty rows + the key in `failed`.
```python
from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.base import ProviderBase
from portfolio_dash.pricing.registry import Registry
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.shared.enums import Market
# ... define FakeOK / FakeFails / FakeUnsupported subclasses ...
# assert order honored, unsupported skipped, fallback on exception, provenance recorded
```

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Implement `Registry`** — holds `dict[str, list[ProviderBase]]` keyed by a `(data_type, market)` config order (built from `Settings`); `fetch_quote_latest(instruments)` groups instruments by market, walks the configured providers, **skips** those whose `supports()` is False, calls the next on exception/empty, returns `(rows, per_item_source)`; aggregates a status (ok source / failed). Same shape for `fetch_fx`. Pure given injected providers (no network in tests).

- [ ] **Step 4: Run → pass.** mypy + ruff clean.

- [ ] **Step 5: Commit** — `"feat(pricing): config-ordered capability-aware provider chain"`.

## Task A6: `refresh_quotes` orchestrator + read wiring + config defaults

**Files:** Create `refresh.py`, `tests/pricing/test_refresh_quotes.py`. Modify `portfolio_dash/shared/config.py` (add the per-(data_type, market) provider-order defaults).

- [ ] **Step 1: Failing test** — `refresh_quotes(conn, registry, instruments, fx_pairs, now)` fetches via a registry built from **fake providers**, upserts into the in-memory DB, and returns a `RefreshSummary` (ok sources + failed keys). Then `get_latest_price`/`get_fx` read the stored values. Include an all-fail case → keys in `failed`, no raise, DB unchanged.

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Implement `refresh.py`** `refresh_quotes(conn, registry, instruments, fx_pairs, *, now) -> RefreshSummary`: call `registry.fetch_quote_latest` + `fetch_fx`; `store.upsert_prices`/`upsert_fx`; build `RefreshSummary`. Never raise on provider failure (record in `failed`). Add `Settings` defaults: provider order per (data_type, market) from the probe ranking (US quote: `["yfinance"]`; TW quote: `["twse","tpex","yfinance"]`; MY quote: `["yfinance"]`; FX: `["yfinance"]`).

- [ ] **Step 4: Run → pass.** Full suite + mypy + ruff clean.

- [ ] **Step 5: Commit** — `"feat(pricing): refresh_quotes orchestrator + read API + config provider order"`.

---

# PHASE B — historical daily backfill

## Task B1: history fetch + store + read + `refresh_history`

**Files:** Modify `yfinance_provider.py` (add `fetch_quote_history`), `store.py` (add `get_price_history`; `upsert_prices` already handles many dates), `refresh.py` (add `refresh_history`). Tests: `tests/pricing/test_history.py`.

- [ ] **Step 1: Failing test** — `_parse_history_json` already yields many rows; assert `fetch_quote_history` parser produces a sorted multi-row series from the recorded `3182.KL.json` fixture; `upsert_prices` stores them; `get_price_history(conn, "3182", start, end)` returns the in-range rows ascending; re-upsert is idempotent (no dupes).

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Implement** `YFinanceProvider.fetch_quote_history(ref, start)` → `yf.Ticker(yf_symbol(ref)).history(start=start.isoformat(), auto_adjust=False)` → reuse `_parse_history_json`. `store.get_price_history(conn, instrument, start, end) -> list[PriceRead]`. `refresh.refresh_history(conn, registry, instruments, start, *, now)` (registry resolves QUOTE_HISTORY providers; yfinance supports it).

- [ ] **Step 4: Run → pass.** mypy + ruff clean.

- [ ] **Step 5: Commit** — `"feat(pricing): historical daily backfill (fetch/store/read + refresh_history)"`.

---

# PHASE C — dividend / ex-dividend reference data

## Task C1: dividend-event store

**Files:** Modify `store.py` (`upsert_dividend_events`, `get_dividend_events`). Tests: extend `test_store.py`.

- [ ] **Step 1: Failing test** — upsert a `DividendEvent` list (idempotent on `(instrument, ex_date)`); `get_dividend_events(conn, instrument)` returns them ascending by `ex_date`; Decimals round-trip.

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Implement** the two functions (mirror price upsert; nullable amounts via `_opt`; currency stored as `.value`).

- [ ] **Step 4: Run → pass.** mypy + ruff clean. **Commit** — `"feat(pricing): dividend_events store (reference data)"`.

## Task C2: FinMind provider (dividends, keyed) + yfinance dividends fallback

**Files:** Create `finmind_provider.py`; modify `yfinance_provider.py` (`fetch_dividends`). Tests: `tests/pricing/test_finmind_provider.py`. Parse the recorded fixture `tests/pricing/fixtures/finmind/TaiwanStockDividend_2330.json` (validated 2026-06-08).

- [ ] **Step 1: Failing test** — `FinMindProvider()._parse_dividends(payload, instrument="2330")` → `list[DividendEvent]` with `ex_date` from `CashExDividendTradingDate`, `pay_date` from `CashDividendPaymentDate`, `cash_amount` from `CashEarningsDistribution` (`Decimal(str)`), `currency=TWD`, `source="finmind"`. `supports(DIVIDEND, Market.TW)` true; token gating: `supports` false when no token configured.

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Implement** `FinMindProvider` (reads token from `Settings`/env; `supports(DIVIDEND, TW)` only when token present; `_parse_dividends` pure, tested against the fixture; `fetch_dividends` calls the FinMind REST endpoint — port from `scripts/probe/adapters/finmind_src.py`). Add `YFinanceProvider.fetch_dividends` (from `yf.Ticker(sym).dividends`: ex_date→amount; `currency` per market) as the US/MY/TW fallback.

- [ ] **Step 4: Run → pass** (FinMind parser test uses the recorded fixture; no live token needed). mypy + ruff clean.

- [ ] **Step 5: Commit** — `"feat(pricing): FinMind dividend provider + yfinance dividends fallback"`.

## Task C3: `refresh_dividends` orchestrator + read

**Files:** Modify `refresh.py` (`refresh_dividends`), `shared/config.py` (dividend provider order: `["finmind","yfinance"]`). Tests: `tests/pricing/test_refresh_dividends.py`.

- [ ] **Step 1: Failing test** — `refresh_dividends(conn, registry, instruments, *, now)` with fake providers upserts events + returns `RefreshSummary`; `get_dividend_events` reads them; a TW instrument routes to FinMind-first then yfinance fallback; all-fail → `failed`, no raise.

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Implement** `refresh_dividends` (registry resolves DIVIDEND chain; store upsert). Add the config default order.

- [ ] **Step 4: Run → pass.** Full suite + mypy + ruff clean.

- [ ] **Step 5: Commit** — `"feat(pricing): refresh_dividends orchestrator + dividend provider order"`.

---

## Done criteria

- All phase A/B/C tests green; `mypy --strict` clean over `portfolio_dash` + `tests`; `ruff` clean.
- `prices`/`fx_rates`/`dividend_events` created; idempotent upserts; reads return last-known + staleness.
- Provider chain is config-ordered, capability-aware, falls back, records `source`, and degrades gracefully (no raise on all-fail; `RefreshSummary.failed` lists misses).
- Providers tested against the probe's recorded fixtures — **no live network in tests**.
- `pricing/` imports only `shared/*`; never writes the ledger; dividend events are reference-only.
- Live smoke (manual, optional): a short script calling `refresh_quotes`/`refresh_history`/`refresh_dividends` against the real network + a FinMind token in `.env`, confirming rows land in a scratch SQLite file. Not part of the test suite.

## Notes for the executor

- Verify `shared/money.py` helper names (`to_db`/`from_db` or equivalent) and `shared/config.py` Settings shape before A2/A6; match the existing conventions.
- Providers' **parsing** is pure and unit-tested against fixtures; their **network** methods are thin and exercised only by the optional live smoke, not the suite.
- Keep adapter URLs/columns identical to the probe's verified ones (`scripts/probe/adapters/*`).
- `results.py`: use the shared **`Money`** annotated type (`from portfolio_dash.shared.models.types import Money`) for **all** Decimal money fields (close/open/high/low/volume/rate/cash_amount/stock_amount/value) instead of the `Num`/repeated `Field(allow_inf_nan=False)` shown — do **not** reuse a single `Field()` instance across fields (fragile in Pydantic v2). Keep `Field(default_factory=...)` only for `RefreshSummary`'s `ok`/`failed`. Optional money = `Money | None = None`.
- `_parse_history_json` epoch→date: use `datetime.fromtimestamp(ms/1000, tz=UTC).date()` (not the deprecated `utcfromtimestamp`).
