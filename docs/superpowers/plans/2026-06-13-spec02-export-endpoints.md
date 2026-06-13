# Spec 02 — Reconciliation-Grade Export Endpoints Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add five reconciliation-grade export endpoints (`POST /api/export/{holdings,ledgers,llm-usage,job-runs,tax-package}`) that emit CSV/zip files at raw `Decimal` precision (UTF-8 **with BOM**, **CRLF**, `Content-Disposition: attachment`), each writing a `job_runs` audit row.

**Architecture:** A new consumer-layer module `portfolio_dash/export/` assembles artifacts by reusing the calc core (`build_dashboard`, `build_book`, forex helpers) and raw ledger/usage/job_runs reads — it computes **no numbers of record**. A thin `api/routers/export.py` orchestrates: build artifact → write audit row → return a bytes `Response`. Dependency direction stays one-way: `web_ui → export → {portfolio, forex, pricing, data_ingestion, scheduler, shared}`; nothing lower imports `export`.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, sqlite3, `decimal.Decimal`, stdlib `csv`/`io`/`zipfile`/`json`, pytest + FastAPI TestClient.

---

## Reconciliation decisions (read before starting)

These are settled for this spec and must be implemented as written:

1. **Audit row has no `kind` column.** Spec 02 §3 says "寫一筆 `job_runs`（kind=export）". But spec 15.0's schema extension adds `payload/reason/cost_usd` to `job_runs` and puts `kind` on **`schedule_config`**, *not* on `job_runs`. So export audit rows are written as `job_runs` rows with a **namespaced `job_id`** — `export:holdings`, `export:ledgers`, `export:llm_usage`, `export:job_runs`, `export:tax_package` — `status="ok"`, `started_at`/`finished_at` = now, `detail` = a short summary. No schema change to `job_runs`.
2. **Reuse `build_dashboard` for holdings; do not recompute.** The holdings snapshot reuses `build_dashboard(conn, now, reporting)` output. `reporting_ccy_value` per holding = `convert(market_value, rate(quote_ccy → reporting))` using the promoted public `RateResolver`.
3. **`RealizedRow` gains `sell_date`.** The tax package cuts realized gains by sell year, but `RealizedRow` carries no date today. Add `sell_date: date` (Task 1). This is a domain-model enrichment, **not** an accounting-semantics change — no human sign-off needed, but record it in CHANGELOG.
4. **FX realized detail is derived, single-sourced.** Add `forex.fx_pnl.realized_fx_rows(...)` returning per-reconversion rows with dates; the existing `_realized_fx` aggregate must be refactored to sum over it (so the formula lives in exactly one place).
5. **Per-currency, never summed.** Tax-package dividends and realized gains list each currency separately; no cross-currency total is ever emitted (locked invariant: no money mixing across currencies without explicit FX).
6. **Year-cut by trade date** (spec 02 §impl-2): realized → sell date; dividends → `date`; FX realized → conversion `date`.

---

## File structure

- Create `portfolio_dash/export/__init__.py` — empty package marker.
- Create `portfolio_dash/export/artifact.py` — `ExportArtifact` dataclass + CSV/zip writer helpers (BOM, CRLF, raw strings).
- Create `portfolio_dash/export/holdings.py` — holdings snapshot builder.
- Create `portfolio_dash/export/ledgers.py` — four-ledger zip builder.
- Create `portfolio_dash/export/usage.py` — llm-usage + job-runs CSV builders.
- Create `portfolio_dash/export/tax.py` — annual tax-package zip builder.
- Create `portfolio_dash/api/routers/export.py` — the five POST routes (thin).
- Modify `portfolio_dash/portfolio/results.py` — add `RealizedRow.sell_date`.
- Modify `portfolio_dash/portfolio/cost_basis.py` — pass `sell_date=ev.trade_date`.
- Modify `portfolio_dash/portfolio/dashboard.py` — rename `_RateResolver` → public `RateResolver`.
- Modify `portfolio_dash/forex/fx_pnl.py` — add `realized_fx_rows`; refactor `_realized_fx`.
- Modify `portfolio_dash/scheduler/jobs.py` — add `log_export_run`.
- Modify `portfolio_dash/api/routers/symbol.py` — add `sell_date` to `_realized_wire`.
- Modify `portfolio_dash/api/app.py` — mount the export router.
- Tests under `tests/` (paths per task).

---

### Task 1: `RealizedRow.sell_date` calc-core enrichment

**Files:**
- Modify: `portfolio_dash/portfolio/results.py` (RealizedRow)
- Modify: `portfolio_dash/portfolio/cost_basis.py:88-99` (SELL branch)
- Modify: `portfolio_dash/api/routers/symbol.py:176-187` (`_realized_wire`)
- Test: `tests/portfolio/test_cost_basis.py` (add a case); fix any breakage in `tests/contract/test_symbol_api.py` and dashboard contract tests.

- [ ] **Step 1: Write the failing test** — append to `tests/portfolio/test_cost_basis.py`:

```python
def test_realized_row_carries_sell_date() -> None:
    """A realized row records the sell transaction's trade_date (for year-cut tax export)."""
    from datetime import date
    from decimal import Decimal

    from portfolio_dash.portfolio.cost_basis import build_book
    from portfolio_dash.shared.enums import Currency, Market
    from portfolio_dash.shared.models.assets import Instrument
    from portfolio_dash.shared.models.enums import Side
    from portfolio_dash.shared.models.ledger import Transaction

    inst = {"AAPL": Instrument(symbol="AAPL", market=Market.US,
                               quote_ccy=Currency.USD, sector="Tech", name="Apple")}
    txs = [
        Transaction(account_id="schwab", symbol="AAPL", side=Side.BUY,
                    quantity=Decimal("10"), price=Decimal("100"),
                    fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 10)),
        Transaction(account_id="schwab", symbol="AAPL", side=Side.SELL,
                    quantity=Decimal("4"), price=Decimal("130"),
                    fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 5, 20)),
    ]
    book = build_book(txs, [], [], inst)
    assert len(book.realized.rows) == 1
    assert book.realized.rows[0].sell_date == date(2026, 5, 20)
```

- [ ] **Step 2: Run it, expect failure** — `.venv/Scripts/python -m pytest tests/portfolio/test_cost_basis.py::test_realized_row_carries_sell_date -v` → FAIL (RealizedRow has no `sell_date`).

- [ ] **Step 3: Add the field** — in `portfolio_dash/portfolio/results.py`, add `from datetime import date` and a field to `RealizedRow`:

```python
class RealizedRow(BaseModel):
    """One realized event from a sell."""

    account_id: str
    symbol: str
    quote_ccy: Currency
    sell_date: date
    shares_sold: Decimal
    proceeds_net: Decimal
    original_cost_removed: Decimal
    adjusted_cost_removed: Decimal
    realized: Decimal
```

- [ ] **Step 4: Populate it** — in `cost_basis.py` SELL branch, add `sell_date=ev.trade_date,` to the `RealizedRow(...)` constructor (right after `symbol`/`quote_ccy`).

- [ ] **Step 5: Propagate to symbol wire** — in `symbol.py` `_realized_wire`, add `"sell_date": r.sell_date.isoformat(),` to the returned dict. Update the wire `dict[str, str]` shape comment.

- [ ] **Step 6: Run the full suite, fix breakage** — `.venv/Scripts/python -m pytest -q`. Any test asserting exact realized-row keys (symbol API, dashboard contract) will now include `sell_date`; update those expectations to include the new field. Do **not** weaken assertions — add the field to the expected shape.

- [ ] **Step 7: Gates + commit** — `.venv/Scripts/python -m mypy --strict portfolio_dash` and `.venv/Scripts/python -m ruff check portfolio_dash tests` clean, then:

```bash
git add portfolio_dash/portfolio/results.py portfolio_dash/portfolio/cost_basis.py portfolio_dash/api/routers/symbol.py tests/portfolio/test_cost_basis.py tests/contract/test_symbol_api.py
git commit -m "feat(portfolio): RealizedRow.sell_date (year-cut for tax export) (spec 02)"
```

---

### Task 2: export artifact + writer helpers + audit row

**Files:**
- Create: `portfolio_dash/export/__init__.py` (empty)
- Create: `portfolio_dash/export/artifact.py`
- Modify: `portfolio_dash/scheduler/jobs.py` (add `log_export_run`)
- Test: `tests/export/test_artifact.py`, `tests/scheduler/test_export_audit.py`

- [ ] **Step 1: Write failing tests** — `tests/export/test_artifact.py`:

```python
import csv
import io
import zipfile

from portfolio_dash.export.artifact import ExportArtifact, csv_artifact, zip_artifact

def test_csv_has_bom_and_crlf_and_raw_decimals() -> None:
    art = csv_artifact(
        "x.csv",
        header=["symbol", "shares"],
        rows=[["2330", "1000.000000"], ["AAPL", "10"]],
        footer_lines=["as_of=2026-06-11"],
    )
    assert isinstance(art, ExportArtifact)
    assert art.filename == "x.csv"
    assert art.media_type == "text/csv; charset=utf-8"
    assert art.content[:3] == b"\xef\xbb\xbf"          # UTF-8 BOM
    text = art.content[3:].decode("utf-8")
    assert "\r\n" in text                               # CRLF
    assert "1000.000000" in text                        # raw decimal, untouched
    assert "# as_of=2026-06-11\r\n" in text             # footer comment line
    body = text[: text.index("# ")]
    parsed = list(csv.reader(io.StringIO(body)))
    assert parsed[0] == ["symbol", "shares"]

def test_zip_round_trips_named_files() -> None:
    art = zip_artifact("bundle.zip", {"a.csv": b"hi", "m.json": b"{}"})
    assert art.media_type == "application/zip"
    with zipfile.ZipFile(io.BytesIO(art.content)) as zf:
        assert set(zf.namelist()) == {"a.csv", "m.json"}
        assert zf.read("a.csv") == b"hi"
```

`tests/scheduler/test_export_audit.py`:

```python
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from portfolio_dash.scheduler.jobs import create_scheduler_tables, log_export_run

def test_log_export_run_writes_namespaced_job_runs_row() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_scheduler_tables(conn)
    now = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))
    run_id = log_export_run(conn, "holdings", now=now, detail="rows=2 bytes=128")
    row = conn.execute("SELECT * FROM job_runs WHERE id = ?", (run_id,)).fetchone()
    assert row["job_id"] == "export:holdings"
    assert row["status"] == "ok"
    assert row["started_at"] == now.isoformat()
    assert row["finished_at"] == now.isoformat()
    assert row["detail"] == "rows=2 bytes=128"
```

- [ ] **Step 2: Run, expect failure** — `.venv/Scripts/python -m pytest tests/export/test_artifact.py tests/scheduler/test_export_audit.py -v` → import/attribute errors.

- [ ] **Step 3: Implement `artifact.py`**:

```python
"""Export artifact value object + CSV/zip writers.

Reconciliation-grade output: UTF-8 *with BOM*, CRLF line endings, and raw cell
strings (no rounding/thousands separators — callers pass full-precision Decimal
strings). Display-value export is the frontend's job; this module is the audit file.
"""

import csv
import io
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

_BOM = "﻿"


@dataclass(frozen=True)
class ExportArtifact:
    """A ready-to-serve download: filename, MIME type, and the bytes."""

    filename: str
    media_type: str
    content: bytes


def _csv_text(header: Sequence[str], rows: Iterable[Sequence[str]],
              footer_lines: Sequence[str]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(list(header))
    for row in rows:
        writer.writerow(list(row))
    for line in footer_lines:
        buf.write(f"# {line}\r\n")
    return buf.getvalue()


def csv_artifact(filename: str, *, header: Sequence[str], rows: Iterable[Sequence[str]],
                 footer_lines: Sequence[str] = ()) -> ExportArtifact:
    """Build a UTF-8-with-BOM, CRLF CSV artifact. Footer lines become `# ...` comments."""
    text = _BOM + _csv_text(header, rows, footer_lines)
    return ExportArtifact(filename, "text/csv; charset=utf-8", text.encode("utf-8"))


def csv_blob(header: Sequence[str], rows: Iterable[Sequence[str]],
             footer_lines: Sequence[str] = ()) -> bytes:
    """A standalone CSV byte blob (BOM + CRLF) for embedding inside a zip member."""
    return (_BOM + _csv_text(header, rows, footer_lines)).encode("utf-8")


def zip_artifact(filename: str, files: Mapping[str, bytes]) -> ExportArtifact:
    """Build a zip artifact from member name -> bytes (deterministic member order)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in files:  # insertion order; callers build dicts in stable order
            zf.writestr(name, files[name])
    return ExportArtifact(filename, "application/zip", buf.getvalue())
```

- [ ] **Step 4: Implement `log_export_run`** — in `scheduler/jobs.py`, add (after `run_job`):

```python
def log_export_run(
    conn: sqlite3.Connection, export_type: str, *, now: datetime, detail: str
) -> int:
    """Write a `job_runs` audit row for a completed export (spec 02 §3).

    Exports are not registered jobs, so the row uses a namespaced ``job_id``
    (``export:<type>``) rather than a ``kind`` column — spec 15.0 places ``kind`` on
    ``schedule_config``, not ``job_runs``. ``started_at`` == ``finished_at`` (synchronous).
    """
    ts = now.isoformat()
    cur = conn.execute(
        "INSERT INTO job_runs (job_id, started_at, finished_at, status, detail) "
        "VALUES (?, ?, ?, 'ok', ?)",
        (f"export:{export_type}", ts, ts, detail),
    )
    conn.commit()
    return int(cur.lastrowid or 0)
```

- [ ] **Step 5: Run tests, expect pass.** Create `tests/export/__init__.py` if the suite needs it (match the existing tests layout — check whether `tests/` uses package dirs).

- [ ] **Step 6: Gates + commit**:

```bash
git add portfolio_dash/export/__init__.py portfolio_dash/export/artifact.py portfolio_dash/scheduler/jobs.py tests/export/
git commit -m "feat(export): artifact value object + CSV/zip writers + job_runs audit (spec 02)"
```

---

### Task 3: holdings snapshot endpoint

**Files:**
- Modify: `portfolio_dash/portfolio/dashboard.py` (rename `_RateResolver` → `RateResolver`)
- Create: `portfolio_dash/export/holdings.py`
- Create: `portfolio_dash/api/routers/export.py` (with the `/export/holdings` route)
- Modify: `portfolio_dash/api/app.py` (mount export router)
- Test: `tests/contract/test_export_holdings.py`

- [ ] **Step 1: Promote the resolver** — in `dashboard.py` rename class `_RateResolver` → `RateResolver` (update its one internal use `resolver = _RateResolver(...)` → `RateResolver(...)`). It becomes the shared current-FX resolver (identity → direct → inverse → KeyError).

- [ ] **Step 2: Write the failing contract test** — `tests/contract/test_export_holdings.py`:

```python
from fastapi.testclient import TestClient

def test_export_holdings_csv(api_client: TestClient) -> None:
    r = api_client.post("/api/export/holdings")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert "holdings_snapshot_2026-06-11.csv" in r.headers["content-disposition"]
    body = r.content
    assert body[:3] == b"\xef\xbb\xbf"
    text = body[3:].decode("utf-8")
    header = text.split("\r\n", 1)[0]
    assert header.startswith("symbol,name,market,board,account_id,quote_ccy,shares")
    assert "reporting_ccy_value" in header
    # golden 2330 holding: 1000 sh; reporting_ccy_value is TWD market value (quote=TWD).
    assert ",2330," in text
    # footer comment with as_of + fx_rates + generated
    assert "# as_of=2026-06-11" in text and "fx_rates=" in text

def test_export_holdings_writes_audit_row(api_client: TestClient, golden_db) -> None:
    api_client.post("/api/export/holdings")
    row = golden_db.execute(
        "SELECT * FROM job_runs WHERE job_id = 'export:holdings'"
    ).fetchone()
    assert row is not None and row["status"] == "ok"
```

- [ ] **Step 3: Run, expect failure** — `.venv/Scripts/python -m pytest tests/contract/test_export_holdings.py -v` → 404 (route missing).

- [ ] **Step 4: Implement `export/holdings.py`**:

```python
"""Holdings snapshot export (spec 02). Reuses build_dashboard; computes no numbers."""

import sqlite3
from datetime import datetime
from decimal import Decimal

from portfolio_dash.export.artifact import ExportArtifact, csv_artifact
from portfolio_dash.portfolio.dashboard import RateResolver, build_dashboard
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.fx import convert

_COLUMNS = [
    "symbol", "name", "market", "board", "account_id", "quote_ccy", "shares",
    "original_avg", "adjusted_avg", "original_cost_total", "adjusted_cost_total",
    "market_price", "price_as_of", "price_stale", "market_value", "unrealized_pnl",
    "capital_gain", "dividend_portion", "payback_ratio", "weight", "reporting_ccy_value",
]


def _s(value: object) -> str:
    """Raw cell: Decimal/str/bool/date -> str; None -> empty."""
    return "" if value is None else str(value)


def build_holdings_csv(
    conn: sqlite3.Connection, *, now: datetime, reporting: Currency
) -> ExportArtifact:
    data = build_dashboard(conn, now=now, reporting=reporting)
    resolver = RateResolver(conn, now=now)
    rows: list[list[str]] = []
    for h in data.holdings:
        reporting_value = ""
        if h.market_value is not None:
            try:
                reporting_value = str(convert(h.market_value,
                                              resolver.rate(h.quote_ccy, reporting)))
            except KeyError:
                reporting_value = ""  # missing FX -> blank, never fabricated
        rows.append([
            _s(h.symbol), _s(h.name), h.market.value, _s(h.board), _s(h.account_id),
            h.quote_ccy.value, _s(h.shares), _s(h.original_avg), _s(h.adjusted_avg),
            _s(h.original_cost_total), _s(h.adjusted_cost_total), _s(h.market_price),
            _s(h.price_as_of), _s(h.price_stale), _s(h.market_value), _s(h.unrealized_pnl),
            _s(h.capital_gain), _s(h.dividend_portion), _s(h.payback_ratio), _s(h.weight),
            reporting_value,
        ])
    as_of = data.as_of.date().isoformat()
    fx_rates = _fx_footer(resolver, reporting)
    footer = [f"as_of={as_of}, fx_rates={{{fx_rates}}}, generated={now.isoformat()}"]
    return csv_artifact(f"holdings_snapshot_{as_of}.csv",
                        header=_COLUMNS, rows=rows, footer_lines=footer)


def _fx_footer(resolver: RateResolver, reporting: Currency) -> str:
    """Best-effort current rates for the non-reporting currencies (USD, MYR)."""
    parts: list[str] = []
    for ccy in (Currency.USD, Currency.MYR):
        if ccy == reporting:
            continue
        try:
            parts.append(f"{ccy.value}:{resolver.rate(ccy, reporting)}")
        except KeyError:
            parts.append(f"{ccy.value}:n/a")
    return ", ".join(parts)
```

- [ ] **Step 5: Implement `api/routers/export.py`** (holdings route only for now):

```python
"""POST /api/export/* — reconciliation-grade downloads (spec 02). Thin orchestration.

Each route: build the artifact via portfolio_dash.export.*, write a job_runs audit row
(log_export_run), and return the bytes with a Content-Disposition attachment header.
The web layer computes no numbers of record.
"""

import sqlite3
from datetime import datetime

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from portfolio_dash.api.deps import get_conn, get_now, get_reporting
from portfolio_dash.export.artifact import ExportArtifact
from portfolio_dash.export.holdings import build_holdings_csv
from portfolio_dash.scheduler.jobs import log_export_run
from portfolio_dash.shared.enums import Currency

router = APIRouter()


def _respond(art: ExportArtifact) -> Response:
    return Response(
        content=art.content,
        media_type=art.media_type,
        headers={"Content-Disposition": f'attachment; filename="{art.filename}"'},
    )


@router.post("/export/holdings")
def export_holdings(
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
    reporting: Currency = Depends(get_reporting),
) -> Response:
    art = build_holdings_csv(conn, now=now, reporting=reporting)
    log_export_run(conn, "holdings", now=now,
                   detail=f"rows_bytes={len(art.content)} file={art.filename}")
    return _respond(art)
```

- [ ] **Step 6: Mount the router** — in `app.py` add `export` to the routers import and `app.include_router(export.router, prefix="/api")` (place after `symbol`).

- [ ] **Step 7: Run tests, expect pass.** Verify the golden 2330 holding's `reporting_ccy_value` equals its TWD market value (quote_ccy is TWD, so rate is identity = market_value).

- [ ] **Step 8: Gates + commit**:

```bash
git add portfolio_dash/portfolio/dashboard.py portfolio_dash/export/holdings.py portfolio_dash/api/routers/export.py portfolio_dash/api/app.py tests/contract/test_export_holdings.py
git commit -m "feat(export): POST /api/export/holdings snapshot CSV (spec 02)"
```

---

### Task 4: ledgers zip endpoint

**Files:**
- Create: `portfolio_dash/export/ledgers.py`
- Modify: `portfolio_dash/api/routers/export.py` (add `/export/ledgers`)
- Test: `tests/contract/test_export_ledgers.py`

- [ ] **Step 1: Write the failing test** — `tests/contract/test_export_ledgers.py`:

```python
import io
import json
import zipfile

from fastapi.testclient import TestClient

def test_export_ledgers_zip_members(api_client: TestClient) -> None:
    r = api_client.post("/api/export/ledgers")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert "ledgers_2026-06-11.zip" in r.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = set(zf.namelist())
        assert {"transactions.csv", "dividends.csv", "fx_conversions.csv",
                "opening_inventory.csv", "fee_rules_snapshot.json",
                "manifest.json"} <= names
        # raw table columns as header (transactions DB columns)
        tx = zf.read("transactions.csv")[3:].decode("utf-8")  # strip BOM
        assert tx.split("\r\n", 1)[0].startswith(
            "id,account_id,symbol,side,quantity,price,fees,tax,trade_date")
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["counts"]["transactions"] == 2  # golden: 2330 + AAPL buys
        assert manifest["as_of"] == "2026-06-11"
        fee = json.loads(zf.read("fee_rules_snapshot.json"))
        assert "tw" in fee and "schwab" in fee
```

- [ ] **Step 2: Run, expect failure** (404).

- [ ] **Step 3: Implement `export/ledgers.py`**:

```python
"""Four-ledger zip export (spec 02): raw table dumps + fee rules + manifest."""

import json
import sqlite3
from datetime import datetime

from portfolio_dash.api.serialize import to_wire
from portfolio_dash.data_ingestion.config_seed import FEE_RULES
from portfolio_dash.export.artifact import ExportArtifact, csv_blob, zip_artifact

_LEDGER_TABLES = ["transactions", "dividends", "fx_conversions", "opening_inventory"]
_SCHEMA_VERSION = 1


def _dump_table(conn: sqlite3.Connection, table: str) -> tuple[bytes, int]:
    """Raw `SELECT *` dump: header = DB columns, one row per record. (table is from a
    fixed allow-list, never user input.)"""
    cur = conn.execute(f"SELECT * FROM {table}")
    header = [c[0] for c in cur.description]
    rows = [["" if v is None else str(v) for v in row] for row in cur.fetchall()]
    return csv_blob(header, rows), len(rows)


def build_ledgers_zip(conn: sqlite3.Connection, *, now: datetime) -> ExportArtifact:
    as_of = now.date().isoformat()
    files: dict[str, bytes] = {}
    counts: dict[str, int] = {}
    for table in _LEDGER_TABLES:
        blob, n = _dump_table(conn, table)
        files[f"{table}.csv"] = blob
        counts[table] = n
    fee_snapshot = {name: to_wire(rs.model_dump()) for name, rs in FEE_RULES.items()}
    files["fee_rules_snapshot.json"] = json.dumps(
        fee_snapshot, ensure_ascii=False, indent=2).encode("utf-8")
    manifest = {"as_of": as_of, "schema_version": _SCHEMA_VERSION,
                "generated": now.isoformat(), "counts": counts}
    files["manifest.json"] = json.dumps(
        manifest, ensure_ascii=False, indent=2).encode("utf-8")
    return zip_artifact(f"ledgers_{as_of}.zip", files)
```

- [ ] **Step 4: Add the route** — in `api/routers/export.py` import `build_ledgers_zip` and add:

```python
@router.post("/export/ledgers")
def export_ledgers(
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Response:
    art = build_ledgers_zip(conn, now=now)
    log_export_run(conn, "ledgers", now=now,
                   detail=f"bytes={len(art.content)} file={art.filename}")
    return _respond(art)
```

- [ ] **Step 5: Run tests, expect pass.** Confirm `FeeRuleSet.model_dump()` Decimals serialize as strings via `to_wire` (no floats in the JSON).

- [ ] **Step 6: Gates + commit**:

```bash
git add portfolio_dash/export/ledgers.py portfolio_dash/api/routers/export.py tests/contract/test_export_ledgers.py
git commit -m "feat(export): POST /api/export/ledgers zip (raw ledgers + fee snapshot + manifest) (spec 02)"
```

---

### Task 5: llm-usage + job-runs CSV endpoints

**Files:**
- Create: `portfolio_dash/export/usage.py`
- Modify: `portfolio_dash/api/routers/export.py` (add both routes + a body model)
- Test: `tests/contract/test_export_usage.py`

Range semantics: body `{"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}` (both optional; omitted = unbounded). `from > to` → 400 error envelope (`error_body("validation_error", ..., field="from")`). Filter `llm_usage.ts` / `job_runs.started_at` by the date part of the ISO timestamp (`ts[:10]`).

- [ ] **Step 1: Write the failing test** — `tests/contract/test_export_usage.py`:

```python
from fastapi.testclient import TestClient

def test_export_job_runs_csv(api_client: TestClient) -> None:
    # seed one run via the export itself, then export job-runs
    api_client.post("/api/export/holdings")
    r = api_client.post("/api/export/job-runs", json={})
    assert r.status_code == 200
    assert "job_runs_" in r.headers["content-disposition"]
    text = r.content[3:].decode("utf-8")
    assert text.split("\r\n", 1)[0] == "id,job_id,started_at,finished_at,status,detail"
    assert "export:holdings" in text

def test_export_llm_usage_csv_empty(api_client: TestClient) -> None:
    r = api_client.post("/api/export/llm-usage", json={"from": "2026-01-01", "to": "2026-12-31"})
    assert r.status_code == 200
    assert r.content[3:].decode("utf-8").split("\r\n", 1)[0] == \
        "ts,model,agent,input_tokens,output_tokens,cost"

def test_export_bad_range_400(api_client: TestClient) -> None:
    r = api_client.post("/api/export/job-runs", json={"from": "2026-12-31", "to": "2026-01-01"})
    assert r.status_code == 400
    assert r.json()["error"]["field"] == "from"
```

- [ ] **Step 2: Run, expect failure** (404).

- [ ] **Step 3: Implement `export/usage.py`**:

```python
"""llm_usage + job_runs CSV exports (spec 02). Raw row dumps, date-range filtered."""

import sqlite3
from datetime import datetime

from portfolio_dash.export.artifact import ExportArtifact, csv_artifact

_USAGE_COLS = ["ts", "model", "agent", "input_tokens", "output_tokens", "cost"]
_JOB_COLS = ["id", "job_id", "started_at", "finished_at", "status", "detail"]


def _tag(frm: str | None, to: str | None) -> str:
    return f"{frm or 'all'}_{to or 'all'}"


def _in_range(day: str, frm: str | None, to: str | None) -> bool:
    if frm and day < frm:
        return False
    if to and day > to:
        return False
    return True


def build_llm_usage_csv(
    conn: sqlite3.Connection, *, frm: str | None, to: str | None
) -> ExportArtifact:
    rows: list[list[str]] = []
    for r in conn.execute(
        "SELECT ts, model, agent, input_tokens, output_tokens, cost "
        "FROM llm_usage ORDER BY ts ASC, id ASC"
    ):
        if not _in_range(str(r["ts"])[:10], frm, to):
            continue
        rows.append([str(r["ts"]), str(r["model"]), str(r["agent"]),
                     str(r["input_tokens"]), str(r["output_tokens"]), str(r["cost"])])
    return csv_artifact(f"llm_usage_{_tag(frm, to)}.csv", header=_USAGE_COLS, rows=rows)


def build_job_runs_csv(
    conn: sqlite3.Connection, *, frm: str | None, to: str | None
) -> ExportArtifact:
    rows: list[list[str]] = []
    for r in conn.execute(
        "SELECT id, job_id, started_at, finished_at, status, detail "
        "FROM job_runs ORDER BY started_at ASC, id ASC"
    ):
        if not _in_range(str(r["started_at"])[:10], frm, to):
            continue
        rows.append([str(r["id"]), str(r["job_id"]), str(r["started_at"]),
                     "" if r["finished_at"] is None else str(r["finished_at"]),
                     "" if r["status"] is None else str(r["status"]),
                     "" if r["detail"] is None else str(r["detail"])])
    return csv_artifact(f"job_runs_{_tag(frm, to)}.csv", header=_JOB_COLS, rows=rows)
```

- [ ] **Step 4: Add the routes + body model** — in `api/routers/export.py`:

```python
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from portfolio_dash.api.errors import error_body
from portfolio_dash.export.usage import build_job_runs_csv, build_llm_usage_csv


class RangeBody(BaseModel):
    frm: str | None = Field(default=None, alias="from")
    to: str | None = None
    model_config = {"populate_by_name": True}


def _bad_range(body: RangeBody) -> JSONResponse | None:
    if body.frm and body.to and body.frm > body.to:
        return JSONResponse(status_code=400,
                            content=error_body("validation_error", "日期區間無效", field="from"))
    return None
```

(add `from pydantic import BaseModel, Field` import) and the two routes:

```python
@router.post("/export/llm-usage")
def export_llm_usage(
    body: RangeBody, conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Response:
    bad = _bad_range(body)
    if bad is not None:
        return bad
    art = build_llm_usage_csv(conn, frm=body.frm, to=body.to)
    log_export_run(conn, "llm_usage", now=now, detail=f"file={art.filename}")
    return _respond(art)


@router.post("/export/job-runs")
def export_job_runs(
    body: RangeBody, conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Response:
    bad = _bad_range(body)
    if bad is not None:
        return bad
    art = build_job_runs_csv(conn, frm=body.frm, to=body.to)
    log_export_run(conn, "job_runs", now=now, detail=f"file={art.filename}")
    return _respond(art)
```

Note: `_respond` return type is `Response`; FastAPI allows returning `JSONResponse` (a `Response` subclass) too, so the route return annotation `Response` is fine for the 400 path.

- [ ] **Step 5: Run tests, expect pass.**

- [ ] **Step 6: Gates + commit**:

```bash
git add portfolio_dash/export/usage.py portfolio_dash/api/routers/export.py tests/contract/test_export_usage.py
git commit -m "feat(export): POST /api/export/{llm-usage,job-runs} range CSV (spec 02)"
```

---

### Task 6: annual tax-package zip endpoint

**Files:**
- Modify: `portfolio_dash/forex/fx_pnl.py` (add `realized_fx_rows`; refactor `_realized_fx`)
- Create: `portfolio_dash/export/tax.py`
- Modify: `portfolio_dash/api/routers/export.py` (add `/export/tax-package`)
- Test: `tests/forex/test_fx_pnl.py` (add a case), `tests/contract/test_export_tax.py`

- [ ] **Step 1: Add `realized_fx_rows` to `forex/fx_pnl.py`** with a failing unit test first. Append to `tests/forex/test_fx_pnl.py`:

```python
def test_realized_fx_rows_per_reconversion() -> None:
    from datetime import date
    from decimal import Decimal

    from portfolio_dash.forex.fx_pnl import realized_fx_rows
    from portfolio_dash.shared.enums import Currency
    from portfolio_dash.shared.models.ledger import FXConversion

    convs = [
        FXConversion(account_id="schwab", date=date(2026, 1, 8), from_ccy=Currency.TWD,
                     from_amount=Decimal("32000"), to_ccy=Currency.USD,
                     to_amount=Decimal("1000")),  # acquisition (TWD->USD), avg=32
        FXConversion(account_id="schwab", date=date(2026, 5, 1), from_ccy=Currency.USD,
                     from_amount=Decimal("500"), to_ccy=Currency.TWD,
                     to_amount=Decimal("17000")),  # reconversion: 17000 - 500*32 = +1000
    ]
    rows = realized_fx_rows(convs, Currency.TWD, Currency.USD, Decimal("32"))
    assert len(rows) == 1
    assert rows[0].date == date(2026, 5, 1)
    assert rows[0].foreign_sold == Decimal("500")
    assert rows[0].home_received == Decimal("17000")
    assert rows[0].rate_used == Decimal("32")
    assert rows[0].realized == Decimal("1000")
```

Then implement (add a Pydantic `FxRealizedRow` model to `forex/results.py`, and the function + refactor in `fx_pnl.py`):

In `forex/results.py`:

```python
from datetime import date  # add at top

class FxRealizedRow(BaseModel):
    """One realized-FX event from a reconversion (foreign -> home)."""

    date: date
    foreign_ccy: Currency
    home_ccy: Currency
    foreign_sold: Decimal
    home_received: Decimal
    rate_used: Decimal
    realized: Decimal
```

In `fx_pnl.py`:

```python
def realized_fx_rows(
    conversions: list[FXConversion], home: Currency, foreign: Currency,
    avg_rate: Decimal | None,
) -> list[FxRealizedRow]:
    """Per-reconversion realized FX rows (foreign -> home). Empty if no avg_rate."""
    if avg_rate is None:
        return []
    out: list[FxRealizedRow] = []
    for c in conversions:
        if c.from_ccy == foreign and c.to_ccy == home:
            out.append(FxRealizedRow(
                date=c.date, foreign_ccy=foreign, home_ccy=home,
                foreign_sold=c.from_amount, home_received=c.to_amount,
                rate_used=avg_rate,
                realized=c.to_amount - c.from_amount * avg_rate,
            ))
    return out
```

Refactor `_realized_fx` to reuse it (single-sourced formula):

```python
def _realized_fx(
    conversions: list[FXConversion], home: Currency, foreign: Currency, avg_rate: Decimal | None
) -> Decimal | None:
    if avg_rate is None:
        return None
    return sum((r.realized for r in realized_fx_rows(conversions, home, foreign, avg_rate)), _ZERO)
```

(import `FxRealizedRow` from results.) Run `tests/forex/` to confirm both the new test and existing FX tests pass.

- [ ] **Step 2: Write the failing tax-package contract test** — `tests/contract/test_export_tax.py`. The golden DB has no SELL and no reconversion, so for year 2026: `realized_gains_2026.csv` has only the header; `dividends_2026.csv` has the one 2330 cash dividend (TWD); `fx_realized_2026.csv` header only; `summary.md` lists per-currency subtotals.

```python
import io
import zipfile

from fastapi.testclient import TestClient

def test_export_tax_package(api_client: TestClient) -> None:
    r = api_client.post("/api/export/tax-package", json={"year": 2026})
    assert r.status_code == 200
    assert "tax_package_2026.zip" in r.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = set(zf.namelist())
        assert {"realized_gains_2026.csv", "dividends_2026.csv",
                "fx_realized_2026.csv", "summary.md"} == names
        divs = zf.read("dividends_2026.csv")[3:].decode("utf-8")
        # golden 2330 cash dividend: net 5000 TWD, separate currency column, never summed
        assert divs.split("\r\n", 1)[0].startswith(
            "date,account_id,symbol,type,gross,withholding,net,ccy")
        assert ",2330," in divs and ",5000," in divs and ",TWD" in divs
        realized = zf.read("realized_gains_2026.csv")[3:].decode("utf-8")
        assert realized.split("\r\n", 1)[0].startswith(
            "sell_date,account_id,symbol,quote_ccy,shares_sold,proceeds_net")
        assert "reporting_realized" in realized.split("\r\n", 1)[0]
        assert "rate_used" in realized.split("\r\n", 1)[0]
        summary = zf.read("summary.md").decode("utf-8")
        assert "TWD" in summary

def test_export_tax_bad_year_422(api_client: TestClient) -> None:
    r = api_client.post("/api/export/tax-package", json={"year": 1800})
    assert r.status_code == 422  # pydantic ge/le bound on year
```

- [ ] **Step 3: Run, expect failure** (404).

- [ ] **Step 4: Implement `export/tax.py`**. Reuse `build_book` for realized rows (with `sell_date`), the dividend ledger for dividends, and `realized_fx_rows` for FX. Reporting conversion uses **trade-date FX** (same `get_fx_on` direct→inverse logic as `dashboard.fx_at`); when the trade-date rate is missing, leave `reporting_realized`/`rate_used` blank (never fabricate).

```python
"""Annual tax-package export (spec 02): realized gains, dividends, FX realized, summary.

Year-cut by trade date (sell date / dividend date / conversion date). Per-currency
rows are never summed across currencies. Reporting conversion uses trade-date FX.
"""

import sqlite3
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal

from portfolio_dash.data_ingestion.store import (
    list_accounts, list_dividends, list_fx_conversions, list_instruments,
    list_opening, list_transactions,
)
from portfolio_dash.export.artifact import ExportArtifact, csv_blob, zip_artifact
from portfolio_dash.forex.fx_pnl import realized_fx_rows
from portfolio_dash.forex.pools import average_acquisition_rate
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.pricing.store import get_fx_on
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.enums import DividendType
from portfolio_dash.shared.models.ledger import (
    Dividend, FXConversion, OpeningInventory, Transaction,
)

_ONE = Decimal("1")
_ZERO = Decimal("0")
_REALIZED_COLS = ["sell_date", "account_id", "symbol", "quote_ccy", "shares_sold",
                  "proceeds_net", "original_cost_removed", "adjusted_cost_removed",
                  "realized", "rate_used", "reporting_realized"]
_DIV_COLS = ["date", "account_id", "symbol", "type", "gross", "withholding", "net", "ccy"]
_FX_COLS = ["date", "account_id", "home_ccy", "foreign_ccy", "foreign_sold",
            "home_received", "rate_used", "realized"]


def _rate_on(conn: sqlite3.Connection, d: date, base: Currency,
             quote: Currency) -> Decimal | None:
    if base == quote:
        return _ONE
    direct = get_fx_on(conn, base, quote, on=d)
    if direct is not None:
        return direct.rate
    inverse = get_fx_on(conn, quote, base, on=d)
    if inverse is not None:
        return _ONE / inverse.rate
    return None


def build_tax_package_zip(
    conn: sqlite3.Connection, *, now: datetime, year: int, reporting: Currency
) -> ExportArtifact:
    txs = [Transaction(account_id=s.account_id, symbol=s.symbol, side=s.side,
                       quantity=s.quantity, price=s.price, fees=s.fees, tax=s.tax,
                       trade_date=s.trade_date) for s in list_transactions(conn)]
    divs = [Dividend(account_id=s.account_id, symbol=s.symbol, date=s.date,
                     type=DividendType(s.type), gross=s.gross, withholding=s.withholding,
                     net=s.net, reinvest_shares=s.reinvest_shares,
                     reinvest_price=s.reinvest_price) for s in list_dividends(conn)]
    opening = [OpeningInventory(account_id=s.account_id, symbol=s.symbol, shares=s.shares,
                                original_avg_cost=s.original_avg_cost,
                                original_cost_total=s.original_cost_total,
                                build_date=s.build_date) for s in list_opening(conn)]
    convs = [FXConversion(account_id=s.account_id, date=s.date, from_ccy=s.from_ccy,
                          from_amount=s.from_amount, to_ccy=s.to_ccy,
                          to_amount=s.to_amount) for s in list_fx_conversions(conn)]
    instruments = {i.symbol: i for i in list_instruments(conn)}
    accounts = {a.account_id: a for a in list_accounts(conn)}
    book = build_book(txs, divs, opening, instruments)

    # realized gains for the year
    realized_rows: list[list[str]] = []
    realized_subtotal: dict[Currency, Decimal] = defaultdict(lambda: _ZERO)
    for r in book.realized.rows:
        if r.sell_date.year != year:
            continue
        rate = _rate_on(conn, r.sell_date, r.quote_ccy, reporting)
        reporting_realized = "" if rate is None else str(r.realized * rate)
        realized_rows.append([
            r.sell_date.isoformat(), r.account_id, r.symbol, r.quote_ccy.value,
            str(r.shares_sold), str(r.proceeds_net), str(r.original_cost_removed),
            str(r.adjusted_cost_removed), str(r.realized),
            "" if rate is None else str(rate), reporting_realized,
        ])
        realized_subtotal[r.quote_ccy] += r.realized

    # dividends for the year (per-currency, never summed across currencies)
    div_rows: list[list[str]] = []
    div_subtotal: dict[Currency, Decimal] = defaultdict(lambda: _ZERO)
    for d in divs:
        if d.date.year != year or d.type is DividendType.STOCK:
            continue
        ccy = instruments[d.symbol].quote_ccy
        div_rows.append([
            d.date.isoformat(), d.account_id, d.symbol, d.type.value.lower(),
            "" if d.gross is None else str(d.gross),
            "" if d.withholding is None else str(d.withholding),
            str(d.net), ccy.value,
        ])
        div_subtotal[ccy] += d.net

    # FX realized for the year (per reconversion), grouped by FX-exposed account
    fx_rows: list[list[str]] = []
    fx_subtotal: dict[Currency, Decimal] = defaultdict(lambda: _ZERO)
    for acct in accounts.values():
        if acct.settlement_ccy == acct.funding_ccy:
            continue
        home, foreign = acct.funding_ccy, acct.settlement_ccy
        acct_convs = [c for c in convs if c.account_id == acct.account_id]
        avg = average_acquisition_rate(acct_convs, home, foreign)
        for fr in realized_fx_rows(acct_convs, home, foreign, avg):
            if fr.date.year != year:
                continue
            fx_rows.append([fr.date.isoformat(), acct.account_id, fr.home_ccy.value,
                            fr.foreign_ccy.value, str(fr.foreign_sold),
                            str(fr.home_received), str(fr.rate_used), str(fr.realized)])
            fx_subtotal[fr.home_ccy] += fr.realized

    files: dict[str, bytes] = {
        f"realized_gains_{year}.csv": csv_blob(_REALIZED_COLS, realized_rows),
        f"dividends_{year}.csv": csv_blob(_DIV_COLS, div_rows),
        f"fx_realized_{year}.csv": csv_blob(_FX_COLS, fx_rows),
        "summary.md": _summary_md(year, realized_subtotal, div_subtotal, fx_subtotal),
    }
    return zip_artifact(f"tax_package_{year}.zip", files)


def _subtotal_lines(subtotal: dict[Currency, Decimal]) -> str:
    if not subtotal:
        return "- （無）\n"
    return "".join(f"- {ccy.value}: {amt}\n" for ccy, amt in sorted(
        subtotal.items(), key=lambda kv: kv[0].value))


def _summary_md(year: int, realized: dict[Currency, Decimal],
                dividends: dict[Currency, Decimal],
                fx: dict[Currency, Decimal]) -> bytes:
    md = (
        f"# Tax Package {year}\n\n"
        "Per-currency subtotals (never summed across currencies).\n\n"
        f"## Realized gains\n{_subtotal_lines(realized)}\n"
        f"## Dividends (net)\n{_subtotal_lines(dividends)}\n"
        f"## Realized FX P&L\n{_subtotal_lines(fx)}\n"
    )
    return md.encode("utf-8")
```

- [ ] **Step 5: Add the route + year-bounded body** — in `api/routers/export.py`:

```python
class TaxPackageBody(BaseModel):
    year: int = Field(ge=1900, le=2200)


@router.post("/export/tax-package")
def export_tax_package(
    body: TaxPackageBody, conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now), reporting: Currency = Depends(get_reporting),
) -> Response:
    art = build_tax_package_zip(conn, now=now, year=body.year, reporting=reporting)
    log_export_run(conn, "tax_package", now=now,
                   detail=f"year={body.year} file={art.filename}")
    return _respond(art)
```

(import `build_tax_package_zip`.)

- [ ] **Step 6: Run tests, expect pass.** Confirm: golden dividends row shows TWD column with net 5000 and is NOT summed with any other currency; realized + fx CSVs are header-only; `summary.md` shows `- TWD: 5000` under Dividends.

- [ ] **Step 7: Gates + commit**:

```bash
git add portfolio_dash/forex/results.py portfolio_dash/forex/fx_pnl.py portfolio_dash/export/tax.py portfolio_dash/api/routers/export.py tests/forex/test_fx_pnl.py tests/contract/test_export_tax.py
git commit -m "feat(export): POST /api/export/tax-package annual zip (year-cut, per-ccy, FX attribution) (spec 02)"
```

---

## CHANGELOG (do at the end, before the final review)

Add to `[Unreleased]` Added:
- `portfolio_dash/export/` module + `POST /api/export/{holdings,ledgers,llm-usage,job-runs,tax-package}` (spec 02): raw-Decimal CSV/zip, UTF-8 BOM + CRLF, `Content-Disposition: attachment`, `job_runs` audit rows (`job_id=export:*`).
- `RealizedRow.sell_date` (calc-core enrichment for year-cut tax export).
- `forex.fx_pnl.realized_fx_rows` (single-sourced realized-FX detail; `_realized_fx` now sums over it).

Record the reconciliations:
- Spec 02 §3 "kind=export" implemented as a namespaced `job_id` (`export:*`), because spec 15.0 puts `kind` on `schedule_config`, not `job_runs`.
- New `portfolio_dash/export/` consumer-layer module added to the module map (web_ui → export → lower layers; nothing lower imports it).

Verify integrity: `grep -c "^## \[v" CHANGELOG.md` must equal the version count (1 — only `[Unreleased]` + no released versions yet, so 0 `[v` lines; confirm the count is unchanged from before this edit).

## Self-review (controller, after all tasks)

1. Spec coverage: 5 endpoints + raw Decimal + BOM/CRLF + attachment + audit rows + tax per-ccy-never-summed + year-cut by trade date — all present.
2. Money discipline: every cell is `str(Decimal)`; no float anywhere; FX conversion only via `shared.fx.convert` / `get_fx_on`.
3. Boundary: `export/` imports only lower layers; routers stay thin; no calc of record in the web layer.
4. Determinism: all builders take `conn`/`now` injected; contract tests run against the golden DB + frozen clock; no network.
