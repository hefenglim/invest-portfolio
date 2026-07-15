"""Input center API (spec 12): read context + manual/CSV/AI write paths (12a: context+manual)."""

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from portfolio_dash.api.deps import get_conn, get_now
from portfolio_dash.api.errors import error_body
from portfolio_dash.api.instrument_service import QuickRegisterError, quick_register
from portfolio_dash.api.wire import div_model_wire, fee_rules_wire, issue_wire, parse_side
from portfolio_dash.data_ingestion.agents import ai_agents_input
from portfolio_dash.data_ingestion.config_seed import FeeRuleSet, get_fee_rule_set
from portfolio_dash.data_ingestion.csv_import import (
    build_transaction_preview,
    write_transaction_row,
)
from portfolio_dash.data_ingestion.dividend_import import (
    build_dividend_preview,
    write_dividend_row,
)
from portfolio_dash.data_ingestion.fees import forecast_tw_rebate
from portfolio_dash.data_ingestion.fx_import import build_fx_preview, write_fx_row
from portfolio_dash.data_ingestion.holdings import current_shares
from portfolio_dash.data_ingestion.import_templates import (
    TEMPLATE_KINDS,
    render_import_template,
    template_filename,
)
from portfolio_dash.data_ingestion.manual import enter_transaction
from portfolio_dash.data_ingestion.markets import account_market as _account_market
from portfolio_dash.data_ingestion.opening_import import (
    build_opening_preview,
    write_opening_row,
)
from portfolio_dash.data_ingestion.preview import ImportPreview, PreviewRow, commit_preview
from portfolio_dash.data_ingestion.store import (
    list_accounts,
    list_cash_movements,
    list_dividends,
    list_fx_conversions,
    list_instruments,
    list_transactions,
)
from portfolio_dash.data_ingestion.validate import Issue, TxnInput
from portfolio_dash.export.artifact import content_disposition
from portfolio_dash.portfolio.cash import cash_balances
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.wire import decimal_str

router = APIRouter()

_ZERO = Decimal("0")
_BOM = "\ufeff"


@router.get("/input/context")
def context(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT account_id, fee_rule_set, dividend_model FROM accounts ORDER BY account_id"
    ).fetchall()
    meta = {r["account_id"]: r for r in rows}
    accts = list_accounts(conn)
    # `ccy` stays the settlement currency (back-compat); `funding_ccy` is added so the
    # cash page can constrain its currency dropdowns to {settlement, funding} (audit C2).
    accounts_out = [
        {
            "id": a.account_id,
            "name": a.name,
            "ccy": a.settlement_ccy.value,
            "settlement_ccy": a.settlement_ccy.value,
            "funding_ccy": a.funding_ccy.value,
            "div_model": div_model_wire(meta[a.account_id]["dividend_model"]),
        }
        for a in accts
    ]
    fee_rules = {
        aid: fee_rules_wire(get_fee_rule_set(m["fee_rule_set"], conn))
        for aid, m in meta.items()
    }
    insts = list_instruments(conn)
    instruments = [
        {
            "symbol": i.symbol,
            "name": i.name,
            "market": i.market.value,
            "ccy": i.quote_ccy.value,
            "etf": i.is_etf,
        }
        for i in insts
    ]
    holdings: dict[str, dict[str, str]] = {}
    for a in accts:
        per = {
            inst.symbol: decimal_str(sh)
            for inst in insts
            if (sh := current_shares(conn, a.account_id, inst.symbol)) != 0
        }
        if per:
            holdings[a.account_id] = per
    return {
        "accounts": accounts_out,
        "fee_rules": fee_rules,
        "instruments": instruments,
        "holdings": holdings,
    }


class ManualBody(BaseModel):
    account_id: str
    symbol: str
    side: str
    date: date
    # shares/price bounded (audit M4) so an overflow-sized value 400s here rather than
    # reaching the fee quantize as a 500. fee/tax overrides constrained >= 0 (audit H2).
    shares: Decimal = Field(le=Decimal("1e12"))
    price: Decimal = Field(le=Decimal("1e12"))
    fee_override: Decimal | None = Field(default=None, ge=0)
    tax_override: Decimal | None = Field(default=None, ge=0)
    daytrade: bool = False  # TW same-day round trip → 0.15% sell tax (persisted, MED-1)
    note: str | None = None
    ack_oversell: bool = False  # used by commit (Task 3)


def _txn_input(body: ManualBody) -> TxnInput:
    # is_etf is NOT taken from the body: the instrument registry is authoritative
    # (resolved at the fee-computation seam in manual.py / csv_import.py).
    return TxnInput(
        account_id=body.account_id, symbol=body.symbol, side=parse_side(body.side),
        quantity=body.shares, price=body.price, trade_date=body.date,
        fee=body.fee_override, tax=body.tax_override, daytrade=body.daytrade,
        note=body.note,
    )


def _rule_for(conn: sqlite3.Connection, account_id: str) -> FeeRuleSet | None:
    row = conn.execute(
        "SELECT fee_rule_set FROM accounts WHERE account_id=?", (account_id,)
    ).fetchone()
    return get_fee_rule_set(row["fee_rule_set"], conn) if row is not None else None


def _issue_wire_manual(issue: Issue, symbol: str) -> dict[str, Any]:
    """issue_wire + the manual-entry auto-register overlay (2026-07-02).

    ``symbol_unresolved`` is a HARD block at the data_ingestion layer (the ledger
    safety net stands), but the manual COMMIT path auto-registers unknown symbols,
    so the PREVIEW presents it as an info-severity note instead of an error — the
    confirm button must not be gated on a condition the commit resolves itself.
    """
    if issue.kind == "symbol_unresolved":
        return {
            "sev": "info", "code": "symbol_auto_register",
            "text": f"未註冊標的 {symbol} — 寫入時將自動查詢並註冊（查無報價則無法寫入）",
            "field": "symbol",
        }
    wired: dict[str, Any] = issue_wire(issue)
    return wired


def _cash_overdraft_issue(
    conn: sqlite3.Connection, body: ManualBody, draft_fee: Decimal, draft_tax: Decimal
) -> Issue | None:
    """Soft overdraft warning (audit C1b): only when the account opted into cash tracking
    (>=1 cash movement) AND this BUY would push the instrument's cash pool below zero.

    Never a hard block — users may not track cash at all (only fires once they do).
    """
    if parse_side(body.side) is not Side.BUY:
        return None
    inst = next((i for i in list_instruments(conn) if i.symbol == body.symbol), None)
    if inst is None:  # unregistered symbol: pool ccy unknown until auto-register — skip
        return None
    tracked = conn.execute(
        "SELECT 1 FROM cash_movements WHERE account_id=? LIMIT 1", (body.account_id,)
    ).fetchone()
    if tracked is None:
        return None
    bal = cash_balances(
        list_cash_movements(conn), list_fx_conversions(conn), list_transactions(conn),
        list_dividends(conn), {i.symbol: i for i in list_instruments(conn)},
    )
    current = bal.get((body.account_id, inst.quote_ccy), _ZERO)
    cost = body.shares * body.price + draft_fee + draft_tax
    if current - cost < _ZERO:
        return Issue(
            kind="cash_overdraft",
            needs_confirm=True,
            message=f"此帳戶 {inst.quote_ccy.value} 現金將不足(可能漏登入金或換匯),確認要寫入?",
        )
    return None


@router.post("/input/manual/preview")
def manual_preview(
    body: ManualBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    draft = enter_transaction(conn, _txn_input(body), confirm=False, today=now.date())
    gross = body.shares * body.price
    total = (
        -(gross + draft.fee + draft.tax)
        if draft.inp.side.value == "BUY"
        else (gross - draft.fee - draft.tax)
    )
    rule = _rule_for(conn, body.account_id)
    issues = list(draft.issues)
    overdraft = _cash_overdraft_issue(conn, body, draft.fee, draft.tax)
    if overdraft is not None:
        issues.append(overdraft)
    # FE-D1 forecast HINT (informational, 不計入成本): the TW charge-first rebate on next
    # month's refund = floor(resolved fee × rebate_rate). Null when the account never rebates
    # (rebate_rate 0 — every non-TW rule) so the UI only shows the line where it applies.
    rebate_estimate = (
        decimal_str(forecast_tw_rebate(draft.fee, rule.rebate_rate))
        if rule is not None and rule.rebate_rate > _ZERO
        else None
    )
    return {
        "fee": decimal_str(draft.fee), "tax": decimal_str(draft.tax),
        "gross": decimal_str(gross), "total": decimal_str(total),
        "fee_rule_label": fee_rules_wire(rule)["label"] if rule is not None else None,
        "fee_overridden": body.fee_override is not None,
        "tax_overridden": body.tax_override is not None,
        "rebate_estimate": rebate_estimate,
        "issues": [_issue_wire_manual(i, body.symbol) for i in issues],
    }


@router.post("/input/manual/commit", status_code=201)
def manual_commit(
    body: ManualBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    today = now.date()
    inp = _txn_input(body)
    draft = enter_transaction(conn, inp, confirm=False, today=today)  # inspect, no write

    # Auto-register an unknown symbol (2026-07-02): infer the market from the
    # account's settlement currency and run the one-step registration (which
    # REQUIRES a real quote — the typo guard). Success -> the entry proceeds as if
    # the symbol had been registered first; failure -> a clear 400, nothing written.
    auto_registered: dict[str, Any] | None = None
    if any(i.kind == "symbol_unresolved" for i in draft.issues):
        market = _account_market(conn, body.account_id)
        if market is None:
            return JSONResponse(status_code=400, content=error_body(
                "validation_error", f"帳戶 {body.account_id} 不存在，無法自動註冊標的",
                field="account_id"))
        try:
            outcome = quick_register(
                conn, symbol=body.symbol, market=market, now=now, force=False)
        except QuickRegisterError as exc:
            if exc.code != "duplicate_symbol":  # duplicate = registered meanwhile -> proceed
                return JSONResponse(status_code=400, content=error_body(
                    "symbol_auto_register_failed",
                    f"自動註冊失敗：{exc.message}", field="symbol"))
        else:
            auto_registered = {
                "symbol": outcome.instrument.symbol,
                "name": outcome.instrument.name,
                "last": decimal_str(outcome.last) if outcome.last is not None else None,
            }
            body = body.model_copy(update={"symbol": outcome.instrument.symbol})
        inp = _txn_input(body)
        draft = enter_transaction(conn, inp, confirm=False, today=today)  # re-validate now

    hard = [i for i in draft.issues if not i.needs_confirm]
    if hard:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", hard[0].message,
            issues=[issue_wire(i) for i in draft.issues]))
    oversell = any(i.kind == "sell_exceeds_holdings" for i in draft.issues)
    if oversell and not body.ack_oversell:
        return JSONResponse(status_code=422, content=error_body(
            "oversell_unacknowledged", "需確認賣超",
            issues=[issue_wire(i) for i in draft.issues]))
    written = enter_transaction(conn, inp, confirm=True, today=today)
    gross = body.shares * body.price
    total = (-(gross + written.fee + written.tax) if inp.side.value == "BUY"
             else (gross - written.fee - written.tax))
    return {"txn_id": written.transaction_id, "total": decimal_str(total),
            "auto_registered": auto_registered}


# --- CSV import: preview (12.3) + commit (12.4) shared infrastructure ---

_BUILDERS = {
    "transactions": build_transaction_preview, "dividends": build_dividend_preview,
    "fx": build_fx_preview, "openings": build_opening_preview,
}
_WRITERS = {
    "transactions": write_transaction_row, "dividends": write_dividend_row,
    "fx": write_fx_row, "openings": write_opening_row,
}


def _row_status(row: PreviewRow) -> str:
    if row.has_hard_issue:
        return "error"
    return "warn" if row.issues else "ok"


def _row_data(row: PreviewRow) -> dict[str, Any]:
    data = {k: v for k, v in row.payload.items() if not k.startswith("snap.")}
    if row.fee is not None:
        data["fee"] = decimal_str(row.fee)
    if row.tax is not None:
        data["tax"] = decimal_str(row.tax)
    return data


def _preview_wire(preview: ImportPreview) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    counts = {"ok": 0, "warn": 0, "error": 0}
    for r in preview.rows:
        st = _row_status(r)
        counts[st] += 1
        rows.append({"n": r.index, "status": st,
                     "reason": r.issues[0].message if r.issues else None,
                     "data": _row_data(r)})
    return {"rows": rows, "summary": {"total": len(preview.rows), **counts}}


class ImportPreviewBody(BaseModel):
    kind: str
    csv_text: str


@router.post("/import/preview")
def import_preview(body: ImportPreviewBody, conn: sqlite3.Connection = Depends(get_conn)) -> Any:
    builder = _BUILDERS.get(body.kind)
    if builder is None:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", f"未知 kind: {body.kind}", field="kind"))
    return _preview_wire(builder(conn, body.csv_text))


class ImportCommitBody(BaseModel):
    kind: str
    csv_text: str
    ack_warnings: bool = False


@router.post("/import/commit")
def import_commit(body: ImportCommitBody, conn: sqlite3.Connection = Depends(get_conn)) -> Any:
    builder = _BUILDERS.get(body.kind)
    writer = _WRITERS.get(body.kind)
    if builder is None or writer is None:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", f"未知 kind: {body.kind}", field="kind"))
    preview = builder(conn, body.csv_text)  # re-derive (re-validate vs current ledger)
    has_warn = any((not r.has_hard_issue) and r.issues for r in preview.rows)
    if has_warn and not body.ack_warnings:
        return JSONResponse(status_code=422, content=error_body(
            "warnings_unacknowledged", "有警告列需確認後才寫入"))
    accept = {r.index for r in preview.rows if not r.has_hard_issue}
    summary = commit_preview(conn, preview, accept=accept, writer=writer)
    return {"written": len(summary.written), "skipped": len(summary.skipped)}


@router.get("/import/template")
def import_template(kind: str = "transactions") -> Response:
    """Download a CSV import template (canonical header + worked example rows) for *kind*.

    UTF-8 **with BOM** (so Excel opens the Chinese ``note`` column cleanly) + CRLF, mirroring
    the export download shape. The header is the parser's own column constant
    (:mod:`data_ingestion.import_templates`) — a single source guarded by the round-trip test.
    Unknown ``kind`` -> 400 (same envelope as the preview/commit routes).
    """
    if kind not in TEMPLATE_KINDS:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", f"未知 kind: {kind}", field="kind"))
    body = (_BOM + render_import_template(kind)).encode("utf-8")
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": content_disposition(template_filename(kind))},
    )


# --- AI agents input: NL text -> preview + meta + commit CSV (12.4) ---

_LLM_HTTP = {"budget_exceeded": 402, "ai_not_activated": 409,
             "llm_unavailable": 503, "llm_error": 503}


class AiBody(BaseModel):
    text: str


@router.post("/input/ai/preview")
def ai_preview(
    body: AiBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    result = ai_agents_input(conn, body.text, today=now.date())
    for r in result.preview.rows:
        for issue in r.issues:
            if issue.kind in _LLM_HTTP:
                return JSONResponse(status_code=_LLM_HTTP[issue.kind],
                                    content=error_body(issue.kind, issue.message))
    wire = _preview_wire(result.preview)
    wire["meta"] = {"model": result.meta.model, "via": result.meta.via,
                    "cost_usd": None if result.meta.cost_usd is None
                    else decimal_str(result.meta.cost_usd)}
    wire["csv_text"] = result.csv_text
    return wire
