"""GET /api/snapshots — 月度快照 read view (R6 item 8)."""

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Query

from portfolio_dash.api.deps import get_conn
from portfolio_dash.api.snapshots import list_snapshots

router = APIRouter()


@router.get("/snapshots")
def snapshots(
    limit: int = Query(24, ge=1, le=120),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    rows = list_snapshots(conn, limit=limit)
    return {"rows": rows, "total_count": len(rows)}
