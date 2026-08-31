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
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from portfolio_dash.api.deps import get_conn, get_now, get_reporting
from portfolio_dash.api.errors import error_body
from portfolio_dash.export.ai_predictions import build_ai_predictions_csv
from portfolio_dash.export.artifact import ExportArtifact, content_disposition
from portfolio_dash.export.cash_statement import (
    build_cash_statement_csv,
    build_cash_statement_report_html,
)
from portfolio_dash.export.holdings import build_holdings_csv
from portfolio_dash.export.holdings_report import build_holdings_report_html
from portfolio_dash.export.ledgers import LEDGER_KINDS, build_ledger_csv, build_ledgers_zip
from portfolio_dash.export.ledgers_report import build_ledgers_report_html
from portfolio_dash.export.realized import build_realized_csv
from portfolio_dash.export.rebalance_report import build_rebalance_report_html
from portfolio_dash.export.symbol_detail import build_symbol_detail_csv
from portfolio_dash.export.tax import build_tax_package_zip
from portfolio_dash.export.usage import build_job_runs_csv, build_llm_usage_csv
from portfolio_dash.portfolio.cost_basis import OversellError, UnbookableLedgerError
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.ledger_registry import EXPORT_KINDS

router = APIRouter()


class HoldingsFilterBody(BaseModel):
    """Optional (account, market) filter for the holdings CSV / report exports so a download
    follows the dashboard's active chips. Both optional; an absent or empty body ({}, or no
    body at all) means the full, unfiltered snapshot (identical to the legacy behaviour).

    ``extra="forbid"`` (audit L3, 2026-07-26): a MISSPELLED filter key used to be dropped
    silently by Pydantic's default, so a caller asking for one account received the WHOLE
    portfolio — with a footer that read ``filter: account=all``, giving no hint that the
    request had been ignored. Silently WIDENING the scope of a reconciliation export is the
    worst failure mode available to it, so an unknown key is now a loud 422."""

    model_config = {"extra": "forbid"}

    account: str | None = None
    market: Market | None = None


def _respond(art: ExportArtifact) -> Response:
    # Content-Disposition is built via content_disposition() so a user-derived filename
    # component (symbol / date range) can never inject a header or crash header encoding.
    return Response(
        content=art.content,
        media_type=art.media_type,
        headers={"Content-Disposition": content_disposition(art.filename)},
    )


class RangeBody(BaseModel):
    frm: str | None = Field(default=None, alias="from")
    to: str | None = None
    model_config = {"populate_by_name": True, "extra": "forbid"}


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
    body: HoldingsFilterBody | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
    reporting: Currency = Depends(get_reporting),
) -> Response:
    # Optional (account, market) filter follows the dashboard chips; no body -> full set.
    account = body.account if body is not None else None
    market = body.market if body is not None else None
    return _respond(build_holdings_csv(
        conn, now=now, reporting=reporting, account=account, market=market))


@router.get("/export/ledgers")
def list_export_ledgers() -> Any:
    """Which ledgers the zip below contains — the SAME set, from the same declaration.

    Exists so the export centre can name them without holding its own copy of the list.
    It held one until 2026-08-16, written as 「四帳本（期初/交易/股利/換匯）」, and it went
    stale the moment corporate actions joined the zip: the sentence under-reported what the
    user was about to download, and nothing could have caught it, because a hand-written
    count is not wrong in any way a test can see.

    Deliberately **not** served off ``/api/db-stats``, which also derives from the registry
    but lists all SIX ledgers. Filtering ``cash_movements`` out on the client would put
    "which ledgers are exportable" back in the frontend — the exact knowledge this endpoint
    exists to keep in one place. ``export_kind is None`` is that answer, and it is a
    property of the registry row, not of the caller.

    GET beside the POST of the same path is the pairing it looks like: GET says what the
    artifact would contain, POST builds it.
    """
    return {"ledgers": [{"kind": kind, "label": t.label}
                        for kind, t in EXPORT_KINDS.items()]}


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
    body: HoldingsFilterBody | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
    reporting: Currency = Depends(get_reporting),
) -> Response:
    # Print-optimized 持倉報告 (self-contained HTML). Optional (account, market) filter
    # follows the dashboard chips; empty/absent body -> the full unfiltered report.
    account = body.account if body is not None else None
    market = body.market if body is not None else None
    return _respond(build_holdings_report_html(
        conn, now=now, reporting=reporting, account=account, market=market))


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
    # never-500 at every build_book call site: an un-bookable ledger (a dividend inside
    # an open-short window) is a user-fixable data problem, not an internal error.
    try:
        art = build_tax_package_zip(conn, now=now, year=body.year, reporting=reporting)
    except UnbookableLedgerError as exc:
        return JSONResponse(status_code=422, content=error_body(
            "unbookable_ledger", str(exc)))
    except OversellError as exc:
        # QA-06: ``OversellError`` is a SEPARATE hierarchy (``Exception``, not ``ValueError``,
        # and ``cost_basis.py`` says so on purpose), so the arm above never caught it — and one
        # acknowledged 賣超 took the tax package down with a 500 while /api/dashboard and the
        # realized / holdings / holdings-report exports all answered 200 on the SAME ledger.
        # 賣超 is a state this app is designed to survive; surviving it everywhere but here is
        # the defect. The exact shape ``strategy/whatif.py`` already fixed at this same seam.
        #
        # ``build_tax_package_zip``'s ``build_book`` stays STRICT (``allow_oversell`` defaults
        # False) — a tax package must not silently omit a sale — so this degrades with the
        # REASON rather than relaxing the strictness, and names the offending row (the error
        # carries account/symbol/date precisely so no caller has to regex a sentence for them).
        return JSONResponse(status_code=422, content=error_body(
            "oversold_position",
            f"帳本中有賣超部位待釐清（{exc.account_id}／{exc.symbol}，"
            f"{exc.trade_date.isoformat()}）— 無法產生稅務套件，請先修正該筆交易",
            issues=[{
                "sev": "error",
                "code": "oversold_position",
                "text": str(exc),
                "field": None,
                "account_id": exc.account_id,
                "symbol": exc.symbol,
                "trade_date": exc.trade_date.isoformat(),
            }]))
    return _respond(art)


class CashStatementBody(BaseModel):
    account: str
    ccy: Currency | None = None  # None = the account's all-currency statement (FU-D5)


@router.post("/export/cash-statement")
def export_cash_statement(
    body: CashStatementBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Response:
    # 現金收支明細 CSV for one account (all pools when ccy is null), from the SAME
    # pool_lines/running_statement seam the statement view uses. Unknown account -> 400.
    art = build_cash_statement_csv(conn, account=body.account, ccy=body.ccy, now=now)
    if art is None:
        return JSONResponse(
            status_code=400,
            content=error_body("validation_error", f"未知帳戶：{body.account}", field="account"),
        )
    return _respond(art)


@router.post("/export/cash-statement-report")
def export_cash_statement_report(
    body: CashStatementBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Response:
    # Print-optimized 現金收支明細 report (one section per pool). Unknown account -> 400.
    art = build_cash_statement_report_html(conn, account=body.account, ccy=body.ccy, now=now)
    if art is None:
        return JSONResponse(
            status_code=400,
            content=error_body("validation_error", f"未知帳戶：{body.account}", field="account"),
        )
    return _respond(art)


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
