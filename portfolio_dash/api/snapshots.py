"""月度快照 (2026-07-03, R6 item 8): month-end KPI snapshots, queryable forever.

One row per month, upserted: the daily scheduler runner writes the CURRENT
month's snapshot every evening, so the value standing when the month rolls over
IS the month-end record (mid-month manual runs are simply overwritten by later
ones). Reads are a table lookup — the review view never replays history.
"""

import json
import sqlite3
from datetime import datetime

from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.shared.config import get_settings
from portfolio_dash.shared.wire import decimal_str

_DDL = """
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    month TEXT PRIMARY KEY,
    as_of TEXT NOT NULL,
    reporting_ccy TEXT NOT NULL,
    total_value TEXT,
    total_return TEXT,
    total_return_rate TEXT,
    xirr TEXT,
    by_currency TEXT NOT NULL
);
"""


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    conn.commit()


def write_snapshot(conn: sqlite3.Connection, *, now: datetime) -> str:
    """Compute the KPIs via the SAME combiner the dashboard uses and upsert.

    Optional KPIs (stale prices / missing FX) store NULL — honest degradation,
    never fabricated. Returns a short run summary.
    """
    ensure_table(conn)
    reporting = get_settings().reporting_currency
    data = build_dashboard(conn, now=now, reporting=reporting)
    k = data.kpis
    month = now.strftime("%Y-%m")
    by_ccy = {
        ccy.value: decimal_str(v)
        for ccy, v in data.currency_view.by_currency_value.items()
    } if data.currency_view is not None else {}
    conn.execute(
        "INSERT INTO portfolio_snapshots (month, as_of, reporting_ccy, total_value, "
        "total_return, total_return_rate, xirr, by_currency) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(month) DO UPDATE SET as_of=excluded.as_of, "
        "reporting_ccy=excluded.reporting_ccy, total_value=excluded.total_value, "
        "total_return=excluded.total_return, "
        "total_return_rate=excluded.total_return_rate, xirr=excluded.xirr, "
        "by_currency=excluded.by_currency",
        (
            month,
            now.isoformat(),
            reporting.value,
            decimal_str(k.total_market_value) if k.total_market_value is not None else None,
            decimal_str(k.total_return) if k.total_return is not None else None,
            decimal_str(k.total_return_rate) if k.total_return_rate is not None else None,
            decimal_str(k.xirr) if k.xirr is not None else None,
            json.dumps(by_ccy),
        ),
    )
    conn.commit()
    return f"快照已寫入 {month}"


def snapshot_job(conn: sqlite3.Connection, *, now: datetime) -> str:
    """Scheduler runner: refresh the current month's snapshot daily.

    The row standing at month rollover is the month-end record (upsert-by-month).
    """
    return write_snapshot(conn, now=now)


def list_snapshots(conn: sqlite3.Connection, *, limit: int = 24) -> list[dict[str, object]]:
    ensure_table(conn)
    rows = conn.execute(
        "SELECT month, as_of, reporting_ccy, total_value, total_return, "
        "total_return_rate, xirr, by_currency FROM portfolio_snapshots "
        "ORDER BY month DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        {
            "month": r["month"],
            "as_of": r["as_of"],
            "reporting_ccy": r["reporting_ccy"],
            "total_value": r["total_value"],
            "total_return": r["total_return"],
            "total_return_rate": r["total_return_rate"],
            "xirr": r["xirr"],
            "by_currency": json.loads(r["by_currency"] or "{}"),
        }
        for r in rows
    ]
