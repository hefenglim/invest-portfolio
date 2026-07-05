"""Input center API (spec 12): read context + manual/CSV/AI write paths (12a: context+manual)."""

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

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
from portfolio_dash.data_ingestion.fx_import import build_fx_preview, write_fx_row
from portfolio_dash.data_ingestion.holdings import current_shares
from portfolio_dash.data_ingestion.manual import enter_transaction
from portfolio_dash.data_ingestion.opening_import import (
    build_opening_preview,
    write_opening_row,
)
from portfolio_dash.data_ingestion.preview import ImportPreview, PreviewRow, commit_preview
from portfolio_dash.data_ingestion.store import list_accounts, list_instruments
from portfolio_dash.data_ingestion.validate import Issue, TxnInput
from portfolio_dash.shared.enums import Market
from portfolio_dash.shared.wire import decimal_str

router = APIRouter()

# The account's settlement currency determines the market a bare symbol entered
# under it belongs to (TW broker→TWD/TW, Schwab & Moomoo-US→USD/US, Moomoo-MY→
# MYR/MY) — the basis for auto-registering unknown symbols from trade input.
_CCY_MARKET = {"TWD": Market.TW, "USD": Market.US, "MYR": Market.MY}


def _account_market(conn: sqlite3.Connection, account_id: str) -> Market | None:
    row = conn.execute(
        "SELECT settlement_ccy FROM accounts WHERE account_id=?", (account_id,)
    ).fetchone()
    return _CCY_MARKET.get(row["settlement_ccy"]) if row is not None else None


@router.get("/input/context")
def context(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT account_id, fee_rule_set, dividend_model FROM accounts ORDER BY account_id"
    ).fetchall()
    meta = {r["account_id"]: r for r in rows}
    accts = list_accounts(conn)
    accounts_out = [
        {
            "id": a.account_id,
            "name": a.name,
            "ccy": a.settlement_ccy.value,
            "div_model": div_model_wire(meta[a.account_id]["dividend_model"]),
        }
        for a in accts
    ]
    fee_rules = {
        aid: fee_rules_wire(get_fee_rule_set(m["fee_rule_set"])) for aid, m in meta.items()
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
    shares: Decimal
    price: Decimal
    fee_override: Decimal | None = None
    tax_override: Decimal | None = None
    note: str | None = None
    ack_oversell: bool = False  # used by commit (Task 3)


def _txn_input(body: ManualBody) -> TxnInput:
    return TxnInput(
        account_id=body.account_id, symbol=body.symbol, side=parse_side(body.side),
        quantity=body.shares, price=body.price, trade_date=body.date,
        fee=body.fee_override, tax=body.tax_override, note=body.note,
    )


def _rule_for(conn: sqlite3.Connection, account_id: str) -> FeeRuleSet | None:
    row = conn.execute(
        "SELECT fee_rule_set FROM accounts WHERE account_id=?", (account_id,)
    ).fetchone()
    return get_fee_rule_set(row["fee_rule_set"]) if row is not None else None


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


@router.post("/input/manual/preview")
def manual_preview(
    body: ManualBody, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    draft = enter_transaction(conn, _txn_input(body), confirm=False)
    gross = body.shares * body.price
    total = (
        -(gross + draft.fee + draft.tax)
        if draft.inp.side.value == "BUY"
        else (gross - draft.fee - draft.tax)
    )
    rule = _rule_for(conn, body.account_id)
    return {
        "fee": decimal_str(draft.fee), "tax": decimal_str(draft.tax),
        "gross": decimal_str(gross), "total": decimal_str(total),
        "fee_rule_label": fee_rules_wire(rule)["label"] if rule is not None else None,
        "fee_overridden": body.fee_override is not None,
        "tax_overridden": body.tax_override is not None,
        "issues": [_issue_wire_manual(i, body.symbol) for i in draft.issues],
    }


@router.post("/input/manual/commit", status_code=201)
def manual_commit(
    body: ManualBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    inp = _txn_input(body)
    draft = enter_transaction(conn, inp, confirm=False)  # inspect, no write

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
        draft = enter_transaction(conn, inp, confirm=False)  # re-validate, resolved now

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
    written = enter_transaction(conn, inp, confirm=True)
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
