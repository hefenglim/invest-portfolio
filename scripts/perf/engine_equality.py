"""Replay a ledger per event day with the frozen v0.1.31 engine and with the live one.

    python scripts/perf/engine_equality.py --db PATH --now ISO --out DIR [--engine NAME ...]
        [--reporting TWD] [--no-series]

PERF plan W0.4 / W0.2 / §7.3 and spec §18 "Engine": the oracle a new engine must equal. The
database is COPIED to a temp directory first and only the copy is opened; ``--db`` and
``--out`` must lie OUTSIDE the repository (it is public — the real scratch ledgers this is
pointed at must never be read in place, nor a derived file land in a checkout; to run it on the
stress audit's synthetic ``scripts/stress_audit/evidence/phase1.db``, copy that file out first).

Two families, each against the FROZEN v0.1.31 code in ``tests/reference/``:

* **books** — the ledger is read exactly as ``build_dashboard`` reads it (unregistered rows
  dropped, cut at ``--now``'s day), and for every day some row counts from the reference book
  ``ref_build_book(bundle.through(d), allow_oversell=True)`` is compared with each engine's book
  for that day. Engines are a registry (:data:`ENGINES`): ``live`` is today's ``build_book`` per
  day, ``iter_books`` the one-pass engine (``cost_basis.iter_books``, spec §8.1).
* **series** (spec §18: "``value_series`` ≡ ``daily_value_series``; the exactness guard's
  fallback forced and equal") — the one-pass trend the dashboard and the settlement run use
  (``timeseries.value_series`` over ``iter_books`` books, from the trend's first day) against the
  frozen ``ref_daily_value_series``, on the inputs ``build_dashboard`` assembles
  (``tests/reference/dashboard_inputs.load_inputs``: v0.1.31's trend FX pair set, kept for both
  engines — PERF-F2's shape included). Run twice: as it runs, and with
  ``_running_sum_is_exact`` forced ``False`` so ``net_invested`` takes the reference per-day
  sum — which must then actually have run (``_reference_net_invested`` called) whenever the
  trend is available.

Comparison is :func:`tests.reference.canon.canon` (exact Decimal digits and exponents, field
order, ``exclude=True`` fields included). The report names the FIRST differing day per engine
(and the first differing path of each series) and is written to ``<out>/engine_equality.json``;
nothing in it is an amount — only days, counts, timings and PATHS (``--show-values`` adds the
two values). Exit status 0 = everything equal, 1 = a difference, 2 = usage error.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_ROOT = str(Path(__file__).resolve().parents[2])
if sys.path[:1] != [_ROOT]:
    sys.path.insert(0, _ROOT)

from portfolio_dash.data_ingestion.store import load_ledger_bundle  # noqa: E402
from portfolio_dash.portfolio import timeseries as ts  # noqa: E402
from portfolio_dash.portfolio.cost_basis import build_book, iter_books  # noqa: E402
from portfolio_dash.portfolio.dashboard_models import TrendSeries  # noqa: E402
from portfolio_dash.portfolio.results import Book  # noqa: E402
from portfolio_dash.shared.enums import Currency  # noqa: E402
from portfolio_dash.shared.models.ledger import LedgerBundle  # noqa: E402
from scripts.perf._refcommon import copy_db_to_temp, refuse_inside_repo  # noqa: E402
from tests.reference import trend_v0131 as ref_trend  # noqa: E402
from tests.reference.canon import canon, first_difference  # noqa: E402
from tests.reference.corpora import open_db  # noqa: E402
from tests.reference.cost_basis_v0131 import ref_build_book  # noqa: E402
from tests.reference.dashboard_inputs import DashboardInputs, load_inputs  # noqa: E402

TAIPEI = ZoneInfo("Asia/Taipei")
REPORT = "engine_equality.json"

#: ``(bundle, days) -> (day, book)`` for each requested day, in order.
Engine = Callable[[LedgerBundle, list[date]], Iterable[tuple[date, Book]]]


def _live_per_day(bundle: LedgerBundle, days: list[date]) -> Iterator[tuple[date, Book]]:
    for d in days:
        yield d, build_book(bundle.through(d), allow_oversell=True)


def _one_pass(bundle: LedgerBundle, days: list[date]) -> Iterator[tuple[date, Book]]:
    yield from zip(days, iter_books(bundle, days), strict=True)


ENGINES: dict[str, Engine] = {
    "live": _live_per_day,
    "iter_books": _one_pass,
}


def _parse_now(text: str) -> datetime:
    now = datetime.fromisoformat(text)
    return now if now.tzinfo is not None else now.replace(tzinfo=TAIPEI)


def _bundle_of(conn: sqlite3.Connection, now: datetime) -> LedgerBundle:
    bundle = load_ledger_bundle(conn)
    if bundle.unregistered_symbols:
        bundle = bundle.without_unregistered()
    return bundle.valued_as_of(now.date())


def load_bundle(db: Path, now: datetime) -> LedgerBundle:
    """The ledger as ``build_dashboard`` values it at *now* (steps 1, 1b, 1c)."""
    conn = open_db(db)
    try:
        return _bundle_of(conn, now)
    finally:
        conn.close()


def _where(expected: Any, got: Any, *, show_values: bool) -> str:
    """The path of the first difference; its VALUES only with ``--show-values`` (they are
    amounts, and this report may be made from a real ledger)."""
    found = first_difference(expected, got) or ""
    return found if show_values else found.split(": ", 1)[0]


def compare(bundle: LedgerBundle, engines: list[str], *, show_values: bool = False
            ) -> dict[str, Any]:
    days = sorted(bundle.counts_on_dates())
    t0 = time.perf_counter()
    reference = [canon(ref_build_book(bundle.through(d), allow_oversell=True)) for d in days]
    report: dict[str, Any] = {
        "event_days": len(days),
        "first_day": days[0].isoformat() if days else None,
        "last_day": days[-1].isoformat() if days else None,
        "reference_seconds": round(time.perf_counter() - t0, 3),
        "engines": {},
    }
    for name in engines:
        t1 = time.perf_counter()
        first: dict[str, str] | None = None
        seen = 0
        for (d, book), (expected_day, expected) in zip(ENGINES[name](bundle, days),
                                                       zip(days, reference, strict=True),
                                                       strict=True):
            seen += 1
            if d != expected_day:
                first = {"day": expected_day.isoformat(), "where": f"engine yielded {d}"}
                break
            got = canon(book)
            if got != expected:
                first = {"day": d.isoformat(),
                         "where": _where(expected, got, show_values=show_values)}
                break
        report["engines"][name] = {
            "equal": first is None,
            "days_compared": seen,
            "first_difference": first,
            "seconds": round(time.perf_counter() - t1, 3),
        }
    report["equal"] = all(e["equal"] for e in report["engines"].values())
    return report


def one_pass_series(i: DashboardInputs) -> TrendSeries:
    """The trend as the dashboard and the settlement run build it (spec §8.2): ONE
    ``iter_books`` pass over :func:`timeseries.trend_book_days`, the whole bundle's refusals
    from one replay, every day from the trend's first (so no day before the start exists and
    ``options_open_before`` is ``False``)."""
    first = ts.trend_start(i.bundle)
    if first is None:
        return TrendSeries(points=[], reporting_currency=i.reporting, available=False)
    days = ts.trend_book_days(i.bundle, start=first, end=i.as_of)
    return ts.value_series(
        i.bundle, i.price_history, i.trend_fx_history, i.reporting, start=first, end=i.as_of,
        cash_movements=i.cash_movements,
        refused_dividends=build_book(i.bundle, allow_oversell=True).refused_dividends,
        books=zip(days, iter_books(i.bundle, days), strict=True), options_open_before=False)


def compare_series(i: DashboardInputs, *, show_values: bool = False) -> dict[str, Any]:
    """``value_series`` ≡ the frozen ``daily_value_series``, as it runs and with the
    exactness guard's fallback forced (spec §18 "Engine")."""
    if not i.has_events or ts.trend_start(i.bundle) is None:
        return {"equal": True, "skipped": "no ledger event: no trend"}
    t0 = time.perf_counter()
    reference = ref_trend.ref_daily_value_series(
        i.bundle, i.price_history, i.trend_fx_history, i.reporting, end=i.as_of,
        cash_movements=i.cash_movements)
    expected = canon(reference)
    out: dict[str, Any] = {
        "available": reference.available,
        "points": len(reference.points),
        "reference_seconds": round(time.perf_counter() - t0, 3),
    }

    def run(label: str, *, forced: bool) -> None:
        calls = [0]
        real_exact, real_reference = ts._running_sum_is_exact, ts._reference_net_invested

        def counted(converted: Any, day: date) -> Decimal:
            calls[0] += 1
            return real_reference(converted, day)

        t1 = time.perf_counter()
        if forced:
            ts._running_sum_is_exact = lambda amounts: False
            ts._reference_net_invested = counted
        try:
            got = canon(one_pass_series(i))
        finally:
            ts._running_sum_is_exact = real_exact
            ts._reference_net_invested = real_reference
        equal = got == expected
        result: dict[str, Any] = {
            "equal": equal,
            "first_difference": None if equal else _where(expected, got,
                                                          show_values=show_values),
            "seconds": round(time.perf_counter() - t1, 3),
        }
        if forced:
            # The fallback must have RUN, or "forced and equal" proves nothing.
            result["fallback_calls"] = calls[0]
            if reference.available and calls[0] == 0:
                result["equal"] = False
                result["first_difference"] = "the forced fallback never ran"
        out[label] = result

    run("value_series", forced=False)
    run("value_series_fallback", forced=True)
    out["equal"] = out["value_series"]["equal"] and out["value_series_fallback"]["equal"]
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", type=Path, required=True, help="the SQLite ledger (copied first)")
    parser.add_argument("--now", required=True,
                        help="the valuation clock, ISO 8601 (naive = Asia/Taipei)")
    parser.add_argument("--out", type=Path, required=True, help="the report directory")
    parser.add_argument("--engine", action="append", choices=sorted(ENGINES), default=[],
                        help="an engine to compare (repeatable; default: all)")
    parser.add_argument("--reporting", default="TWD", choices=[c.value for c in Currency],
                        help="the reporting currency of the trend (default TWD)")
    parser.add_argument("--no-series", action="store_true",
                        help="compare the books only, not the one-pass trend")
    parser.add_argument("--show-values", action="store_true",
                        help="print the differing values too (they are amounts)")
    args = parser.parse_args(argv)
    refuse_inside_repo(args.db, "--db")
    refuse_inside_repo(args.out, "--out")
    now = _parse_now(args.now)

    copy, tmp = copy_db_to_temp(args.db)
    try:
        conn = open_db(copy)
        try:
            bundle = _bundle_of(conn, now)
            inputs = (None if args.no_series
                      else load_inputs(conn, now, Currency(args.reporting)))
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    report = {"now": now.isoformat(),
              **compare(bundle, args.engine or sorted(ENGINES), show_values=args.show_values)}
    if inputs is not None:
        report["series"] = compare_series(inputs, show_values=args.show_values)
        report["equal"] = report["equal"] and report["series"]["equal"]
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / REPORT).write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n",
                                   encoding="utf-8")
    for name, result in report["engines"].items():
        verdict = "equal" if result["equal"] else f"FIRST DIFFERENCE {result['first_difference']}"
        print(f"{name}: {result['days_compared']}/{report['event_days']} days, {verdict}")
    series = report.get("series")
    if series is not None:
        if "skipped" in series:
            print(f"series: skipped ({series['skipped']})")
        for label in ("value_series", "value_series_fallback"):
            if label in series:
                r = series[label]
                verdict = "equal" if r["equal"] else f"FIRST DIFFERENCE {r['first_difference']}"
                extra = (f", fallback ran {r['fallback_calls']}x"
                         if "fallback_calls" in r else "")
                print(f"{label}: {series['points']} points, {verdict}{extra}")
    return 0 if report["equal"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
