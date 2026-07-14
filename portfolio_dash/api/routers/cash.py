"""資金管理 API (2026-07-03, R6 item 7): per-account cash pools on one seam.

GET /api/cash — balances per (account, ccy) + a best-effort reporting-ccy total
(skip-not-abort on a missing FX rate — a dust pool no longer nulls the whole total,
audit C6) + a negative-pool list (overdraft visibility, audit C1a) + the movements
ledger. Writes: deposits/withdrawals/openings and FX conversions, both guarded by the
DATE-AWARE running-balance check (audit C3): an entry that would drive the pool below
zero at ANY point in time answers 422 ``negative_cash`` until explicitly acked — a
negative pool almost always means a missed deposit/conversion, the cash analog of the
oversell guard. Currency↔account coherence is enforced too (audit C2): a movement/FX
leg must be in the account's {settlement, funding} currencies. GET /api/cash/statement
serves the merged, date-ordered flow timeline with a server-computed running balance
(audit C5). Corrections (edit/delete) follow the ledger discipline.
"""

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from portfolio_dash.api.deps import get_conn, get_now, get_reporting
from portfolio_dash.api.errors import error_body
from portfolio_dash.data_ingestion.store import (
    StoredCashMovement,
    StoredFxConversion,
    delete_cash_movement,
    get_cash_movement,
    insert_cash_movement,
    insert_fx_conversion,
    list_accounts,
    list_cash_movements,
    list_dividends,
    list_fx_conversions,
    list_instruments,
    list_transactions,
    update_cash_movement,
)
from portfolio_dash.portfolio.cash import cash_balances, pool_lines, running_min, running_statement
from portfolio_dash.pricing.store import get_fx
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.assets import Account
from portfolio_dash.shared.wire import decimal_str

router = APIRouter()

_ZERO = Decimal("0")
_KINDS = {"DEPOSIT", "WITHDRAW", "OPENING"}


def _accounts(conn: sqlite3.Connection) -> dict[str, Account]:
    return {a.account_id: a for a in list_accounts(conn)}


def _allowed_ccys(account: Account) -> set[Currency]:
    """The currencies a pool may legitimately hold: settlement + funding (audit C2)."""
    return {account.settlement_ccy, account.funding_ccy}


def _balances(conn: sqlite3.Connection) -> dict[tuple[str, Currency], Decimal]:
    return cash_balances(
        list_cash_movements(conn),
        list_fx_conversions(conn),
        list_transactions(conn),
        list_dividends(conn),
        {i.symbol: i for i in list_instruments(conn)},
    )


def _pool_min(
    conn: sqlite3.Connection,
    account_id: str,
    ccy: Currency,
    *,
    movements: list[StoredCashMovement] | None = None,
    fx: list[StoredFxConversion] | None = None,
) -> Decimal:
    """Minimum running balance of one pool over its date-ordered ledger (audit C3).

    Callers pass the WOULD-BE movement/fx list; unspecified ledgers load from store.
    """
    ms = movements if movements is not None else list_cash_movements(conn)
    fxs = fx if fx is not None else list_fx_conversions(conn)
    lines = pool_lines(
        account_id, ccy, ms, fxs, list_transactions(conn), list_dividends(conn),
        {i.symbol: i for i in list_instruments(conn)},
    )
    return running_min(lines)


def _negative_response(account_id: str, ccy: Currency, low: Decimal) -> JSONResponse:
    return JSONResponse(status_code=422, content=error_body(
        "negative_cash",
        f"此筆會使 {account_id} 的 {ccy.value} 現金於某時點降至 {decimal_str(low)} — "
        "通常代表漏記入金或換匯;確認無誤可強制寫入"))


@router.get("/cash")
def cash_overview(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
    reporting: Currency = Depends(get_reporting),
) -> dict[str, Any]:
    accounts = _accounts(conn)
    bal = _balances(conn)
    rows = [
        {
            "account_id": account_id,
            "account": accounts[account_id].name if account_id in accounts else account_id,
            "ccy": ccy.value,
            "amount": decimal_str(amount),
        }
        for (account_id, ccy), amount in sorted(
            bal.items(), key=lambda kv: (kv[0][0], kv[0][1].value))
    ]
    # Overdraft visibility across doors (audit C1a): every pool currently < 0.
    negative_pools = [
        {
            "account_id": account_id,
            "account": accounts[account_id].name if account_id in accounts else account_id,
            "ccy": ccy.value,
            "amount": decimal_str(amount),
        }
        for (account_id, ccy), amount in sorted(
            bal.items(), key=lambda kv: (kv[0][0], kv[0][1].value))
        if amount < _ZERO
    ]
    # Best-effort reporting-ccy total: SKIP a pool whose FX rate is missing and annotate
    # it, rather than nulling the whole total (audit C6).
    total = _ZERO
    excluded: list[dict[str, str]] = []
    for (account_id, ccy), amount in bal.items():
        if ccy == reporting:
            total += amount
            continue
        read = get_fx(conn, ccy, reporting, now=now)
        if read is not None:
            total += amount * read.rate
            continue
        inv = get_fx(conn, reporting, ccy, now=now)
        if inv is not None and inv.rate != _ZERO:
            total += amount / inv.rate
        else:
            excluded.append({
                "account_id": account_id, "ccy": ccy.value,
                "amount": decimal_str(amount),
            })
    reason = (
        None if not excluded
        else "部分幣別缺匯率已略過:" + "、".join(sorted({e["ccy"] for e in excluded}))
    )
    # WPE (2026-07-07): the movements ledger pages via limit/offset (additive — same
    # shape, total_count still counts the WHOLE ledger; balances untouched).
    movements = list(reversed(list_cash_movements(conn)))
    page = movements[offset:offset + limit]
    return {
        "balances": rows,
        "negative_pools": negative_pools,
        "reporting_total": decimal_str(total),
        "reporting_currency": reporting.value,
        "reporting_total_excluded": excluded,
        "reporting_total_unavailable_reason": reason,
        "movements": {
            "rows": [
                {
                    "id": m.id, "date": m.date.isoformat(), "account_id": m.account_id,
                    "account": accounts[m.account_id].name
                    if m.account_id in accounts else m.account_id,
                    "kind": m.kind.lower(), "ccy": m.ccy.value,
                    "amount": decimal_str(m.amount), "note": m.note,
                }
                for m in page
            ],
            "total_count": len(movements),
        },
    }


@router.get("/cash/statement")
def cash_statement(
    account: str = Query(...),
    ccy: Currency = Query(...),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    """Merged, date-ordered flow timeline for one (account, ccy) pool with a
    server-computed running balance (audit C5). Newest-first, paged; Decimal strings."""
    accounts = _accounts(conn)
    acct = accounts.get(account)
    if acct is None:
        return JSONResponse(status_code=404, content=error_body(
            "not_found", f"帳戶 {account} 不存在", field="account"))
    lines = pool_lines(
        account, ccy, list_cash_movements(conn), list_fx_conversions(conn),
        list_transactions(conn), list_dividends(conn),
        {i.symbol: i for i in list_instruments(conn)},
    )
    stmt = running_statement(lines)
    current_balance = stmt[-1][1] if stmt else _ZERO
    rows_all = [
        {
            "date": ln.date.isoformat(), "kind": ln.kind, "ref": ln.ref,
            "delta": decimal_str(ln.delta), "balance": decimal_str(bal),
        }
        for ln, bal in stmt
    ]
    rows_desc = list(reversed(rows_all))
    return {
        "account_id": account,
        "account": acct.name,
        "ccy": ccy.value,
        "current_balance": decimal_str(current_balance),
        "rows": rows_desc[offset:offset + limit],
        "total_count": len(rows_all),
    }


class MovementBody(BaseModel):
    account_id: str
    date: date
    kind: str  # deposit | withdraw | opening
    ccy: Currency
    amount: Decimal
    note: str | None = None
    ack_negative: bool = False


def _movement_guard(
    conn: sqlite3.Connection, body: MovementBody
) -> JSONResponse | None:
    if body.kind.strip().upper() not in _KINDS:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", f"未知類型 {body.kind}（deposit / withdraw / opening）",
            field="kind"))
    if body.amount <= _ZERO:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", "金額必須大於 0", field="amount"))
    acct = _accounts(conn).get(body.account_id)
    if acct is None:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", f"帳戶 {body.account_id} 不存在", field="account_id"))
    if body.ccy not in _allowed_ccys(acct):  # audit C2
        return JSONResponse(status_code=400, content=error_body(
            "validation_error",
            f"{body.ccy.value} 非此帳戶可用幣別"
            f"（交割幣 {acct.settlement_ccy.value}／資金幣 {acct.funding_ccy.value}）",
            field="ccy"))
    return None


def _synthetic_movement(body: MovementBody, kind: str) -> StoredCashMovement:
    return StoredCashMovement(
        id=0, account_id=body.account_id, date=body.date, kind=kind,
        ccy=body.ccy, amount=body.amount, note=body.note)


@router.post("/cash/movements", status_code=201)
def add_movement(
    body: MovementBody,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    bad = _movement_guard(conn, body)
    if bad is not None:
        return bad
    kind = body.kind.strip().upper()
    if kind == "WITHDRAW" and not body.ack_negative:
        would_be = [*list_cash_movements(conn), _synthetic_movement(body, kind)]
        low = _pool_min(conn, body.account_id, body.ccy, movements=would_be)
        if low < _ZERO:
            return _negative_response(body.account_id, body.ccy, low)
    move_id = insert_cash_movement(
        conn, account_id=body.account_id, move_date=body.date, kind=kind,
        ccy=body.ccy, amount=body.amount, note=body.note)
    return {"id": move_id}


@router.put("/cash/movements/{move_id}")
def edit_movement(
    move_id: int,
    body: MovementBody,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    existing = get_cash_movement(conn, move_id)
    if existing is None:
        return JSONResponse(status_code=404,
                            content=error_body("not_found", f"紀錄 #{move_id} 不存在"))
    bad = _movement_guard(conn, body)
    if bad is not None:
        return bad
    kind = body.kind.strip().upper()
    if not body.ack_negative:
        edited = _synthetic_movement(body, kind)
        would_be = [edited if m.id == move_id else m for m in list_cash_movements(conn)]
        # Any pool the edit touches (old or new account/ccy) must stay non-negative.
        for account_id, ccy in {
            (existing.account_id, existing.ccy), (body.account_id, body.ccy)
        }:
            low = _pool_min(conn, account_id, ccy, movements=would_be)
            if low < _ZERO:
                return _negative_response(account_id, ccy, low)
    update_cash_movement(
        conn, move_id, account_id=body.account_id, move_date=body.date,
        kind=kind, ccy=body.ccy, amount=body.amount, note=body.note)
    return {"ok": True, "id": move_id}


@router.delete("/cash/movements/{move_id}")
def remove_movement(
    move_id: int,
    ack_negative: bool = False,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    existing = get_cash_movement(conn, move_id)
    if existing is None:
        return JSONResponse(status_code=404,
                            content=error_body("not_found", f"紀錄 #{move_id} 不存在"))
    if not ack_negative:
        would_be = [m for m in list_cash_movements(conn) if m.id != move_id]
        low = _pool_min(conn, existing.account_id, existing.ccy, movements=would_be)
        if low < _ZERO:
            return _negative_response(existing.account_id, existing.ccy, low)
    delete_cash_movement(conn, move_id)
    return {"ok": True, "id": move_id}


class CashFxBody(BaseModel):
    account_id: str
    date: date
    from_ccy: Currency
    from_amt: Decimal
    to_ccy: Currency
    to_amt: Decimal
    ack_negative: bool = False


@router.post("/cash/fx", status_code=201)
def add_fx(
    body: CashFxBody,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    """FX conversion entry with the date-aware negative-pool guard (audit C3) and
    currency↔account coherence (audit C2).

    Writes the SAME fx_conversions ledger row the CSV path writes — one ledger,
    two doors; this door checks the pool first.
    """
    if body.from_amt <= _ZERO or body.to_amt <= _ZERO:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", "兩側金額必須大於 0", field="from_amt"))
    if body.from_ccy is body.to_ccy:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", "換出與換入幣別不可相同", field="to_ccy"))
    acct = _accounts(conn).get(body.account_id)
    if acct is None:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", f"帳戶 {body.account_id} 不存在", field="account_id"))
    allowed = _allowed_ccys(acct)
    for leg in (body.from_ccy, body.to_ccy):  # audit C2: both legs must be allowed
        if leg not in allowed:
            return JSONResponse(status_code=400, content=error_body(
                "validation_error",
                f"{leg.value} 非此帳戶可用幣別"
                f"（交割幣 {acct.settlement_ccy.value}／資金幣 {acct.funding_ccy.value}）",
                field="from_ccy" if leg is body.from_ccy else "to_ccy"))
    if not body.ack_negative:
        synthetic = StoredFxConversion(
            id=0, account_id=body.account_id, date=body.date, from_ccy=body.from_ccy,
            from_amount=body.from_amt, to_ccy=body.to_ccy, to_amount=body.to_amt)
        would_be = [*list_fx_conversions(conn), synthetic]
        low = _pool_min(conn, body.account_id, body.from_ccy, fx=would_be)
        if low < _ZERO:
            return _negative_response(body.account_id, body.from_ccy, low)
    fx_id = insert_fx_conversion(
        conn, account_id=body.account_id, date=body.date, from_ccy=body.from_ccy,
        from_amount=body.from_amt, to_ccy=body.to_ccy, to_amount=body.to_amt)
    return {"id": fx_id}
