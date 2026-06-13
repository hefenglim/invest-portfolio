"""POST /api/export/* — reconciliation-grade downloads (spec 02). Thin orchestration.

Each route: build the artifact via portfolio_dash.export.*, write a job_runs audit row
(log_export_run), and return the bytes with a Content-Disposition attachment header.
The web layer computes no numbers of record.
"""

import sqlite3
from datetime import datetime

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from portfolio_dash.api.deps import get_conn, get_now, get_reporting
from portfolio_dash.api.errors import error_body
from portfolio_dash.export.artifact import ExportArtifact
from portfolio_dash.export.holdings import build_holdings_csv
from portfolio_dash.export.ledgers import build_ledgers_zip
from portfolio_dash.export.tax import build_tax_package_zip
from portfolio_dash.export.usage import build_job_runs_csv, build_llm_usage_csv
from portfolio_dash.scheduler.jobs import log_export_run
from portfolio_dash.shared.enums import Currency

router = APIRouter()


def _respond(art: ExportArtifact) -> Response:
    return Response(
        content=art.content,
        media_type=art.media_type,
        headers={"Content-Disposition": f'attachment; filename="{art.filename}"'},
    )


class RangeBody(BaseModel):
    frm: str | None = Field(default=None, alias="from")
    to: str | None = None
    model_config = {"populate_by_name": True}


class TaxPackageBody(BaseModel):
    year: int = Field(ge=1900, le=2200)


def _bad_range(body: RangeBody) -> JSONResponse | None:
    if body.frm and body.to and body.frm > body.to:
        return JSONResponse(
            status_code=400,
            content=error_body("validation_error", "日期區間無效", field="from"),
        )
    return None


@router.post("/export/holdings")
def export_holdings(
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
    reporting: Currency = Depends(get_reporting),
) -> Response:
    art = build_holdings_csv(conn, now=now, reporting=reporting)
    log_export_run(conn, "holdings", now=now,
                   detail=f"bytes={len(art.content)} file={art.filename}")
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


@router.post("/export/llm-usage")
def export_llm_usage(
    body: RangeBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Response:
    bad = _bad_range(body)
    if bad is not None:
        return bad
    art = build_llm_usage_csv(conn, frm=body.frm, to=body.to)
    log_export_run(conn, "llm_usage", now=now, detail=f"file={art.filename}")
    return _respond(art)


@router.post("/export/job-runs")
def export_job_runs(
    body: RangeBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Response:
    bad = _bad_range(body)
    if bad is not None:
        return bad
    art = build_job_runs_csv(conn, frm=body.frm, to=body.to)
    log_export_run(conn, "job_runs", now=now, detail=f"file={art.filename}")
    return _respond(art)


@router.post("/export/tax-package")
def export_tax_package(
    body: TaxPackageBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
    reporting: Currency = Depends(get_reporting),
) -> Response:
    art = build_tax_package_zip(conn, now=now, year=body.year, reporting=reporting)
    log_export_run(conn, "tax_package", now=now,
                   detail=f"year={body.year} file={art.filename}")
    return _respond(art)
