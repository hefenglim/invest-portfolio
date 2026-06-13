"""POST /api/export/* — reconciliation-grade downloads (spec 02). Thin orchestration.

Each route: build the artifact via portfolio_dash.export.*, write a job_runs audit row
(log_export_run), and return the bytes with a Content-Disposition attachment header.
The web layer computes no numbers of record.
"""

import sqlite3
from datetime import datetime

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from portfolio_dash.api.deps import get_conn, get_now, get_reporting
from portfolio_dash.export.artifact import ExportArtifact
from portfolio_dash.export.holdings import build_holdings_csv
from portfolio_dash.export.ledgers import build_ledgers_zip
from portfolio_dash.scheduler.jobs import log_export_run
from portfolio_dash.shared.enums import Currency

router = APIRouter()


def _respond(art: ExportArtifact) -> Response:
    return Response(
        content=art.content,
        media_type=art.media_type,
        headers={"Content-Disposition": f'attachment; filename="{art.filename}"'},
    )


@router.post("/export/holdings")
def export_holdings(
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
    reporting: Currency = Depends(get_reporting),
) -> Response:
    art = build_holdings_csv(conn, now=now, reporting=reporting)
    log_export_run(conn, "holdings", now=now,
                   detail=f"rows_bytes={len(art.content)} file={art.filename}")
    return _respond(art)


@router.post("/export/ledgers")
def export_ledgers(
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Response:
    art = build_ledgers_zip(conn, now=now)
    log_export_run(conn, "ledgers", now=now,
                   detail=f"bytes={len(art.content)} file={art.filename}")
    return _respond(art)
