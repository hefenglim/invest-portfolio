"""Time every GET ``/api`` route on a copy of a database (PERF plan W0.4, from ``bench_all``).

Per route: wall and CPU time of the first call (and of a second call when the first cost
≥ ``--second-call-threshold`` seconds), status, response bytes, gzip size, SQL statement count
and the tables touched, peak RSS growth, and how often each heavy calculation function ran.

Usage (from the checkout to measure; see ``_harness`` for the isolation rules)::

    python scripts/perf/bench_routes.py --db D:/scratch/x.db --now 2026-10-13T13:30 --out D:/out

The auth gate is overridden (a route's cost, not the login, is what is measured), and the
clock dependency ``get_now`` is pinned to ``--now``. A ``{symbol}`` route is called with the
copy's most-traded symbol and with a less-traded one, labelled ``SYM_MOST_TRADED`` /
``SYM_LESS_TRADED`` — the symbols themselves are never written. Other parameterised routes are
listed as skipped. Output: ``<out>/routes.json``.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _harness as H  # noqa: E402

#: Routes never called: they change session state.
SKIP = frozenset({"/api/auth/logout"})
PLACEHOLDERS = ("SYM_MOST_TRADED", "SYM_LESS_TRADED")


def _symbol_samples(conn: sqlite3.Connection) -> dict[str, str]:
    """placeholder -> a real symbol from the copy (used in the call, never written out)."""
    rows = conn.execute(
        "SELECT symbol FROM transactions GROUP BY symbol ORDER BY count(*) DESC, symbol"
    ).fetchall()
    symbols = [r[0] for r in rows]
    out: dict[str, str] = {}
    if symbols:
        out["SYM_MOST_TRADED"] = symbols[0]
    if len(symbols) > 1:
        out["SYM_LESS_TRADED"] = symbols[min(3, len(symbols) - 1)]
    return out


def main(argv: list[str] | None = None) -> int:
    parser = H.common_parser(__doc__.splitlines()[0] if __doc__ else "bench routes")
    parser.add_argument("--only", default="", help="comma-separated route templates")
    parser.add_argument("--skip", default="", help="comma-separated route templates")
    parser.add_argument("--second-call-threshold", type=float, default=0.25)
    parser.add_argument("--count", action="append", metavar="LABEL=MODULE:QUALNAME",
                        help="an extra function to count calls of (repeatable)")
    args = parser.parse_args(argv)
    ws = H.open_workspace(args)
    try:
        return _run(ws, args)
    finally:
        ws.close()


def _run(ws: H.Workspace, args: argparse.Namespace) -> int:
    # App imports only after open_workspace pointed DB_PATH at the copy.
    from fastapi.routing import APIRoute
    from fastapi.testclient import TestClient

    from portfolio_dash.api.app import create_app
    from portfolio_dash.api.auth_store import require_session
    from portfolio_dash.api.deps import get_now

    sql = H.SqlCounter()
    sql.install()
    counter = H.CallCounter(H.parse_count_targets(args.count))
    only = {r for r in args.only.split(",") if r}
    skip = SKIP | {r for r in args.skip.split(",") if r}

    probe = H.connect(ws.db)
    try:
        shape = H.ledger_shape(probe)
        samples = _symbol_samples(probe)
    finally:
        probe.close()

    app = create_app()
    app.dependency_overrides[get_now] = lambda: ws.now
    app.dependency_overrides[require_session] = lambda: None
    rows: list[dict[str, Any]] = []
    skipped: list[str] = []
    with TestClient(app) as client:
        boot_rss = H.rss_mb()
        calls: list[tuple[str, str]] = []
        for route in app.routes:
            if not isinstance(route, APIRoute) or "GET" not in route.methods:
                continue
            path = route.path
            if path in skip or (only and path not in only):
                continue
            if "{" not in path:
                calls.append((path, path))
            elif path.count("{") == 1 and "{symbol}" in path:
                for placeholder in PLACEHOLDERS:
                    if placeholder in samples:
                        calls.append((path.replace("{symbol}", placeholder),
                                      path.replace("{symbol}", samples[placeholder])))
            else:
                skipped.append(path)
        calls.sort()
        for label, real_path in calls:
            rec: dict[str, Any] = {"route": label}
            for attempt in (1, 2):
                counter.reset()
                with H.RssPeak() as peak, sql.window():
                    def _get(p: str = real_path) -> tuple[int | str, bytes]:
                        try:
                            resp = client.get(p)
                        except Exception as exc:  # noqa: BLE001 - recorded by class only
                            return f"exception:{type(exc).__name__}", b""
                        return resp.status_code, resp.content
                    (status, body), t = H.timed(_get)
                rec[f"call{attempt}"] = t.as_dict()
                if attempt == 1:
                    rec.update(status=status, bytes=len(body), gzip_bytes=H.gzip_size(body),
                               sql=sql.snapshot(), rss_growth_mb=peak.result(),
                               calls=counter.snapshot())
                if t.wall_s < args.second_call_threshold:
                    break
            rows.append(rec)
            first = rec["call1"]
            H.log(f"{first['wall_s']:8.3f}s cpu {first['cpu_s']:7.3f}s "
                  f"{rec['status']} {rec['bytes']:>9}B {label}")
    counter.close()
    sql.uninstall()
    slowest = sorted(rows, key=lambda r: -float(r["call1"]["wall_s"]))[:25]
    H.write_json(ws, "routes", {
        "kind": "bench_routes",
        "environment": H.environment_info(ws),
        "shape": shape,
        "rss_after_boot_mb": boot_rss,
        "missing_count_targets": counter.missing,
        "skipped_param_routes": sorted(skipped),
        "routes": rows,
        "slowest": [{"route": r["route"], **r["call1"]} for r in slowest],
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
