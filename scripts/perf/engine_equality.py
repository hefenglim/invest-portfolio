"""Replay a ledger per event day with the frozen v0.1.31 engine and with the live one.

    python scripts/perf/engine_equality.py --db PATH --now ISO --out DIR [--engine NAME ...]

PERF plan W0.4 / W0.2: the per-event-day oracle a new engine must equal. The database is
COPIED to a temp directory first and only the copy is opened; ``--db`` and ``--out`` must lie
OUTSIDE the repository (it is public — the real scratch ledgers this is pointed at must never
be read in place, nor a derived file land in a checkout).

The ledger is read exactly as ``build_dashboard`` reads it (unregistered rows dropped, cut at
``--now``'s day), and for every day some row counts from the reference book
``ref_build_book(bundle.through(d), allow_oversell=True)`` is compared with each engine's book
for that day by :func:`tests.reference.canon.canon` (exact Decimal digits, field order,
excluded fields included). The report names the FIRST differing day per engine and is
written to ``<out>/engine_equality.json``; nothing in it is an amount — only days, counts,
timings and the PATH of the first difference (``--show-values`` adds the two values).
Exit status 0 = every engine equal, 1 = a difference, 2 = usage error.

Engines are a registry (:data:`ENGINES`): ``live`` is today's ``build_book`` per day. The
next wave adds its one-pass engine (``cost_basis.iter_books``) as another entry.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_ROOT = str(Path(__file__).resolve().parents[2])
if sys.path[:1] != [_ROOT]:
    sys.path.insert(0, _ROOT)

from portfolio_dash.data_ingestion.store import load_ledger_bundle  # noqa: E402
from portfolio_dash.portfolio.cost_basis import build_book  # noqa: E402
from portfolio_dash.portfolio.results import Book  # noqa: E402
from portfolio_dash.shared.models.ledger import LedgerBundle  # noqa: E402
from scripts.perf._refcommon import copy_db_to_temp, refuse_inside_repo  # noqa: E402
from tests.reference.canon import canon, first_difference  # noqa: E402
from tests.reference.corpora import open_db  # noqa: E402
from tests.reference.cost_basis_v0131 import ref_build_book  # noqa: E402

TAIPEI = ZoneInfo("Asia/Taipei")
REPORT = "engine_equality.json"

#: ``(bundle, days) -> (day, book)`` for each requested day, in order.
Engine = Callable[[LedgerBundle, list[date]], Iterable[tuple[date, Book]]]


def _live_per_day(bundle: LedgerBundle, days: list[date]) -> Iterator[tuple[date, Book]]:
    for d in days:
        yield d, build_book(bundle.through(d), allow_oversell=True)


ENGINES: dict[str, Engine] = {
    "live": _live_per_day,
}


def _parse_now(text: str) -> datetime:
    now = datetime.fromisoformat(text)
    return now if now.tzinfo is not None else now.replace(tzinfo=TAIPEI)


def load_bundle(db: Path, now: datetime) -> LedgerBundle:
    """The ledger as ``build_dashboard`` values it at *now* (steps 1, 1b, 1c)."""
    conn = open_db(db)
    try:
        bundle = load_ledger_bundle(conn)
    finally:
        conn.close()
    if bundle.unregistered_symbols:
        bundle = bundle.without_unregistered()
    return bundle.valued_as_of(now.date())


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", type=Path, required=True, help="the SQLite ledger (copied first)")
    parser.add_argument("--now", required=True,
                        help="the valuation clock, ISO 8601 (naive = Asia/Taipei)")
    parser.add_argument("--out", type=Path, required=True, help="the report directory")
    parser.add_argument("--engine", action="append", choices=sorted(ENGINES), default=[],
                        help="an engine to compare (repeatable; default: all)")
    parser.add_argument("--show-values", action="store_true",
                        help="print the differing values too (they are amounts)")
    args = parser.parse_args(argv)
    refuse_inside_repo(args.db, "--db")
    refuse_inside_repo(args.out, "--out")
    now = _parse_now(args.now)

    copy, tmp = copy_db_to_temp(args.db)
    try:
        bundle = load_bundle(copy, now)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    report = {"now": now.isoformat(),
              **compare(bundle, args.engine or sorted(ENGINES), show_values=args.show_values)}
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / REPORT).write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n",
                                   encoding="utf-8")
    for name, result in report["engines"].items():
        verdict = "equal" if result["equal"] else f"FIRST DIFFERENCE {result['first_difference']}"
        print(f"{name}: {result['days_compared']}/{report['event_days']} days, {verdict}")
    return 0 if report["equal"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
