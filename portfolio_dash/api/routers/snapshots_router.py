"""GET /api/snapshots — 月度成績 (R6 item 8; PERF spec 2026-10-18 §11.4).

The month rows, newest first: past months are their month-end closes (settled, or computed
live when the store does not hold them yet), the current month is live. MONTHS ONLY — the
year rows are ``GET /api/settlement/periods?kind=Y``. ``api/snapshots.py`` owns the rule.
"""

import sqlite3
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query

from portfolio_dash.api.deps import get_conn, get_now, get_reporting
from portfolio_dash.api.snapshots import snapshot_rows
from portfolio_dash.shared.enums import Currency

router = APIRouter()


@router.get("/snapshots")
def snapshots(
    limit: int = Query(24, ge=1, le=120),
    conn: sqlite3.Connection = Depends(get_conn, scope="function"),
    now: datetime = Depends(get_now),
    reporting: Currency = Depends(get_reporting),
) -> dict[str, Any]:
    """``{rows, total_count}``: the newest *limit* months (``total_count`` counts them all)."""
    return snapshot_rows(conn, limit=limit, now=now, reporting=reporting)
