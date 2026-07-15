"""Shared harness utilities for the portfolio-dash stress verification.

Handles: evidence logging (oplog/assertions jsonl), Decimal-aware assertion comparison,
an httpx API client, a local uvicorn subprocess launcher (Phase 1), direct-SQLite fact
loading, and fixture seeding via the app's write seams.

The app is used here ONLY for SETUP/fixtures and as the system-under-test HTTP surface.
The accounting oracle (oracle.py) imports nothing from the app.

PORTABILITY
-----------
All paths derive from this file's location, so the package works from any checkout:
  - ``REPO_ROOT`` = the repo root (scripts/stress_audit/ -> parents[2]).
  - ``PY`` = ``sys.executable`` — the interpreter running the harness. RUN THE HARNESS
    WITH THE REPO ``.venv`` PYTHON so the spawned uvicorn uses the project's deps:
        .venv\\Scripts\\python.exe -m scripts.stress_audit.run_all   (from the repo root)
    (or invoke run_all.py / run_phase1.py directly with that interpreter).
  - Evidence (oplog/assertions jsonl, the phase-1 DB, uvicorn logs) is written under
    ``EVIDENCE`` (scripts/stress_audit/evidence/), which is git-ignored — evidence is
    REGENERATED every run and never committed or trusted from a previous run.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import oracle as O

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]           # scripts/stress_audit -> scripts -> <repo root>
PY = Path(sys.executable)             # run with the repo .venv python (documented above)

EVIDENCE = HERE / "evidence"
EVIDENCE.mkdir(parents=True, exist_ok=True)
OPLOG = EVIDENCE / "oplog.jsonl"
ASSERTIONS = EVIDENCE / "assertions.jsonl"


# ------------------------------------------------------------------ evidence logs
class Evidence:
    def __init__(self, oplog: Path = OPLOG, assertions: Path = ASSERTIONS,
                 reset: bool = True) -> None:
        self.oplog = oplog
        self.assertions = assertions
        self.op_n = 0
        self.n_pass = 0
        self.n_fail = 0
        self.fails: list[dict] = []
        if reset:
            oplog.write_text("", encoding="utf-8")
            assertions.write_text("", encoding="utf-8")

    def op(self, phase: str, surface: str, kind: str, inputs: Any, response: Any,
           note: str = "") -> int:
        self.op_n += 1
        rec = {"op": self.op_n, "phase": phase, "surface": surface, "kind": kind,
               "inputs": inputs, "response": _jsonable(response), "note": note,
               "ts": datetime.now().isoformat()}
        with self.oplog.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return self.op_n

    def check(self, check: str, scope: str, expected: Any, actual: Any,
              phase: str = "") -> bool:
        """Exact-Decimal assertion (NO tolerance). The default for the whole suite."""
        ok = _decimal_equal(expected, actual)
        rec = {"check": check, "scope": scope, "phase": phase,
               "expected": _sval(expected), "actual": _sval(actual), "pass": ok}
        with self.assertions.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if ok:
            self.n_pass += 1
        else:
            self.n_fail += 1
            self.fails.append(rec)
        return ok

    def check_close(self, check: str, scope: str, expected: Any, actual: Any,
                    tol: Decimal, phase: str = "") -> bool:
        """The ONE documented-tolerance assertion family (XIRR scalar only).

        Everything else in the suite uses exact-Decimal ``check()``. XIRR is an
        inherently numeric root-find, so |expected - actual| <= ``tol`` (oracle.XIRR_TOL)
        is the disclosed comparison. Logged with the delta for the evidence trail.
        """
        de, da = _as_decimal(expected), _as_decimal(actual)
        if de is None or da is None:
            ok = expected is None and actual is None
            delta = None
        else:
            delta = abs(de - da)
            ok = delta <= tol
        rec = {"check": check, "scope": scope, "phase": phase,
               "expected": _sval(expected), "actual": _sval(actual),
               "tol": _sval(tol), "delta": _sval(delta), "pass": ok}
        with self.assertions.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if ok:
            self.n_pass += 1
        else:
            self.n_fail += 1
            self.fails.append(rec)
        return ok


def _jsonable(v: Any) -> Any:
    try:
        json.dumps(v)
        return v
    except TypeError:
        return str(v)


def _sval(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return format(v, "f")
    return str(v)


def _decimal_equal(a: Any, b: Any) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    da, db = _as_decimal(a), _as_decimal(b)
    if da is not None and db is not None:
        return da == db
    return str(a) == str(b)


def _as_decimal(v: Any) -> Decimal | None:
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except Exception:
        return None


def dec(v: Any) -> Decimal:
    return Decimal(str(v))


# ------------------------------------------------------------------ API client
class Api:
    def __init__(self, base_url: str, verify: bool = True) -> None:
        self.base = base_url.rstrip("/")
        self.c = httpx.Client(base_url=self.base, timeout=60.0, verify=verify)

    def get(self, path: str, **params: Any) -> httpx.Response:
        return self.c.get(path, params=params or None)

    def post(self, path: str, body: Any = None) -> httpx.Response:
        return self.c.post(path, json=(body if body is not None else {}))

    def put(self, path: str, body: Any) -> httpx.Response:
        return self.c.put(path, json=body)

    def delete(self, path: str, **params: Any) -> httpx.Response:
        return self.c.delete(path, params=params or None)

    def download(self, path: str, body: Any = None) -> bytes:
        r = self.c.post(path, json=(body if body is not None else {}))
        r.raise_for_status()
        return r.content

    def close(self) -> None:
        self.c.close()


# ------------------------------------------------------------------ local server
def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class LocalServer:
    """Spawn uvicorn against a fresh DB file, scheduler disabled (Phase 1 clean-room).

    The interpreter is ``PY`` (= sys.executable): whatever python runs the harness runs
    the app, so the repo .venv is used end-to-end. ``REPO_ROOT`` is the code checkout.
    """

    def __init__(self, db_path: Path, port: int | None = None) -> None:
        self.db_path = db_path
        self.port = port or free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.proc: subprocess.Popen | None = None
        self.log = db_path.with_suffix(".uvicorn.log")

    def start(self) -> str:
        env = dict(os.environ)
        env["DB_PATH"] = str(self.db_path)
        env["PD_DISABLE_SCHEDULER"] = "1"
        env["PYTHONPATH"] = str(REPO_ROOT)
        env["PYTHONIOENCODING"] = "utf-8"
        fh = self.log.open("wb")
        self._fh = fh
        self.proc = subprocess.Popen(
            [str(PY), "-m", "uvicorn", "portfolio_dash.api.app:create_app",
             "--factory", "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=str(REPO_ROOT), env=env, stdout=fh, stderr=fh)
        self._wait_ready()
        return self.base_url

    def _wait_ready(self, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        url = self.base_url + "/api/health"
        while time.monotonic() < deadline:
            if self.proc and self.proc.poll() is not None:
                raise RuntimeError(f"uvicorn exited early; see {self.log}")
            try:
                with urllib.request.urlopen(url, timeout=2.0) as resp:  # noqa: S310
                    if resp.status == 200:
                        return
            except Exception:
                pass
            time.sleep(0.25)
        raise TimeoutError(f"server not ready; see {self.log}")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        try:
            self._fh.close()
        except Exception:
            pass


# ------------------------------------------------------------------ fixture seeding
def seed_instrument(db_path: Path, symbol: str, market: str, quote_ccy: str,
                    name: str, sector: str, is_etf: bool) -> None:
    """Insert an instrument straight into the DB (setup fixture; no network).

    Uses the app's own upsert to keep schema/columns correct — a fixture step, not
    the calculation under test.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from portfolio_dash.data_ingestion.store import upsert_instrument
    from portfolio_dash.shared.enums import Currency, Market
    from portfolio_dash.shared.models.assets import Instrument
    conn = sqlite3.connect(str(db_path))
    try:
        upsert_instrument(conn, Instrument(
            symbol=symbol, market=Market(market), quote_ccy=Currency(quote_ccy),
            sector=sector, name=name, board="", target_low=None, is_etf=is_etf))
    finally:
        conn.close()


def seed_price(db_path: Path, symbol: str, market: str, close: Decimal,
               as_of: date, source: str = "stress-fixture") -> None:
    sys.path.insert(0, str(REPO_ROOT))
    from portfolio_dash.pricing.results import PriceRow
    from portfolio_dash.pricing.store import upsert_prices
    from portfolio_dash.shared.enums import Market
    conn = sqlite3.connect(str(db_path))
    try:
        upsert_prices(conn, [PriceRow(instrument=symbol, market=Market(market),
                                      as_of=as_of, close=close, source=source)],
                      fetched_at=datetime.now())
    finally:
        conn.close()


def seed_fx(db_path: Path, base: str, quote: str, rate: Decimal, as_of: date,
            source: str = "stress-fixture") -> None:
    sys.path.insert(0, str(REPO_ROOT))
    from portfolio_dash.pricing.results import FxRow
    from portfolio_dash.pricing.store import upsert_fx
    from portfolio_dash.shared.enums import Currency
    conn = sqlite3.connect(str(db_path))
    try:
        upsert_fx(conn, [FxRow(base=Currency(base), quote=Currency(quote),
                               as_of=as_of, rate=rate, source=source)],
                  fetched_at=datetime.now())
    finally:
        conn.close()


# ------------------------------------------------------------------ fact loading
def load_facts_from_db(db_path: Path) -> O.Facts:
    """Read the raw ledger tables straight from SQLite -> oracle Facts.

    Maximally independent: the oracle's inputs are the stored FACTS, not any
    app-computed result.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        f = O.Facts()
        for r in conn.execute("SELECT * FROM instruments"):
            f.instruments[r["symbol"]] = O.Instrument(
                symbol=r["symbol"], market=r["market"], quote_ccy=r["quote_ccy"],
                is_etf=bool(r["is_etf"]), sector=r["sector"] or "")
        for r in conn.execute("SELECT * FROM transactions ORDER BY trade_date, id"):
            f.txs.append(O.TxFact(
                id=r["id"], account_id=r["account_id"], symbol=r["symbol"],
                side=r["side"], qty=dec(r["quantity"]), price=dec(r["price"]),
                fee=dec(r["fees"]), tax=dec(r["tax"]),
                trade_date=date.fromisoformat(r["trade_date"])))
        for r in conn.execute("SELECT * FROM dividends ORDER BY date, id"):
            f.divs.append(O.DivFact(
                id=r["id"], account_id=r["account_id"], symbol=r["symbol"],
                d=date.fromisoformat(r["date"]), type=r["type"],
                gross=dec(r["gross"] or "0"), withholding=dec(r["withholding"] or "0"),
                net=dec(r["net"] or "0"),
                reinvest_shares=dec(r["reinvest_shares"]) if r["reinvest_shares"] else None,
                reinvest_price=dec(r["reinvest_price"]) if r["reinvest_price"] else None))
        for r in conn.execute("SELECT * FROM fx_conversions ORDER BY date, id"):
            f.fxs.append(O.FxFact(
                id=r["id"], account_id=r["account_id"], d=date.fromisoformat(r["date"]),
                from_ccy=r["from_ccy"], from_amt=dec(r["from_amount"]),
                to_ccy=r["to_ccy"], to_amt=dec(r["to_amount"])))
        for r in conn.execute("SELECT * FROM opening_inventory"):
            f.openings.append(O.OpenFact(
                account_id=r["account_id"], symbol=r["symbol"], shares=dec(r["shares"]),
                orig_avg=dec(r["original_avg_cost"]), orig_total=dec(r["original_cost_total"]),
                build_date=date.fromisoformat(r["build_date"])))
        for r in conn.execute("SELECT * FROM cash_movements ORDER BY date, id"):
            f.cash.append(O.CashFact(
                id=r["id"], account_id=r["account_id"], d=date.fromisoformat(r["date"]),
                kind=r["kind"], ccy=r["ccy"], amount=dec(r["amount"])))
        return f
    finally:
        conn.close()


def _page(api: Api, path: str, key_rows: str = "rows") -> list[dict]:
    """Collect ALL rows across pages of a paginated ledger/cash endpoint."""
    out: list[dict] = []
    offset = 0
    while True:
        j = api.get(path, limit=500, offset=offset).json()
        if key_rows == "movements":
            block = j.get("movements", {})
            rows = block.get("rows", [])
            total = block.get("total_count", len(rows))
        else:
            rows = j.get("rows", [])
            total = j.get("total_count", len(rows))
        out.extend(rows)
        offset += len(rows)
        if not rows or offset >= total:
            break
    return out


def load_facts_from_api(api: Api) -> O.Facts:
    """Read the raw ledger FACTS via the public read endpoints (Phase 2 / remote demo).

    Same independence posture as the DB loader: these endpoints return stored rows
    (facts), and the oracle computes the derived state itself.
    """
    f = O.Facts()
    insts = api.get("/api/instruments").json().get("list", [])
    for i in insts:
        f.instruments[i["symbol"]] = O.Instrument(
            symbol=i["symbol"], market=i["market"], quote_ccy=i["ccy"],
            is_etf=bool(i.get("is_etf")), sector=i.get("sector") or "")
    known = set(f.instruments)
    for r in _page(api, "/api/ledgers/transactions"):
        if r["symbol"] not in known:
            continue
        f.txs.append(O.TxFact(
            id=r["id"], account_id=r["account_id"], symbol=r["symbol"],
            side=str(r["side"]).upper(), qty=dec(r["shares"]), price=dec(r["price"]),
            fee=dec(r["fee"]), tax=dec(r["tax"]),
            trade_date=date.fromisoformat(r["date"])))
    for r in _page(api, "/api/ledgers/dividends"):
        if r["symbol"] not in known:
            continue
        f.divs.append(O.DivFact(
            id=r["id"], account_id=r["account_id"], symbol=r["symbol"],
            d=date.fromisoformat(r["date"]), type=str(r["type"]).upper(),
            gross=dec(r.get("gross") or "0"), withholding=dec(r.get("withhold") or "0"),
            net=dec(r.get("net") or "0"),
            reinvest_shares=dec(r["reinvest_shares"]) if r.get("reinvest_shares") else None,
            reinvest_price=dec(r["reinvest_price"]) if r.get("reinvest_price") else None))
    for r in _page(api, "/api/ledgers/fx"):
        f.fxs.append(O.FxFact(
            id=r["id"], account_id=r["account_id"], d=date.fromisoformat(r["date"]),
            from_ccy=r["from_ccy"], from_amt=dec(r["from_amt"]),
            to_ccy=r["to_ccy"], to_amt=dec(r["to_amt"])))
    for r in _page(api, "/api/ledgers/openings"):
        if r["symbol"] not in known:
            continue
        f.openings.append(O.OpenFact(
            account_id=r["account_id"], symbol=r["symbol"], shares=dec(r["shares"]),
            orig_avg=dec(r["avg"]), orig_total=dec(r["total"]),
            build_date=date.fromisoformat(r["date"])))
    for r in _page(api, "/api/cash", key_rows="movements"):
        f.cash.append(O.CashFact(
            id=r["id"], account_id=r["account_id"], d=date.fromisoformat(r["date"]),
            kind=str(r["kind"]).upper(), ccy=r["ccy"], amount=dec(r["amount"])))
    return f


def read_fee_tax_from_api(api: Api, txn_id: int) -> tuple[Decimal, Decimal] | None:
    for r in _page(api, "/api/ledgers/transactions"):
        if r["id"] == txn_id:
            return dec(r["fee"]), dec(r["tax"])
    return None


def read_fee_tax_from_db(db_path: Path, txn_id: int) -> tuple[Decimal, Decimal]:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT fees, tax FROM transactions WHERE id=?", (txn_id,)).fetchone()
        return dec(row[0]), dec(row[1])
    finally:
        conn.close()


# ------------------------------------------------------------------ fx resolver
class Spot:
    """Mirror get_fx resolution: direct latest, else inverse. Built from seeded rates."""

    def __init__(self, rates: dict[tuple[str, str], Decimal]) -> None:
        self.rates = rates

    def rate(self, base: str, quote: str) -> Decimal:
        if base == quote:
            return O.ONE
        if (base, quote) in self.rates:
            return self.rates[(base, quote)]
        if (quote, base) in self.rates:
            return O.ONE / self.rates[(quote, base)]
        raise KeyError(f"no rate {base}/{quote}")
