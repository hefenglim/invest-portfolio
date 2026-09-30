"""GET /api/ledger-audit — the ledger audit trail, read-only (post-closure item 10).

Owner ruling 2026-09-30: every edit / delete of a ledger row already leaves its BEFORE image
in ``ledger_audit`` (audit M9); this is the route the 資料中心 「帳本操作稽核」 list reads it
through. Newest first, paged with the shared pager (``limit`` / ``offset`` +
``total_count``), each row presented by ``export/ledger_audit.py`` — the same presentation
the export centre's CSV uses, so the list and the file cannot disagree.

A GET never writes (DEF-065): the table is created at boot by the ledger schema, and this
route only SELECTs.
"""

import sqlite3

from fastapi import APIRouter, Depends, Query

from portfolio_dash.api.deps import get_conn
from portfolio_dash.export.ledger_audit import list_entries

router = APIRouter()

#: The pager's ceiling — the same one the other long lists use (``pdPrefs.page_size`` is
#: clamped to it on the page).
_MAX_LIMIT = 500


@router.get("/ledger-audit")
def list_ledger_audit(
    limit: int = Query(50, ge=1, le=_MAX_LIMIT),
    offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, object]:
    """One page of the audit trail, newest first, with the trail's total size."""
    entries, total = list_entries(conn, limit=limit, offset=offset)
    return {"rows": [e.to_wire() for e in entries], "total_count": total}
