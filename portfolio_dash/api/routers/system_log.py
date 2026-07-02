"""GET /api/system-log — user-facing system action history (2026-07-03, item 8).

Read-only over the ``action_log`` table the app middleware writes. Protected by
the global session gate like every other data endpoint.
"""

import sqlite3

from fastapi import APIRouter, Depends, Query

from portfolio_dash.api.action_log import list_actions
from portfolio_dash.api.deps import get_conn

router = APIRouter()


@router.get("/system-log")
def system_log(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, object]:
    return list_actions(conn, limit=limit, offset=offset)
