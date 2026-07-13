"""POST /api/export/* — reconciliation-grade downloads (spec 02). Thin orchestration.

Each route builds its artifact via portfolio_dash.export.* and returns the bytes
with a Content-Disposition attachment header. The web layer computes no numbers
of record.

Audit (2026-07-03, human decision): exports are USER ACTIONS, not scheduler jobs
— they are recorded by the 系統操作記錄 middleware (action_log), and no longer
write ``job_runs`` rows (the old ``log_export_run`` seam), so the 排程執行歷史
stays a pure scheduler view.
"""

import sqlite3
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from portfolio_dash.api.deps import get_conn, get_now, get_reporting
from portfolio_dash.api.errors import error_body
from portfolio_dash.export.artifact import ExportArtifact
from portfolio_dash.export.holdings import build_holdings_csv
from portfolio_dash.export.ledgers import build_ledgers_zip
from portfolio_dash.export.rebalance_report import build_rebalance_report_html
from portfolio_dash.export.tax import build_tax_package_zip
from portfolio_dash.export.usage import build_job_runs_csv, build_llm_usage_csv
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
    return _respond(build_holdings_csv(conn, now=now, reporting=reporting))


@router.post("/export/ledgers")
def export_ledgers(
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Response:
    return _respond(build_ledgers_zip(conn, now=now))


@router.post("/export/llm-usage")
def export_llm_usage(
    body: RangeBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Response:
    bad = _bad_range(body)
    if bad is not None:
        return bad
    return _respond(build_llm_usage_csv(conn, frm=body.frm, to=body.to))


@router.post("/export/job-runs")
def export_job_runs(
    body: RangeBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Response:
    bad = _bad_range(body)
    if bad is not None:
        return bad
    return _respond(build_job_runs_csv(conn, frm=body.frm, to=body.to))


@router.post("/export/tax-package")
def export_tax_package(
    body: TaxPackageBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
    reporting: Currency = Depends(get_reporting),
) -> Response:
    return _respond(
        build_tax_package_zip(conn, now=now, year=body.year, reporting=reporting)
    )


class RebalanceReportBody(BaseModel):
    targets: dict[str, Decimal]  # symbol -> reporting-ccy weight RATIO (Decimal string)


@router.post("/export/rebalance-report")
def export_rebalance_report(
    body: RebalanceReportBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
    reporting: Currency = Depends(get_reporting),
) -> Response:
    # Validation PARITY with POST /api/rebalance/preview (api/routers/strategy.py
    # ::post_rebalance): a non-decimal ratio is a Pydantic 422 in both; reject a negative
    # ratio with the SAME 400 validation_error / field=targets shape (few lines duplicated).
    for symbol, ratio in body.targets.items():
        if ratio < Decimal("0"):
            return JSONResponse(status_code=400, content=error_body(
                "validation_error", f"{symbol} 目標權重不可為負", field="targets"))
    return _respond(
        build_rebalance_report_html(conn, now=now, reporting=reporting, targets=body.targets)
    )
