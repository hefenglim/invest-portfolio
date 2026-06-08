# Data-Source Availability Probe — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Empirically measure each candidate finance data source (per data type × market) and emit a ranked primary/fallback recommendation + recorded fixtures that feed the future `pricing/` module.

**Architecture:** A small probe harness (typed, unit-tested) + per-source adapters (live-network, exploratory) living in `scripts/probe/` — **outside** the `portfolio_dash/` product package. Each adapter: fetch live → save raw response as a fixture → write a parser tested against that fixture → emit a `ProbeResult`. The harness aggregates results into a markdown comparison report. **Nothing here implements `pricing/`.**

**Tech Stack:** Python 3.12, Pydantic v2 (probe models), `yfinance`, `FinMind`, `twstock`, `requests`, `beautifulsoup4` (MY scrape), pandas. Probe deps are an **optional dependency group**, kept out of the core runtime deps.

**Spike note:** Adapter response shapes are *discovered* by the probe. Where a step says "inspect the saved fixture and assert the real field path," that is intentional — the executor runs the live fetch first, then writes the parser test against what actually came back. Do not fabricate field names; read them from the recorded fixture.

---

## File Structure

```
scripts/probe/
  __init__.py
  models.py          # ProbeResult, DataType, Verdict enums  (typed, tested)
  runner.py          # run_probe(), save_fixture()            (typed, tested)
  report.py          # aggregate + render markdown matrix     (typed, tested)
  adapters/
    __init__.py
    yfinance_src.py  # workhorse: quotes/history/fx/dividends, US/TW/MY  (live)
    tw_gov.py        # TWSE (上市) + TPEx (上櫃) open data               (live)
    finmind_src.py   # TW price/fx/dividend (keyed)                       (live)
    twstock_src.py   # TW intraday                                        (live)
    us_alt.py        # stockprices.dev / AlphaVantage / Finnhub          (live)
    my_src.py        # klsescreener / Bursa scrape + MY discovery        (live)
  run_all.py         # entrypoint: run every adapter, write report+fixtures
tests/probe/
  conftest.py        # add scripts/ to sys.path
  test_models.py
  test_runner.py
  test_report.py
  test_parsers.py    # parser tests against recorded fixtures
tests/pricing/fixtures/<source>/<symbol|pair>.<ext>   # recorded raw responses
docs/probes/2026-06-08-data-source-probe-results.md    # final report (Task 10)
```

Adapter modules carry `# mypy: ignore-errors` (spike, live I/O); harness modules
(`models`, `runner`, `report`) are fully typed and unit-tested.

---

## Task 1: Probe scaffold, deps, and `ProbeResult` model

**Files:**
- Create: `scripts/probe/__init__.py`, `scripts/probe/adapters/__init__.py`, `tests/probe/__init__.py`
- Create: `scripts/probe/models.py`
- Create: `tests/probe/conftest.py`, `tests/probe/test_models.py`
- Modify: `pyproject.toml` (probe optional-deps group + mypy `files`/override)

- [ ] **Step 1: Add the probe dependency group + mypy config to `pyproject.toml`**

Add (do not touch core `dependencies`):

```toml
[project.optional-dependencies]
probe = [
    "yfinance>=0.2.40",
    "FinMind>=1.7",
    "twstock>=1.3",
    "requests>=2.31",
    "beautifulsoup4>=4.12",
]
```

Extend the existing mypy config so harness code is checked but adapters are not:

```toml
[tool.mypy]
files = ["portfolio_dash", "tests", "scripts/probe"]

[[tool.mypy.overrides]]
module = "scripts.probe.adapters.*"
ignore_errors = true

[[tool.mypy.overrides]]
module = ["yfinance.*", "FinMind.*", "twstock.*", "bs4.*"]
ignore_missing_imports = true
```

- [ ] **Step 2: Install the probe group**

Run: `.\.venv\Scripts\python.exe -m pip install -e ".[probe]"`
Expected: yfinance, FinMind, twstock, requests, beautifulsoup4 installed.

- [ ] **Step 3: `tests/probe/conftest.py` — make `scripts/` importable**

```python
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
```

- [ ] **Step 4: Write the failing test for the model** — `tests/probe/test_models.py`

```python
from decimal import Decimal

from scripts.probe.models import DataType, ProbeResult, Verdict


def test_probe_result_minimal_and_defaults() -> None:
    r = ProbeResult(
        source="yfinance",
        data_type=DataType.QUOTE_LATEST,
        market="US",
        requires_key=False,
        verdict=Verdict.PRIMARY,
    )
    assert r.coverage_hits == 0
    assert r.coverage_misses == []
    assert r.verdict is Verdict.PRIMARY
    assert r.sample_value is None


def test_probe_result_rejects_non_finite_sample() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ProbeResult(
            source="x", data_type=DataType.FX, market="FX",
            requires_key=False, verdict=Verdict.UNUSABLE,
            sample_value=Decimal("NaN"),
        )
```

- [ ] **Step 5: Run it; verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/probe/test_models.py -v`
Expected: FAIL (module `scripts.probe.models` not found).

- [ ] **Step 6: Implement `scripts/probe/models.py`**

```python
"""Probe result model — one row per (source × data_type × market) measurement."""

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field


class DataType(StrEnum):
    QUOTE_LATEST = "quote_latest"
    QUOTE_HISTORY = "quote_history"
    FX = "fx"
    DIVIDEND = "dividend"


class Verdict(StrEnum):
    PRIMARY = "primary"
    FALLBACK = "fallback"
    UNUSABLE = "unusable"
    SKIPPED = "skipped"  # e.g. keyed source with no key supplied


class ProbeResult(BaseModel):
    source: str
    data_type: DataType
    market: str  # "US" | "TW" | "MY" | "FX"
    requires_key: bool
    verdict: Verdict

    batch_max: int | None = None          # max symbols per single call
    rate_limit: str | None = None         # observed/declared
    latency_ms: float | None = None
    coverage_hits: int = 0
    coverage_misses: list[str] = Field(default_factory=list)
    decimals_ok: bool | None = None       # MY 3-dp fidelity preserved
    has_raw_and_adj: bool | None = None    # raw + adjusted close both available
    history_earliest: str | None = None   # ISO date of earliest datum
    sample_value: Decimal | None = Field(default=None, allow_inf_nan=False)
    error: str | None = None
    notes: str | None = None
```

- [ ] **Step 7: Run the tests; verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/probe/test_models.py -v`
Expected: PASS (2 tests).

- [ ] **Step 8: Commit**

```bash
git add scripts/probe/__init__.py scripts/probe/adapters/__init__.py scripts/probe/models.py tests/probe/__init__.py tests/probe/conftest.py tests/probe/test_models.py pyproject.toml
git commit -m "feat(probe): scaffold probe harness + ProbeResult model"
```

---

## Task 2: Probe runner + fixture recorder

**Files:**
- Create: `scripts/probe/runner.py`
- Test: `tests/probe/test_runner.py`

- [ ] **Step 1: Write the failing test** — `tests/probe/test_runner.py`

```python
from pathlib import Path

from scripts.probe.models import DataType, Verdict
from scripts.probe.runner import run_probe, save_fixture


def test_run_probe_captures_latency_and_success() -> None:
    def ok() -> dict[str, int]:
        return {"price": 100}

    r = run_probe("fake", DataType.QUOTE_LATEST, "US", ok, requires_key=False)
    assert r.error is None
    assert r.latency_ms is not None and r.latency_ms >= 0
    assert r.verdict in (Verdict.PRIMARY, Verdict.FALLBACK)


def test_run_probe_records_error_after_retries() -> None:
    calls = {"n": 0}

    def boom() -> dict[str, int]:
        calls["n"] += 1
        raise RuntimeError("nope")

    r = run_probe("fake", DataType.QUOTE_LATEST, "US", boom,
                  requires_key=False, attempts=3)
    assert calls["n"] == 3
    assert r.verdict is Verdict.UNUSABLE
    assert r.error is not None and "nope" in r.error


def test_save_fixture_writes_file(tmp_path: Path) -> None:
    p = save_fixture("yfinance", "AAPL", '{"ok": 1}', root=tmp_path, ext="json")
    assert p.exists()
    assert p.read_text(encoding="utf-8") == '{"ok": 1}'
    assert p.parent.name == "yfinance"
```

- [ ] **Step 2: Run it; verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/probe/test_runner.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement `scripts/probe/runner.py`**

```python
"""Run one source probe with retries + timing; record raw fixtures."""

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from scripts.probe.models import DataType, ProbeResult, Verdict

_FIXTURE_ROOT = Path("tests/pricing/fixtures")


def run_probe(
    source: str,
    data_type: DataType,
    market: str,
    fn: Callable[[], Any],
    *,
    requires_key: bool,
    attempts: int = 3,
) -> ProbeResult:
    """Call ``fn`` up to ``attempts`` times, timing the first success.

    ``fn`` performs the live fetch and returns parsed data (truthy on success).
    On total failure the result is UNUSABLE with the last error recorded.
    """
    last_err: Exception | None = None
    for _ in range(attempts):
        start = time.perf_counter()
        try:
            fn()
            latency = (time.perf_counter() - start) * 1000.0
            return ProbeResult(
                source=source, data_type=data_type, market=market,
                requires_key=requires_key, verdict=Verdict.FALLBACK,
                latency_ms=latency,
            )
        except Exception as exc:  # noqa: BLE001 - probe records any failure
            last_err = exc
    return ProbeResult(
        source=source, data_type=data_type, market=market,
        requires_key=requires_key, verdict=Verdict.UNUSABLE,
        error=str(last_err),
    )


def save_fixture(
    source: str,
    key: str,
    raw: str,
    *,
    root: Path = _FIXTURE_ROOT,
    ext: str = "json",
) -> Path:
    """Persist a raw response under ``root/source/key.ext`` for later mock tests."""
    safe = key.replace("/", "_").replace("=", "")
    out = root / source / f"{safe}.{ext}"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(raw, encoding="utf-8")
    return out
```

- [ ] **Step 4: Run the tests; verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/probe/test_runner.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/probe/runner.py tests/probe/test_runner.py
git commit -m "feat(probe): add run_probe runner + fixture recorder"
```

---

## Task 3: Aggregator + markdown report renderer

**Files:**
- Create: `scripts/probe/report.py`
- Test: `tests/probe/test_report.py`

- [ ] **Step 1: Write the failing test** — `tests/probe/test_report.py`

```python
from scripts.probe.models import DataType, ProbeResult, Verdict
from scripts.probe.report import render_report


def _r(source: str, dt: DataType, market: str, verdict: Verdict) -> ProbeResult:
    return ProbeResult(source=source, data_type=dt, market=market,
                       requires_key=False, verdict=verdict, coverage_hits=5)


def test_render_report_groups_by_market_and_type() -> None:
    results = [
        _r("yfinance", DataType.QUOTE_LATEST, "US", Verdict.PRIMARY),
        _r("finnhub", DataType.QUOTE_LATEST, "US", Verdict.FALLBACK),
        _r("yfinance", DataType.FX, "FX", Verdict.PRIMARY),
    ]
    md = render_report(results)
    assert "# Data-Source Probe Results" in md
    assert "## US — quote_latest" in md
    assert "yfinance" in md and "finnhub" in md
    # primary listed before fallback in the recommendation line
    assert md.index("Recommended order") < md.index("finnhub")


def test_render_report_marks_unusable_and_skipped() -> None:
    md = render_report([_r("bursa", DataType.QUOTE_LATEST, "MY", Verdict.UNUSABLE)])
    assert "unusable" in md.lower()
```

- [ ] **Step 2: Run it; verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/probe/test_report.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement `scripts/probe/report.py`**

```python
"""Aggregate ProbeResults into a markdown comparison matrix + ranked recommendation."""

from collections import defaultdict

from scripts.probe.models import ProbeResult, Verdict

_ORDER = {Verdict.PRIMARY: 0, Verdict.FALLBACK: 1, Verdict.SKIPPED: 2, Verdict.UNUSABLE: 3}


def render_report(results: list[ProbeResult]) -> str:
    groups: dict[tuple[str, str], list[ProbeResult]] = defaultdict(list)
    for r in results:
        groups[(r.market, r.data_type.value)].append(r)

    lines: list[str] = ["# Data-Source Probe Results", ""]
    for (market, dtype) in sorted(groups):
        rows = sorted(groups[(market, dtype)], key=lambda r: _ORDER[r.verdict])
        lines.append(f"## {market} — {dtype}")
        usable = [r.source for r in rows if r.verdict in (Verdict.PRIMARY, Verdict.FALLBACK)]
        lines.append(f"Recommended order: {' → '.join(usable) if usable else '(none)'}")
        lines.append("")
        lines.append("| source | verdict | cov | batch | latency ms | 3dp | raw+adj | hist | notes |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for r in rows:
            lines.append(
                f"| {r.source} | {r.verdict.value} | {r.coverage_hits} | "
                f"{r.batch_max or ''} | {r.latency_ms or ''} | {r.decimals_ok if r.decimals_ok is not None else ''} | "
                f"{r.has_raw_and_adj if r.has_raw_and_adj is not None else ''} | "
                f"{r.history_earliest or ''} | {(r.error or r.notes or '').replace('|', '/')} |"
            )
        lines.append("")
    return "\n".join(lines)
```

- [ ] **Step 4: Run the tests; verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/probe/test_report.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/probe/report.py tests/probe/test_report.py
git commit -m "feat(probe): add markdown report renderer + ranked recommendation"
```

---

## Task 4: yfinance adapter (workhorse) + parser test

**Files:**
- Create: `scripts/probe/adapters/yfinance_src.py`
- Test: `tests/probe/test_parsers.py` (create; extended by later tasks)

Concrete yfinance calls (exact API):
- Latest+history (raw + adjusted): `yf.Ticker(sym).history(period="5y", auto_adjust=False)` → DataFrame columns `Open High Low Close "Adj Close" Volume Dividends "Stock Splits"`.
- Batch latest: `yf.download("AAPL MSFT", period="1d", auto_adjust=False, group_by="ticker")`.
- Dividends: `yf.Ticker(sym).dividends` (Series: ex-date → amount).
- FX: symbols `USDTWD=X`, `USDMYR=X`, `MYRTWD=X`.
- Suffixes: US bare (`AAPL`), TW 上市 `.TW` (`2330.TW`), TW 上櫃 `.TWO` (`8299.TWO`), MY `.KL` (`3182.KL`).

- [ ] **Step 1: Implement the live fetch + a pure parser** — `scripts/probe/adapters/yfinance_src.py`

```python
# mypy: ignore-errors
"""yfinance adapter: latest/history quotes, FX, dividends across US/TW/MY."""

from decimal import Decimal

import yfinance as yf

US = ["TSLA", "AAPL", "NVDA", "IVV", "VOO", "RIVN", "O", "BEN", "BABA",
      "GOOGL", "MSFT", "MU", "SNDK", "ARKK", "GGR", "SE"]
TW = ["0050", "8299", "2454", "2330", "6488", "6531", "2543", "2317",
      "3005", "6139", "2308", "1519"]
MY = ["5212", "3182", "5347", "1155", "1818"]
FX = ["USDTWD=X", "USDMYR=X", "MYRTWD=X"]


def fetch_history_df(symbol: str, period: str = "5y"):
    return yf.Ticker(symbol).history(period=period, auto_adjust=False)


def parse_latest_close(df) -> Decimal | None:
    """Last raw Close from a yfinance history DataFrame (None if empty)."""
    if df is None or df.empty or "Close" not in df.columns:
        return None
    return Decimal(str(df["Close"].iloc[-1]))


def has_raw_and_adj(df) -> bool:
    return df is not None and {"Close", "Adj Close"}.issubset(df.columns)


def max_decimals(df) -> int:
    """Max decimal places seen in the Close column (for MY 3-dp fidelity)."""
    if df is None or df.empty:
        return 0
    return max(
        (len(str(v).split(".")[-1]) if "." in str(v) else 0) for v in df["Close"]
    )
```

- [ ] **Step 2: Run a live capture to produce a fixture** (executor runs this once)

Run a throwaway snippet that fetches `3182.KL` history and writes it:

```python
from scripts.probe.adapters.yfinance_src import fetch_history_df
from scripts.probe.runner import save_fixture
df = fetch_history_df("3182.KL", period="1mo")
save_fixture("yfinance", "3182.KL", df.tail(5).to_json(), ext="json")
```

Expected: `tests/pricing/fixtures/yfinance/3182.KL.json` created. **Inspect it** to confirm the JSON field path for Close.

- [ ] **Step 3: Write the parser test against the recorded fixture** — append to `tests/probe/test_parsers.py`

```python
import json
from decimal import Decimal
from pathlib import Path

import pandas as pd

from scripts.probe.adapters.yfinance_src import (
    has_raw_and_adj, max_decimals, parse_latest_close,
)

_FX = Path("tests/pricing/fixtures/yfinance/3182.KL.json")


def test_yf_parser_against_recorded_my_fixture() -> None:
    df = pd.read_json(_FX)  # adjust if recorded shape differs (inspect fixture)
    close = parse_latest_close(df)
    assert close is None or isinstance(close, Decimal)
    # MY counters tick to 3 dp; the probe records whether fidelity is preserved
    assert max_decimals(df) >= 0
```

> If the recorded JSON shape differs from `pd.read_json` expectations, adjust the
> test to load the actual structure you saved. The fixture is the source of truth.

- [ ] **Step 4: Run the parser test**

Run: `.\.venv\Scripts\python.exe -m pytest tests/probe/test_parsers.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/probe/adapters/yfinance_src.py tests/probe/test_parsers.py tests/pricing/fixtures/yfinance/
git commit -m "feat(probe): yfinance adapter + recorded MY fixture parser test"
```

---

## Task 5: TW government open-data adapters (TWSE 上市 + TPEx 上櫃)

**Files:**
- Create: `scripts/probe/adapters/tw_gov.py`
- Test: extend `tests/probe/test_parsers.py`

Concrete endpoints:
- **TWSE (上市)** per-stock daily: `https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date=YYYYMMDD&stockNo=2330` → JSON `{"stat":"OK","data":[[date,vol,...,close,...]]}`.
- **TWSE** all latest: `https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL`.
- **TPEx (上櫃)** daily close all: `https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes` → JSON list with `SecuritiesCompanyCode`, `Close`.

- [ ] **Step 1: Implement** `scripts/probe/adapters/tw_gov.py`

```python
# mypy: ignore-errors
"""TW government open data: TWSE (上市) and TPEx (上櫃)."""

import requests

TWSE_DAY = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"
TPEX_DAILY = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"

LISTED_TW = ["0050", "2454", "2330", "2543", "2317", "3005", "2308", "1519"]
OTC_TWO = ["8299", "6488", "6531", "6139"]


def fetch_twse_day(stock_no: str, yyyymmdd: str) -> dict:
    resp = requests.get(
        TWSE_DAY, params={"response": "json", "date": yyyymmdd, "stockNo": stock_no},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def parse_twse_close(payload: dict) -> str | None:
    """Last row's close from a STOCK_DAY payload (close is the 7th column)."""
    if payload.get("stat") != "OK" or not payload.get("data"):
        return None
    return payload["data"][-1][6]


def fetch_tpex_daily() -> list[dict]:
    resp = requests.get(TPEX_DAILY, timeout=15)
    resp.raise_for_status()
    return resp.json()


def tpex_close_for(rows: list[dict], code: str) -> str | None:
    for row in rows:
        if row.get("SecuritiesCompanyCode") == code:
            return row.get("Close")
    return None
```

- [ ] **Step 2: Live capture → fixtures** (executor runs once)

Fetch one TWSE stock + the TPEx list, save via `save_fixture` (`ext="json"`). Inspect
the TPEx field names — **the real key for the close column may differ**; correct the
parser/test to the actual field.

- [ ] **Step 3: Parser tests against fixtures** — extend `tests/probe/test_parsers.py`

```python
import json
from pathlib import Path

from scripts.probe.adapters.tw_gov import parse_twse_close, tpex_close_for


def test_twse_parser_against_fixture() -> None:
    payload = json.loads(Path("tests/pricing/fixtures/twse/2330.json").read_text("utf-8"))
    assert parse_twse_close(payload) is not None


def test_tpex_parser_against_fixture() -> None:
    rows = json.loads(Path("tests/pricing/fixtures/tpex/daily.json").read_text("utf-8"))
    # 8299 is a 上櫃 sample; presence + a close confirms coverage
    assert tpex_close_for(rows, "8299") is not None
```

- [ ] **Step 4: Run; verify pass** (fix field names from the real fixture if needed)

Run: `.\.venv\Scripts\python.exe -m pytest tests/probe/test_parsers.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/probe/adapters/tw_gov.py tests/probe/test_parsers.py tests/pricing/fixtures/twse/ tests/pricing/fixtures/tpex/
git commit -m "feat(probe): TWSE + TPEx open-data adapters with fixture parser tests"
```

---

## Task 6: FinMind (keyed) + twstock TW adapters

**Files:**
- Create: `scripts/probe/adapters/finmind_src.py`, `scripts/probe/adapters/twstock_src.py`
- Test: extend `tests/probe/test_parsers.py`

Concrete:
- **FinMind** REST: `https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice&data_id=2330&start_date=2024-01-01&token=TOKEN`. FX dataset `TaiwanExchangeRate`; dividend `TaiwanStockDividend`. Token from `FINMIND_TOKEN` env (`.env`, gitignored). **No token → record SKIPPED.**
- **twstock**: `twstock.realtime.get("2330")` (intraday) and `twstock.Stock("2330").price` (history list).

- [ ] **Step 1: Implement both adapters** (key gating returns SKIPPED upstream)

```python
# mypy: ignore-errors
"""FinMind (keyed) TW price/fx/dividend."""

import os

import requests

FINMIND = "https://api.finmindtrade.com/api/v4/data"


def finmind_token() -> str | None:
    return os.environ.get("FINMIND_TOKEN")


def fetch_finmind(dataset: str, data_id: str, start: str, token: str) -> dict:
    resp = requests.get(
        FINMIND,
        params={"dataset": dataset, "data_id": data_id, "start_date": start, "token": token},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def parse_finmind_close(payload: dict) -> float | None:
    data = payload.get("data") or []
    return data[-1].get("close") if data else None
```

```python
# mypy: ignore-errors
"""twstock TW intraday + history."""

import twstock


def fetch_twstock_realtime(code: str) -> dict:
    return twstock.realtime.get(code)


def parse_twstock_price(payload: dict) -> str | None:
    if not payload.get("success"):
        return None
    return payload["realtime"]["latest_trade_price"]
```

- [ ] **Step 2: Live capture → fixtures** (FinMind only if `FINMIND_TOKEN` set; else note SKIPPED). Inspect FinMind JSON for the dividend/FX field names.

- [ ] **Step 3: Parser tests against fixtures** (skip FinMind test with `pytest.mark.skipif` when no fixture exists)

```python
import json
from pathlib import Path

import pytest

from scripts.probe.adapters.finmind_src import parse_finmind_close

_FM = Path("tests/pricing/fixtures/finmind/2330.json")


@pytest.mark.skipif(not _FM.exists(), reason="FinMind fixture needs a token to record")
def test_finmind_parser() -> None:
    assert parse_finmind_close(json.loads(_FM.read_text("utf-8"))) is not None
```

- [ ] **Step 4: Run; verify pass/skip**

Run: `.\.venv\Scripts\python.exe -m pytest tests/probe/test_parsers.py -v`
Expected: PASS (FinMind test skipped if no token/fixture).

- [ ] **Step 5: Commit**

```bash
git add scripts/probe/adapters/finmind_src.py scripts/probe/adapters/twstock_src.py tests/probe/test_parsers.py tests/pricing/fixtures/
git commit -m "feat(probe): FinMind (keyed) + twstock adapters with parser tests"
```

---

## Task 7: US alternative adapters (stockprices.dev, AlphaVantage, Finnhub)

**Files:**
- Create: `scripts/probe/adapters/us_alt.py`
- Test: extend `tests/probe/test_parsers.py`

Concrete:
- **stockprices.dev** — no key, no docs known: **discovery step** — `GET https://stockprices.dev/` and any `/api` path; inspect the response to find the quote endpoint, then implement `fetch_stockprices(symbol)`. Record what you find in `notes`.
- **AlphaVantage** — `https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol=AAPL&apikey=KEY` (latest); `TIME_SERIES_DAILY` (history); `CURRENCY_EXCHANGE_RATE`/`FX_DAILY` (FX). Key from `ALPHAVANTAGE_KEY`. **No key → SKIPPED.** Measure the real rate limit (declared 5/min was historical; free tier may now be ~25/day).
- **Finnhub** — `https://finnhub.io/api/v1/quote?symbol=AAPL&token=KEY` (latest). Key from `FINNHUB_KEY`. **No key → SKIPPED.**

- [ ] **Step 1: Implement `us_alt.py`**

```python
# mypy: ignore-errors
"""US alternative sources: stockprices.dev (no key), AlphaVantage + Finnhub (keyed)."""

import os

import requests

ALPHA = "https://www.alphavantage.co/query"
FINNHUB = "https://finnhub.io/api/v1/quote"


def alpha_key() -> str | None:
    return os.environ.get("ALPHAVANTAGE_KEY")


def finnhub_key() -> str | None:
    return os.environ.get("FINNHUB_KEY")


def fetch_stockprices(symbol: str) -> dict:
    # Endpoint not documented here: GET the site root first to discover the quote
    # path, then correct this URL. Record the discovered path in the result notes.
    resp = requests.get(f"https://stockprices.dev/api/quote/{symbol}", timeout=15)
    resp.raise_for_status()
    return resp.json()


def fetch_alpha_global_quote(symbol: str, key: str) -> dict:
    resp = requests.get(
        ALPHA, params={"function": "GLOBAL_QUOTE", "symbol": symbol, "apikey": key},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def parse_alpha_close(payload: dict) -> str | None:
    return (payload.get("Global Quote") or {}).get("05. price")


def fetch_finnhub_quote(symbol: str, key: str) -> dict:
    resp = requests.get(FINNHUB, params={"symbol": symbol, "token": key}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def parse_finnhub_close(payload: dict) -> float | None:
    return payload.get("c")  # Finnhub quote: c=current, pc=previous close
```

Keyed sources (`alpha_key()`/`finnhub_key()` returning `None`) are reported `SKIPPED`
by `run_all` (Task 9). For stockprices.dev the live capture's first action is to GET the
root/docs to confirm the real endpoint before recording.

- [ ] **Step 2: Live capture → fixtures** (stockprices.dev now; AlphaVantage/Finnhub only if keys set, else SKIPPED). Inspect each JSON for the close field path.

- [ ] **Step 3: Parser tests against recorded fixtures** (skipif for keyed sources without a fixture).

- [ ] **Step 4: Run; verify pass/skip**

Run: `.\.venv\Scripts\python.exe -m pytest tests/probe/test_parsers.py -v`
Expected: PASS (keyed tests skipped if no key).

- [ ] **Step 5: Commit**

```bash
git add scripts/probe/adapters/us_alt.py tests/probe/test_parsers.py tests/pricing/fixtures/
git commit -m "feat(probe): US alt adapters (stockprices.dev/AlphaVantage/Finnhub)"
```

---

## Task 8: MY adapters (scrape) + MY source discovery

**Files:**
- Create: `scripts/probe/adapters/my_src.py`
- Test: extend `tests/probe/test_parsers.py`

Concrete:
- **klsescreener** — `https://www.klsescreener.com/v2/stocks/view/<code>` (HTML; parse with BeautifulSoup). Inspect for the price node.
- **Bursa** — `https://www.bursamalaysia.com/market_information/equities_prices` (delayed, HTML/JSON behind it).
- **Discovery (research + validate, free tiers):** marketstack `http://api.marketstack.com/v1/eod?access_key=KEY&symbols=3182.XKLS`; eodhd `https://eodhd.com/api/eod/3182.KL?api_token=KEY&fmt=json`; twelvedata `https://api.twelvedata.com/price?symbol=3182:XKLS&apikey=KEY`; plus web: i3investor, Malaysiastock.biz. Record availability/key-needs/3-dp fidelity in `notes`. (yfinance(.KL) was already covered in Task 4 and is the expected MY primary.)

- [ ] **Step 1: Implement `my_src.py`** — `fetch_klse(code)` (requests + BeautifulSoup parse → price string), `fetch_bursa()`, and discovery `fetch_marketstack/eodhd/twelvedata` (key-gated). Emphasis: **preserve 3-dp** (compare against yfinance(.KL) for the same counters).

- [ ] **Step 2: Live capture → fixtures** (klsescreener HTML for 3182/5212; discovery sources only if keys). Inspect the HTML for the price selector.

- [ ] **Step 3: Parser test against the klsescreener fixture** (assert a non-empty price string + 3-dp where applicable).

- [ ] **Step 4: Run; verify pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/probe/test_parsers.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/probe/adapters/my_src.py tests/probe/test_parsers.py tests/pricing/fixtures/
git commit -m "feat(probe): MY scrape adapters + MY free-source discovery"
```

---

## Task 9: Orchestrate the full run

**Files:**
- Create: `scripts/probe/run_all.py`

- [ ] **Step 1: Implement `run_all.py`** — call every adapter through `run_probe`, fill the `ProbeResult` fields (coverage over the sample tickers, `decimals_ok`, `has_raw_and_adj`, `history_earliest`, `batch_max`, `latency_ms`), enrich keyed sources with `Verdict.SKIPPED` when their `*_token()`/env is missing, collect into `list[ProbeResult]`, then `render_report(...)` → write `docs/probes/2026-06-08-data-source-probe-results.md`. Also save raw fixtures as it goes.

```python
# mypy: ignore-errors
"""Entrypoint: run all source probes, write report + fixtures."""

from pathlib import Path

from scripts.probe.report import render_report
# import adapters + run_probe; build results list ...


def main() -> None:
    results = []
    # ... populate results from each adapter ...
    out = Path("docs/probes/2026-06-08-data-source-probe-results.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_report(results), encoding="utf-8")
    print(f"wrote {out} with {len(results)} results")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it live** (no-key sources now; set `FINMIND_TOKEN`/`ALPHAVANTAGE_KEY`/`FINNHUB_KEY` in `.env` first if available)

Run: `.\.venv\Scripts\python.exe -m scripts.probe.run_all`
Expected: prints the report path; `docs/probes/...md` exists; fixtures populated under `tests/pricing/fixtures/`.

- [ ] **Step 3: Commit** (report + run_all + any new fixtures; **never commit `.env`**)

```bash
git add scripts/probe/run_all.py
git commit -m "feat(probe): full-run orchestrator (run_all)"
```

---

## Task 10: Finalize the results report (matrix + recommendation + catalogue)

**Files:**
- Modify: `docs/probes/2026-06-08-data-source-probe-results.md` (hand-augment the generated matrix)
- Modify: `CHANGELOG.md` ([Unreleased] — move the probe from Planned to a delivered note)

- [ ] **Step 1: Augment the generated report** with, per (data type × market): the **ranked primary/fallback recommendation** with one-line justification; a **"discovered MY sources"** subsection; a **"qualitative sources catalogue"** (Google Trends/FRED/news — for the future `llm_insight/` probe); and an **architecture recommendation** for `pricing/` (provider protocol + config-driven ordered chain + provenance + graceful degradation). Note any keyed sources left `SKIPPED` (Schwab pending; others if no key).

- [ ] **Step 2: Update `CHANGELOG.md`** — under `[Unreleased]`, add an `### Added` bullet recording the probe spike delivery + the results-report path; remove the probe line from `### Planned` (leave the `llm_insight/` Planned bullet). Run `grep -c "^## \[v" CHANGELOG.md` and confirm the count is unchanged (1).

- [ ] **Step 3: Commit**

```bash
git add docs/probes/2026-06-08-data-source-probe-results.md CHANGELOG.md
git commit -m "docs(probe): finalize data-source probe results + recommendation"
```

---

## Done criteria

- Harness (`models`/`runner`/`report`) unit-tested green; `mypy --strict` clean over
  `portfolio_dash`, `tests`, `scripts/probe` (adapters ignored); `ruff` clean.
- `docs/probes/2026-06-08-data-source-probe-results.md` contains a full comparison matrix
  + a ranked primary/fallback recommendation per (data type × market), MY-source
  discovery, qualitative-source catalogue, and a `pricing/` architecture recommendation.
- Raw fixtures recorded under `tests/pricing/fixtures/` for later `pricing/` mock tests.
- Keyed sources without a key are honestly marked `SKIPPED` (not failed); Schwab `pending`.
- `.env` never committed.
```
