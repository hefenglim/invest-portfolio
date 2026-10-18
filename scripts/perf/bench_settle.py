"""Settlement-store measurements on a copy of a database (PERF plan W0.4, from
``bench_settle_sim``).

Sections (``--sections``, default all, in this order):

- ``profile`` — cProfile of one full dashboard build: calls, cumulative seconds and share for
  the functions that matter, plus the top package functions by cumulative time;
- ``settled`` — the full build versus a SIMULATED settled build (``_harness.SettledSimulation``:
  the daily value series, the daily cash series and the benchmark served from memory, the
  trend's full price-history loads skipped). The two payloads must be byte-identical: a
  difference fails the script (exit 1) and names the first differing top-level field;
- ``readback`` — the cost of reading the trend's settled points back from SQLite;
- ``prefix`` — the prefix property (spec §4): on a fresh copy each, a row dated D (a buy
  today, a cash dividend today, a back-dated buy, a restated close) must change no trend
  point before D. Days are reported as offsets from the first trend day, never as dates;
- ``payload`` — ``/api/dashboard`` bytes by top-level key. It boots the app on the working
  copy (boot DDL and seeds), so it runs last.

Output: ``<out>/settle.json`` (timings, counts and sizes only).
"""

from __future__ import annotations

import argparse
import cProfile
import hashlib
import json
import pstats
import sqlite3
import sys
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _harness as H  # noqa: E402

SECTIONS = ("profile", "settled", "readback", "prefix", "payload")

#: Functions whose profile rows are always reported when present (code names, not data).
PROFILE_FUNCTIONS = (
    "_build_dashboard", "daily_value_series", "build_book", "through", "daily_cash_series",
    "compose_net_worth", "_benchmark_comparison", "counterfactual", "get_price_history",
    "get_fx_history", "load_ledger_bundle", "xirr_reporting", "build_reporting_flows",
    "replay_dividend_refusals", "value_holdings", "dividend_summary", "_holdings_subtotals",
    "trading_financing_cost", "price_in", "_entry_at_or_before", "counts_by", "valued_as_of",
    "iter_books", "value_series", "kpis_as_of",
)


def main(argv: list[str] | None = None) -> int:
    parser = H.common_parser(__doc__.splitlines()[0] if __doc__ else "bench settle")
    parser.add_argument("--sections", default=",".join(SECTIONS),
                        help=f"comma-separated subset of {','.join(SECTIONS)}")
    parser.add_argument("--repeat", type=int, default=3, help="settled builds to time")
    args = parser.parse_args(argv)
    sections = [s for s in args.sections.split(",") if s]
    unknown = sorted(set(sections) - set(SECTIONS))
    if unknown:
        raise SystemExit(f"--sections: unknown {unknown}")
    ws = H.open_workspace(args)
    try:
        return _run(ws, args, sections)
    finally:
        ws.close()


def _run(ws: H.Workspace, args: argparse.Namespace, sections: list[str]) -> int:
    from portfolio_dash.portfolio import dashboard
    from portfolio_dash.shared.config import get_settings

    reporting = get_settings().reporting_currency
    conn = H.connect(ws.db)

    def build(c: sqlite3.Connection = conn) -> Any:
        return dashboard._build_dashboard(c, now=ws.now, reporting=reporting)

    report: dict[str, Any] = {"kind": "bench_settle", "environment": H.environment_info(ws),
                              "shape": H.ledger_shape(conn), "sections": sections}
    status = 0
    try:
        if "profile" in sections:
            report["profile"] = _profile(build)
        reference = None
        if {"settled", "readback", "prefix"} & set(sections):
            reference, t = H.timed(build)
            report["reference_build"] = t.as_dict()
        if "settled" in sections:
            report["settled"] = _settled(dashboard, build, reference, args.repeat)
            if report["settled"].get("identical") is False:
                status = 1
        if "readback" in sections:
            report["readback"] = _readback(reference)
        if "prefix" in sections:
            report["prefix"] = _prefix(ws, build, reference)
        if "payload" in sections:
            report["payload"] = _payload(ws)
    finally:
        conn.close()
    H.write_json(ws, "settle", report)
    return status


# -- profile ---------------------------------------------------------------------------------
def _profile(build: Callable[[], Any]) -> dict[str, Any]:
    profiler = cProfile.Profile()
    profiler.enable()
    _, t = H.timed(build)
    profiler.disable()
    stats = pstats.Stats(profiler)
    table: dict[tuple[str, int, str], tuple[int, int, float, float, Any]] = \
        stats.stats  # type: ignore[attr-defined]
    total = max((v[3] for v in table.values()), default=0.0) or 1.0
    named: dict[str, tuple[int, float]] = {}
    package: list[tuple[float, int, str]] = []
    for (filename, _line, func), (_cc, ncalls, _tt, cum, _callers) in table.items():
        norm = filename.replace("\\", "/")
        if "/portfolio_dash/" not in norm:
            continue
        module = norm.split("/portfolio_dash/", 1)[1]
        package.append((cum, ncalls, f"{module}:{func}"))
        if func in PROFILE_FUNCTIONS and (func not in named or cum > named[func][1]):
            named[func] = (ncalls, cum)
    return {
        "build": t.as_dict(),
        "profiled_total_s": round(total, 3),
        "functions": {f: {"calls": n, "cum_s": round(c, 3), "share_pct": round(100 * c / total, 1)}
                      for f, (n, c) in sorted(named.items(), key=lambda kv: -kv[1][1])},
        "top_package": [{"function": name, "calls": n, "cum_s": round(c, 3),
                         "share_pct": round(100 * c / total, 1)}
                        for c, n, name in sorted(package, reverse=True)[:25]],
    }


# -- settled simulation ----------------------------------------------------------------------
def _digest(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()[:16]


def _settled(dashboard: Any, build: Callable[[], Any], reference: Any,
             repeat: int) -> dict[str, Any]:
    full_times = []
    full = reference
    for _ in range(2):
        full, t = H.timed(build)
        full_times.append(t.as_dict())
    sim = H.SettledSimulation(dashboard)
    if not sim.available:
        return {"full": full_times, "available": False, "missing": sim.missing}
    settled_times = []
    with sim:
        sim.prime(build)
        settled = full
        for _ in range(max(1, repeat)):
            settled, t = H.timed(build)
            settled_times.append(t.as_dict())
    full_json, settled_json = full.model_dump_json(), settled.model_dump_json()
    identical = full_json == settled_json
    out: dict[str, Any] = {
        "available": True, "full": full_times, "settled": settled_times,
        "identical": identical, "payload_bytes": len(full_json.encode("utf-8")),
        "payload_sha256_16": _digest(full_json),
        "trend_points": len(full.trend.points),
    }
    if not identical:
        out["first_differing_field"] = H.first_difference(
            json.loads(full_json), json.loads(settled_json))
    return out


# -- read-back -------------------------------------------------------------------------------
def _readback(reference: Any) -> dict[str, Any]:
    from portfolio_dash.portfolio.dashboard_models import TrendPoint

    points = reference.trend.points
    fields = list(TrendPoint.model_fields)
    mem = sqlite3.connect(":memory:")
    try:
        cols = ", ".join(f'"{f}"' for f in fields)
        mem.execute(f"CREATE TABLE s ({cols})")
        mem.executemany(f"INSERT INTO s VALUES ({', '.join('?' * len(fields))})",
                        [tuple(_cell(p.model_dump(mode="json")[f]) for f in fields)
                         for p in points])

        def read() -> list[Any]:
            return [TrendPoint.model_validate(dict(zip(fields, row, strict=True)))
                    for row in mem.execute(f"SELECT {cols} FROM s ORDER BY rowid")]

        times = []
        back: list[Any] = []
        for _ in range(3):
            back, t = H.timed(read)
            times.append(t.as_dict())
    finally:
        mem.close()
    identical = [p.model_dump() for p in back] == [p.model_dump() for p in points]
    return {"points": len(points), "fields": fields, "reads": times, "identical": identical}


def _cell(value: Any) -> Any:
    return int(value) if isinstance(value, bool) else value


# -- payload split ---------------------------------------------------------------------------
def _payload(ws: H.Workspace) -> dict[str, Any]:
    from fastapi.testclient import TestClient

    from portfolio_dash.api.app import create_app
    from portfolio_dash.api.auth_store import require_session
    from portfolio_dash.api.deps import get_now

    app = create_app()
    app.dependency_overrides[get_now] = lambda: ws.now
    app.dependency_overrides[require_session] = lambda: None
    with TestClient(app) as client:
        resp = client.get("/api/dashboard")
    if resp.status_code != 200:
        return {"status": resp.status_code}
    body = resp.json()
    total = len(resp.content)
    keys = {k: len(json.dumps(v, ensure_ascii=False).encode("utf-8")) for k, v in body.items()}
    return {
        "status": 200, "bytes": total, "gzip_bytes": H.gzip_size(resp.content),
        "by_key_bytes": dict(sorted(keys.items(), key=lambda kv: -kv[1])),
    }


# -- prefix property -------------------------------------------------------------------------
def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _copy_row(conn: sqlite3.Connection, table: str, row: sqlite3.Row,
              changes: dict[str, Any]) -> None:
    """Insert a copy of *row* with *changes*, provenance cleared, a fresh id."""
    cols = [c for c in _columns(conn, table) if c != "id"]
    values = {c: row[c] for c in cols if c in row.keys()}
    for provenance in ("import_batch_id", "source_row_hash"):
        if provenance in values:
            values[provenance] = None
    values.update(changes)
    names = [c for c in cols if c in values]
    conn.execute(f"INSERT INTO {table} ({', '.join(names)}) VALUES "
                 f"({', '.join('?' * len(names))})", [values[c] for c in names])


def _prefix(ws: H.Workspace, build: Callable[..., Any], reference: Any) -> dict[str, Any]:
    points = reference.trend.points
    if not points:
        return {"available": False, "reason": "no trend points"}
    first_day: date = points[0].date
    base = {p.date: p.model_dump_json() for p in points}
    probe = H.connect(ws.db)
    try:
        buy = probe.execute(
            "SELECT * FROM transactions WHERE side = 'BUY' AND symbol = ("
            "  SELECT symbol FROM transactions WHERE side = 'BUY' GROUP BY symbol"
            "  ORDER BY count(*) DESC, symbol LIMIT 1) ORDER BY trade_date DESC, id DESC LIMIT 1"
        ).fetchone()
        dividend = probe.execute(
            "SELECT * FROM dividends WHERE type = 'CASH' ORDER BY date DESC, id DESC LIMIT 1"
        ).fetchone()
        trade_days = [r[0] for r in probe.execute(
            "SELECT DISTINCT trade_date FROM transactions ORDER BY trade_date")]
    finally:
        probe.close()
    today = ws.now.date()
    span = (points[-1].date - first_day).days

    def offset(d: date) -> int:
        return (d - first_day).days

    checks: list[tuple[str, Callable[[sqlite3.Connection], date | None]]] = []
    if buy is not None:
        def add_buy(on: date) -> Callable[[sqlite3.Connection], date | None]:
            def mutate(c: sqlite3.Connection) -> date | None:
                _copy_row(c, "transactions", buy, {"trade_date": on.isoformat(),
                                                   "quantity": "1", "note": "prefix probe"})
                return on
            return mutate

        checks.append(("buy_dated_today", add_buy(today)))
        if trade_days:
            checks.append(("back_dated_buy_median_trade_day",
                           add_buy(date.fromisoformat(trade_days[len(trade_days) // 2]))))

        def restate_close(c: sqlite3.Connection) -> date | None:
            target = first_day + timedelta(days=span // 4)
            row = c.execute("SELECT instrument, as_of_date, close FROM prices WHERE "
                            "instrument = ? AND as_of_date >= ? ORDER BY as_of_date LIMIT 1",
                            (buy["symbol"], target.isoformat())).fetchone()
            if row is None:
                return None
            bumped = Decimal(str(row["close"])) * Decimal("1.01")
            c.execute("UPDATE prices SET close = ? WHERE instrument = ? AND as_of_date = ?",
                      (format(bumped, "f"), row["instrument"], row["as_of_date"]))
            return date.fromisoformat(row["as_of_date"])

        checks.append(("restated_close_quarter_way", restate_close))
    if dividend is not None:
        def add_dividend(c: sqlite3.Connection) -> date | None:
            _copy_row(c, "dividends", dividend, {"date": today.isoformat(), "ex_date": None})
            return today

        checks.append(("cash_dividend_dated_today", add_dividend))

    results: list[dict[str, Any]] = []
    for i, (label, mutate) in enumerate(checks):
        path = ws.scratch_copy(f"prefix-{i}.db")
        c = H.connect(path)
        try:
            d = mutate(c)
            c.commit()
            if d is None:
                results.append({"check": label, "skipped": "no row to mutate"})
                continue
            after, t = H.timed(partial(build, c))
        finally:
            c.close()
            path.unlink(missing_ok=True)
        after_points = {p.date: p.model_dump_json() for p in after.trend.points}
        before_d = [x for x in base if x < d]
        changed_before = [x for x in before_d if after_points.get(x) != base[x]]
        changed_from = [x for x in base if x >= d and after_points.get(x) != base[x]]
        results.append({
            "check": label, "day_offset": offset(d), "build": t.as_dict(),
            "points_before_day": len(before_d), "changed_before_day": len(changed_before),
            "changed_from_day": len(changed_from),
            "first_changed_offset": offset(min(changed_from)) if changed_from else None,
            "prefix_holds": not changed_before,
        })
        H.log(f"prefix {label}: changed before D {len(changed_before)}/{len(before_d)}")
    return {"available": True, "trend_days": len(points), "span_days": span,
            "checks": results}


if __name__ == "__main__":
    raise SystemExit(main())
