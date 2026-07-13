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
from portfolio_dash.export.ai_predictions import build_ai_predictions_csv
from portfolio_dash.export.artifact import ExportArtifact
from portfolio_dash.export.holdings import build_holdings_csv
from portfolio_dash.export.holdings_report import build_holdings_report_html
from portfolio_dash.export.ledgers import LEDGER_KINDS, build_ledger_csv, build_ledgers_zip
from portfolio_dash.export.ledgers_report import build_ledgers_report_html
from portfolio_dash.export.realized import build_realized_csv
from portfolio_dash.export.rebalance_report import build_rebalance_report_html
from portfolio_dash.export.symbol_detail import build_symbol_detail_csv
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


class LedgerBody(RangeBody):
    kind: str  # one of LEDGER_KINDS: transactions | dividends | fx | opening


class SymbolBody(BaseModel):
    symbol: str


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


@router.post("/export/ledger")
def export_ledger(
    body: LedgerBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Response:
    # Single reconciliation-grade CSV for the 交易帳本 page's ACTIVE tab, range-filtered.
    # Unknown kind -> 400; inverted range -> the SAME 400 validation_error as the other
    # range exports.
    if body.kind not in LEDGER_KINDS:
        return JSONResponse(
            status_code=400,
            content=error_body("validation_error", f"未知帳本類型：{body.kind}", field="kind"),
        )
    bad = _bad_range(body)
    if bad is not None:
        return bad
    return _respond(build_ledger_csv(conn, kind=body.kind, frm=body.frm, to=body.to))


@router.post("/export/realized")
def export_realized(
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
    reporting: Currency = Depends(get_reporting),
) -> Response:
    # 已實現損益 snapshot from the ledger-replay core. Empty JSON body {} — no parameters.
    return _respond(build_realized_csv(conn, now=now, reporting=reporting))


@router.post("/export/ai-predictions")
def export_ai_predictions(
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Response:
    # AI 洞察 預測明細 battle record from the evaluations store. Empty JSON body {}.
    return _respond(build_ai_predictions_csv(conn))


@router.post("/export/symbol-detail")
def export_symbol_detail(
    body: SymbolBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Response:
    # Symbol drawer 配息史 from the dividend ledger. Unknown symbol -> 400.
    art = build_symbol_detail_csv(conn, symbol=body.symbol)
    if art is None:
        return JSONResponse(
            status_code=400,
            content=error_body("validation_error", f"未知標的：{body.symbol}", field="symbol"),
        )
    return _respond(art)


@router.post("/export/holdings-report")
def export_holdings_report(
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
    reporting: Currency = Depends(get_reporting),
) -> Response:
    # Print-optimized 持倉報告 (self-contained HTML). Empty JSON body {} — no parameters.
    return _respond(build_holdings_report_html(conn, now=now, reporting=reporting))


@router.post("/export/ledgers-report")
def export_ledgers_report(
    body: RangeBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Response:
    # Print-optimized 帳本報告 over [from, to]. SAME RangeBody + 400 validation as the other
    # range exports (from > to -> validation_error / field=from).
    bad = _bad_range(body)
    if bad is not None:
        return bad
    return _respond(build_ledgers_report_html(conn, now=now, frm=body.frm, to=body.to))


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
