"""待確認匯入 API (2026-07-03, R4 item 1): FinMind dividend detection inbox.

GET computes the inbox fresh (optionally refreshing events from the providers
first); confirm/skip act on server-recomputed items only. 絕不自動入帳.
"""

import sqlite3
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from portfolio_dash.api import dividend_inbox as inbox
from portfolio_dash.api.deps import get_conn, get_now
from portfolio_dash.shared.wire import to_wire

router = APIRouter()


@router.get("/dividend-inbox")
def list_inbox(
    refresh: bool = False,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    refreshed: str | None = None
    if refresh:
        refreshed = inbox.refresh_events_for_acquired(conn, now=now)
    rows = inbox.detect(conn, now=now)
    return {
        "rows": [to_wire(r.model_dump()) for r in rows],
        "total_count": len(rows),
        "refreshed": refreshed,
    }


@router.get("/dividend-inbox/count")
def inbox_count(
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> dict[str, int]:
    """Pending-count for the sidebar badge (R6 item 4) — detection on read."""
    return {"count": len(inbox.detect(conn, now=now))}


class FingerprintsBody(BaseModel):
    fingerprints: list[str]


@router.post("/dividend-inbox/confirm")
def confirm(
    body: FingerprintsBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    written = inbox.confirm(conn, body.fingerprints, now=now)
    return {"written": len(written), "ids": written}


@router.post("/dividend-inbox/skip")
def skip(
    body: FingerprintsBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    for fp in body.fingerprints:
        inbox.mark_skipped(conn, fp, now=now)
    return {"skipped": len(body.fingerprints)}
