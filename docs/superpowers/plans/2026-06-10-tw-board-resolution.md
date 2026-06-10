# TW Board Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve and store each TW instrument's board (TWSE vs TPEx) at registration — `Instrument` gains a `board` field, a `pricing` probe guesses TW board from TWSE→TPEx, and `data_ingestion.register_instrument` fills board (US/MY deterministic, TW via an injected prober) and persists it on confirm.

**Architecture:** `Instrument.board` becomes a stored attribute (`store.py` r/w). `pricing/board.py` probes via the existing TWSE/TPEx providers (structural Protocol, injectable). `data_ingestion/register.py` stays decoupled from `pricing` by taking the prober as an injected `Callable` (mirroring `resolve`'s `llm_resolver`); an unresolved TW board is a soft `board_unresolved` flag, never blocking. The listing/confirm UI is deferred to `web_ui/`.

**Tech Stack:** Python 3.12, Pydantic v2, stdlib sqlite3, pytest, mypy strict, ruff. Run ALL gates with `./.venv/Scripts/python.exe`.

---

## File Structure

- Modify `portfolio_dash/shared/models/assets.py` — `Instrument` gains `board: str = ""`.
- Modify `portfolio_dash/data_ingestion/store.py` — `upsert_instrument`/`get_instrument`/`list_instruments`/`_row_to_instrument` read & write `board`.
- Create `portfolio_dash/pricing/board.py` — `probe_tw_board` (TWSE→TPEx, injectable, graceful).
- Create `portfolio_dash/data_ingestion/register.py` — `register_instrument` + `InstrumentDraft`.
- Tests: `tests/data_ingestion/test_instruments.py`, `tests/pricing/test_board.py`, `tests/data_ingestion/test_register.py`.

---

### Task 1: `Instrument.board` field + store read/write

**Files:**
- Modify: `portfolio_dash/shared/models/assets.py`, `portfolio_dash/data_ingestion/store.py`
- Test: `tests/data_ingestion/test_instruments.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/data_ingestion/test_instruments.py
import sqlite3

from portfolio_dash.data_ingestion.store import (
    get_instrument,
    list_instruments,
    upsert_instrument,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument


def test_instrument_board_defaults_empty() -> None:
    inst = Instrument(
        symbol="AAPL", market=Market.US, quote_ccy=Currency.USD, sector="Tech", name="Apple"
    )
    assert inst.board == ""


def test_upsert_get_persists_board(conn: sqlite3.Connection) -> None:
    upsert_instrument(
        conn,
        Instrument(
            symbol="8299", market=Market.TW, quote_ccy=Currency.TWD,
            sector="Tech", name="X", board="TPEx",
        ),
    )
    got = get_instrument(conn, "8299")
    assert got is not None and got.board == "TPEx"


def test_legacy_null_board_reads_as_empty(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO instruments (symbol, market, quote_ccy, sector, name) "
        "VALUES ('2330','TW','TWD','Tech','TSMC')"
    )
    conn.commit()
    got = get_instrument(conn, "2330")
    assert got is not None and got.board == ""
    assert [i.symbol for i in list_instruments(conn)] == ["2330"]
```

- [ ] **Step 2: Run, verify FAIL**

Run: `./.venv/Scripts/python.exe -m pytest tests/data_ingestion/test_instruments.py -v`
Expected: FAIL — `Instrument` has no `board` (and store doesn't persist it).

- [ ] **Step 3: Implementation**

In `portfolio_dash/shared/models/assets.py`, add `board` to `Instrument`:
```python
class Instrument(BaseModel):
    """A tradable instrument; knows its market and quote currency."""

    symbol: str
    market: Market
    quote_ccy: Currency
    sector: str
    name: str
    board: str = ""  # "TWSE" | "TPEx" | ".KL" | "" (US / unresolved)
```

In `portfolio_dash/data_ingestion/store.py`, update the three instrument functions:
```python
def upsert_instrument(conn: sqlite3.Connection, inst: Instrument) -> None:
    """Insert or update an instrument row (idempotent)."""
    conn.execute(
        """INSERT INTO instruments (symbol, market, quote_ccy, sector, name, board)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(symbol) DO UPDATE SET
               market=excluded.market, quote_ccy=excluded.quote_ccy,
               sector=excluded.sector, name=excluded.name, board=excluded.board""",
        (
            inst.symbol, inst.market.value, inst.quote_ccy.value,
            inst.sector, inst.name, inst.board,
        ),
    )
    conn.commit()


def _row_to_instrument(row: sqlite3.Row) -> Instrument:
    return Instrument(
        symbol=row["symbol"],
        market=Market(row["market"]),
        quote_ccy=Currency(row["quote_ccy"]),
        sector=row["sector"],
        name=row["name"],
        board=row["board"] or "",
    )


def get_instrument(conn: sqlite3.Connection, symbol: str) -> Instrument | None:
    """Return a single instrument by exact symbol, or None if not found."""
    row = conn.execute(
        "SELECT symbol, market, quote_ccy, sector, name, board FROM instruments WHERE symbol=?",
        (symbol,),
    ).fetchone()
    return _row_to_instrument(row) if row is not None else None


def list_instruments(conn: sqlite3.Connection) -> list[Instrument]:
    """Return all instruments in the database."""
    rows = conn.execute(
        "SELECT symbol, market, quote_ccy, sector, name, board FROM instruments"
    ).fetchall()
    return [_row_to_instrument(r) for r in rows]
```

- [ ] **Step 4: Run, verify PASS**

Run: `./.venv/Scripts/python.exe -m pytest tests/data_ingestion/test_instruments.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Gates + commit**

`./.venv/Scripts/python.exe -m pytest -q` (full green — the defaulted field must not break existing instrument tests), `-m mypy`, `-m ruff check portfolio_dash/shared/models/assets.py portfolio_dash/data_ingestion/store.py tests/data_ingestion/test_instruments.py`.
```bash
git add portfolio_dash/shared/models/assets.py portfolio_dash/data_ingestion/store.py tests/data_ingestion/test_instruments.py
git commit -m "feat(data_ingestion): Instrument.board field + store read/write

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: `pricing/board.py` — `probe_tw_board`

**Files:**
- Create: `portfolio_dash/pricing/board.py`
- Test: `tests/pricing/test_board.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/pricing/test_board.py
from datetime import date
from decimal import Decimal

from portfolio_dash.pricing.board import probe_tw_board
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.shared.enums import Market


class _FakeProvider:
    def __init__(self, known: set[str]) -> None:
        self._known = known

    def fetch_quote_latest(self, instruments: list[InstrumentRef]) -> list[PriceRow]:
        return [
            PriceRow(
                instrument=r.symbol, market=Market.TW, as_of=date(2026, 6, 10),
                close=Decimal("1"), source="fake",
            )
            for r in instruments
            if r.symbol in self._known
        ]


class _BoomProvider:
    def fetch_quote_latest(self, instruments: list[InstrumentRef]) -> list[PriceRow]:
        raise RuntimeError("network down")


def test_probe_twse() -> None:
    assert probe_tw_board("2330", twse=_FakeProvider({"2330"}), tpex=_FakeProvider(set())) == "TWSE"


def test_probe_tpex() -> None:
    assert probe_tw_board("8299", twse=_FakeProvider(set()), tpex=_FakeProvider({"8299"})) == "TPEx"


def test_probe_none_when_unknown() -> None:
    assert probe_tw_board("9999", twse=_FakeProvider(set()), tpex=_FakeProvider(set())) is None


def test_probe_graceful_on_provider_error() -> None:
    # TWSE errors -> treated as not-found -> falls through to TPEx
    assert probe_tw_board("8299", twse=_BoomProvider(), tpex=_FakeProvider({"8299"})) == "TPEx"
```

- [ ] **Step 2: Run, verify FAIL**

Run: `./.venv/Scripts/python.exe -m pytest tests/pricing/test_board.py -v`
Expected: FAIL — `ModuleNotFoundError: portfolio_dash.pricing.board`.

- [ ] **Step 3: Implementation — create `portfolio_dash/pricing/board.py`:**

```python
"""Probe a TW instrument's board (TWSE vs TPEx) by trying each source's quote endpoint.

Used at instrument registration to resolve ``instruments.board`` once. Reuses the
TWSE/TPEx providers; both ignore the ``InstrumentRef.board`` field (each *is* a board),
so a probe ref with an empty board is fine. Injectable for tests (no live network).
"""

from typing import Protocol

from portfolio_dash.pricing.providers.tpex_provider import TpexProvider
from portfolio_dash.pricing.providers.twse_provider import TwseProvider
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.shared.enums import Market


class _QuoteProber(Protocol):
    def fetch_quote_latest(self, instruments: list[InstrumentRef]) -> list[PriceRow]: ...


def _has(provider: _QuoteProber, symbol: str) -> bool:
    ref = InstrumentRef(symbol=symbol, market=Market.TW, board="")
    try:
        return bool(provider.fetch_quote_latest([ref]))
    except Exception:  # noqa: BLE001 — network/HTTP error -> treat as "not found here"
        return False


def probe_tw_board(
    symbol: str, *, twse: _QuoteProber | None = None, tpex: _QuoteProber | None = None
) -> str | None:
    """Return ``"TWSE"`` / ``"TPEx"`` for a TW *symbol*, or ``None`` if neither lists it."""
    twse = twse if twse is not None else TwseProvider()
    tpex = tpex if tpex is not None else TpexProvider()
    if _has(twse, symbol):
        return "TWSE"
    if _has(tpex, symbol):
        return "TPEx"
    return None
```

- [ ] **Step 4: Run, verify PASS**

Run: `./.venv/Scripts/python.exe -m pytest tests/pricing/test_board.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Gates + commit**

`./.venv/Scripts/python.exe -m pytest -q`, `-m mypy`, `-m ruff check portfolio_dash/pricing/board.py tests/pricing/test_board.py`.
```bash
git add portfolio_dash/pricing/board.py tests/pricing/test_board.py
git commit -m "feat(pricing): probe_tw_board (TWSE->TPEx board probe, injectable, graceful)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: `data_ingestion/register.py` — `register_instrument`

**Files:**
- Create: `portfolio_dash/data_ingestion/register.py`
- Test: `tests/data_ingestion/test_register.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/data_ingestion/test_register.py
import sqlite3

from portfolio_dash.data_ingestion.register import register_instrument
from portfolio_dash.data_ingestion.store import get_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument


def _inst(symbol: str, market: Market, ccy: Currency, board: str = "") -> Instrument:
    return Instrument(
        symbol=symbol, market=market, quote_ccy=ccy, sector="X", name=symbol, board=board
    )


def test_us_board_empty_no_flag(conn: sqlite3.Connection) -> None:
    d = register_instrument(conn, _inst("AAPL", Market.US, Currency.USD), confirm=True)
    assert d.instrument.board == "" and not d.issues and d.written
    got = get_instrument(conn, "AAPL")
    assert got is not None and got.board == ""


def test_my_board_kl(conn: sqlite3.Connection) -> None:
    d = register_instrument(conn, _inst("3182", Market.MY, Currency.MYR), confirm=True)
    assert d.instrument.board == ".KL" and d.written


def test_tw_board_probed(conn: sqlite3.Connection) -> None:
    d = register_instrument(
        conn, _inst("8299", Market.TW, Currency.TWD), prober=lambda s: "TPEx", confirm=True
    )
    assert d.instrument.board == "TPEx" and not d.issues
    got = get_instrument(conn, "8299")
    assert got is not None and got.board == "TPEx"


def test_tw_unresolved_flagged_but_writes(conn: sqlite3.Connection) -> None:
    d = register_instrument(
        conn, _inst("9999", Market.TW, Currency.TWD), prober=lambda s: None, confirm=True
    )
    assert d.instrument.board == ""
    assert any(i.kind == "board_unresolved" for i in d.issues)
    assert d.written  # soft flag does not block registration


def test_no_confirm_does_not_write(conn: sqlite3.Connection) -> None:
    d = register_instrument(
        conn, _inst("2330", Market.TW, Currency.TWD), prober=lambda s: "TWSE", confirm=False
    )
    assert d.instrument.board == "TWSE" and not d.written
    assert get_instrument(conn, "2330") is None


def test_preset_board_respected_no_probe(conn: sqlite3.Connection) -> None:
    calls: list[str] = []

    def prober(symbol: str) -> str | None:
        calls.append(symbol)
        return "TWSE"

    d = register_instrument(
        conn, _inst("8299", Market.TW, Currency.TWD, board="TPEx"), prober=prober, confirm=True
    )
    assert d.instrument.board == "TPEx" and calls == []  # pre-set board respected; prober not called
```

- [ ] **Step 2: Run, verify FAIL**

Run: `./.venv/Scripts/python.exe -m pytest tests/data_ingestion/test_register.py -v`
Expected: FAIL — `ModuleNotFoundError: portfolio_dash.data_ingestion.register`.

- [ ] **Step 3: Implementation — create `portfolio_dash/data_ingestion/register.py`:**

```python
"""Instrument registration: resolve board, then persist on confirm.

`register_instrument` fills the instrument's board (US/MY deterministic; TW via an
**injected** prober, so this module stays decoupled from `pricing`) and upserts it
when confirmed. An unresolved TW board is a soft ``board_unresolved`` flag that does
not block registration (the work-list's TWSE fallback keeps quotes working until the
user sets it). The listing/confirm UI is `web_ui/`.
"""

import sqlite3
from collections.abc import Callable

from pydantic import BaseModel, Field

from portfolio_dash.data_ingestion.store import upsert_instrument
from portfolio_dash.data_ingestion.validate import Issue
from portfolio_dash.shared.enums import Market
from portfolio_dash.shared.models.assets import Instrument

BoardProber = Callable[[str], str | None]

_MARKET_DEFAULT_BOARD: dict[Market, str] = {Market.US: "", Market.MY: ".KL"}


class InstrumentDraft(BaseModel):
    """Outcome of a registration attempt (preview when not confirmed, else written)."""

    instrument: Instrument
    issues: list[Issue] = Field(default_factory=list)
    written: bool = False


def register_instrument(
    conn: sqlite3.Connection,
    instrument: Instrument,
    *,
    prober: BoardProber | None = None,
    confirm: bool = False,
) -> InstrumentDraft:
    """Resolve the instrument's board and (on confirm) persist it.

    A non-empty ``instrument.board`` is respected as-is (a user-confirmed/edited value;
    the prober is not called). Otherwise: US/MY get their deterministic board; TW is
    probed via *prober* if given. A TW instrument left without a board gets a soft
    ``board_unresolved`` issue but still writes on confirm.
    """
    board = instrument.board
    if not board:
        if instrument.market in _MARKET_DEFAULT_BOARD:
            board = _MARKET_DEFAULT_BOARD[instrument.market]
        elif instrument.market is Market.TW and prober is not None:
            board = prober(instrument.symbol) or ""

    issues: list[Issue] = []
    if instrument.market is Market.TW and not board:
        issues.append(
            Issue(
                kind="board_unresolved",
                needs_confirm=True,
                message=f"could not resolve TW board for {instrument.symbol!r}; set it manually",
            )
        )

    inst = instrument.model_copy(update={"board": board})
    written = False
    hard = [i for i in issues if not i.needs_confirm]
    if confirm and not hard:
        upsert_instrument(conn, inst)
        written = True
    return InstrumentDraft(instrument=inst, issues=issues, written=written)
```

- [ ] **Step 4: Run, verify PASS**

Run: `./.venv/Scripts/python.exe -m pytest tests/data_ingestion/test_register.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: FULL gates + commit**

`./.venv/Scripts/python.exe -m pytest -q` (all green), `-m mypy`, `-m ruff check .`.
```bash
git add portfolio_dash/data_ingestion/register.py tests/data_ingestion/test_register.py
git commit -m "feat(data_ingestion): register_instrument (board resolution + confirm-to-persist)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Final review (after all tasks)

- [ ] Holistic review: `data_ingestion/register.py` imports no `pricing` (prober injected); `Instrument.board` defaulted so nothing else breaks; `board_unresolved` is soft (never blocks); `probe_tw_board` is graceful on provider error.
- [ ] `./.venv/Scripts/python.exe -m pytest -q`, `-m mypy`, `-m ruff check .` — all green.
- [ ] `CHANGELOG.md` `[Unreleased]` entry; `grep -c "^## \[v" CHANGELOG.md` still `1`.
- [ ] `LESSONS_LEARNED.md` updated if anything was learned the hard way.
- [ ] Then use **superpowers:finishing-a-development-branch**.
