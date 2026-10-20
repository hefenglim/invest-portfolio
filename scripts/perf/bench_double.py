"""The data-doubling projection on a copy of a database (PERF plan W0.4, from ``bench_double``).

A second copy gets every transaction and dividend dated before ``--now`` repeated one day later
(provenance cleared), so both the row count and the number of event days roughly double while
every position stays non-negative (the copy is the original ledger shifted by a day). The full
dashboard build and the simulated settled build (``_harness.SettledSimulation``) are timed on
both sizes, and the settled payload must equal the full one byte for byte on each (exit 1 when
it does not, naming the first differing top-level field).

Output: ``<out>/double.json`` (timings, counts and sizes only).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _harness as H  # noqa: E402

_PROVENANCE = ("import_batch_id", "source_row_hash")


def _shift(day: str | None) -> str | None:
    return None if day is None else (date.fromisoformat(day) + timedelta(days=1)).isoformat()


def _repeat_shifted(conn: sqlite3.Connection, table: str, date_cols: tuple[str, ...],
                    before: str) -> int:
    """Insert a +1-day copy of every *table* row whose first date column is before *before*."""
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})") if r[1] != "id"]
    rows = conn.execute(f"SELECT * FROM {table} WHERE {date_cols[0]} < ?", (before,)).fetchall()
    sql = (f"INSERT INTO {table} ({', '.join(cols)}) "
           f"VALUES ({', '.join('?' * len(cols))})")
    for row in rows:
        values = {c: row[c] for c in cols}
        for col in date_cols:
            if col in values:
                values[col] = _shift(values[col])
        for col in _PROVENANCE:
            if col in values:
                values[col] = None
        conn.execute(sql, [values[c] for c in cols])
    return len(rows)


def build_doubled(source: Path, target: Path, now: date) -> dict[str, int]:
    """*target* = *source* with its pre-*now* transactions and dividends repeated +1 day."""
    H.copy_database(source, target)
    conn = H.connect(target)
    try:
        before = now.isoformat()
        added = {
            "transactions": _repeat_shifted(conn, "transactions", ("trade_date",), before),
            "dividends": _repeat_shifted(conn, "dividends", ("date", "ex_date"), before),
        }
        conn.commit()
    finally:
        conn.close()
    return added


def main(argv: list[str] | None = None) -> int:
    parser = H.common_parser(__doc__.splitlines()[0] if __doc__ else "bench double")
    parser.add_argument("--repeat", type=int, default=3, help="settled builds to time")
    args = parser.parse_args(argv)
    ws = H.open_workspace(args)
    try:
        return _run(ws, args)
    finally:
        ws.close()


def _measure(label: str, path: Path, ws: H.Workspace, repeat: int) -> dict[str, Any]:
    from portfolio_dash.portfolio import dashboard
    from portfolio_dash.shared.config import get_settings

    reporting = get_settings().reporting_currency
    conn = H.connect(path)
    try:
        def build() -> Any:
            return dashboard._build_dashboard(conn, now=ws.now, reporting=reporting)

        shape = H.ledger_shape(conn)
        event_days = int(conn.execute(
            "SELECT count(*) FROM (SELECT trade_date AS d FROM transactions "
            "UNION SELECT date FROM dividends)").fetchone()[0])
        full, t_full = H.timed(build)
        out: dict[str, Any] = {
            "label": label, "shape": shape, "event_days": event_days,
            "trend_days": len(full.trend.points), "full": t_full.as_dict(),
        }
        sim = H.SettledSimulation(dashboard)
        if not sim.available:
            out.update(settled_available=False, missing=sim.missing)
            return out
        best: H.Timing | None = None
        with sim:
            sim.prime(build)
            settled = full
            for _ in range(max(1, repeat)):
                settled, t = H.timed(build)
                if best is None or t.wall_s < best.wall_s:
                    best = t
        assert best is not None
        full_json, settled_json = full.model_dump_json(), settled.model_dump_json()
        out.update(settled_available=True, settled_best=best.as_dict(),
                   identical=full_json == settled_json)
        if full_json != settled_json:
            out["first_differing_field"] = H.first_difference(
                json.loads(full_json), json.loads(settled_json))
        H.log(f"{label:8} full {t_full.wall_s:7.2f}s  settled {best.wall_s:6.2f}s")
        return out
    finally:
        conn.close()


def _run(ws: H.Workspace, args: argparse.Namespace) -> int:
    doubled_path = ws.tmp / "doubled.db"
    added = build_doubled(ws.db, doubled_path, ws.now.date())
    rows = [_measure("today", ws.db, ws, args.repeat),
            _measure("doubled", doubled_path, ws, args.repeat)]
    H.write_json(ws, "double", {
        "kind": "bench_double", "environment": H.environment_info(ws),
        "rows_added": added, "sizes": rows,
    })
    return 0 if all(r.get("identical", True) for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
