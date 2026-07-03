"""資金管理 API (2026-07-03, R6 item 7): per-account cash pools on one seam.

GET /api/cash — balances per (account, ccy) + a best-effort reporting-ccy total
+ the movements ledger. Writes: deposits/withdrawals and FX conversions, both
guarded by the NEGATIVE-POOL check (item 2): an entry that would drive a pool
below zero answers 422 ``negative_cash`` until explicitly acked — a negative
pool almost always means a missed deposit/conversion, the cash analog of the
oversell guard. Corrections (edit/delete) follow the ledger discipline.
"""

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from portfolio_dash.api.deps import get_conn, get_now, get_reporting
from portfolio_dash.api.errors import error_body
from portfolio_dash.data_ingestion.store import (
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
from portfolio_dash.portfolio.cash import cash_balances
from portfolio_dash.pricing.store import get_fx
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.wire import decimal_str

router = APIRouter()

_ZERO = Decimal("0")
_KINDS = {"DEPOSIT", "WITHDRAW"}


def _balances(conn: sqlite3.Connection) -> dict[tuple[str, Currency], Decimal]:
    return cash_balances(
        list_cash_movements(conn),
        list_fx_conversions(conn),
        list_transactions(conn),
        list_dividends(conn),
        {i.symbol: i for i in list_instruments(conn)},
    )


def _negative_after(
    bal: dict[tuple[str, Currency], Decimal],
    deltas: list[tuple[str, Currency, Decimal]],
) -> tuple[str, Currency, Decimal] | None:
    """Apply deltas to a copy; the first pool that lands below zero, or None."""
    after = dict(bal)
    for account_id, ccy, delta in deltas:
        key = (account_id, ccy)
        after[key] = after.get(key, _ZERO) + delta
    for account_id, ccy, _delta in deltas:
        v = after[(account_id, ccy)]
        if v < _ZERO:
            return account_id, ccy, v
    return None


def _negative_response(hit: tuple[str, Currency, Decimal]) -> JSONResponse:
    account_id, ccy, v = hit
    return JSONResponse(status_code=422, content=error_body(
        "negative_cash",
        f"此筆會使 {account_id} 的 {ccy.value} 現金變為 {decimal_str(v)} — "
        "通常代表漏記入金或換匯；確認無誤可強制寫入"))


@router.get("/cash")
def cash_overview(
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
    reporting: Currency = Depends(get_reporting),
) -> dict[str, Any]:
    accounts = {a.account_id: a for a in list_accounts(conn)}
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
    # Best-effort reporting-ccy total (display-only conversion at latest stored
    # rate; None with a reason when any needed rate is missing — never fabricated).
    total = _ZERO
    total_ok = True
    reason: str | None = None
    for (_account_id, ccy), amount in bal.items():
        if ccy == reporting:
            total += amount
            continue
        read = get_fx(conn, ccy, reporting, now=now)
        if read is None:
            inv = get_fx(conn, reporting, ccy, now=now)
            if inv is None or inv.rate == _ZERO:
                total_ok = False
                reason = f"缺 {ccy.value}/{reporting.value} 匯率"
                break
            total += amount / inv.rate
        else:
            total += amount * read.rate
    movements = list(reversed(list_cash_movements(conn)))
    return {
        "balances": rows,
        "reporting_total": decimal_str(total) if total_ok else None,
        "reporting_currency": reporting.value,
        "reporting_total_unavailable_reason": None if total_ok else reason,
        "movements": {
            "rows": [
                {
                    "id": m.id, "date": m.date.isoformat(), "account_id": m.account_id,
                    "account": accounts[m.account_id].name
                    if m.account_id in accounts else m.account_id,
                    "kind": m.kind.lower(), "ccy": m.ccy.value,
                    "amount": decimal_str(m.amount), "note": m.note,
                }
                for m in movements
            ],
            "total_count": len(movements),
        },
    }


class MovementBody(BaseModel):
    account_id: str
    date: date
    kind: str  # deposit | withdraw
    ccy: Currency
    amount: Decimal
    note: str | None = None
    ack_negative: bool = False


def _movement_guard(
    conn: sqlite3.Connection, body: MovementBody
) -> JSONResponse | None:
    if body.kind.strip().upper() not in _KINDS:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", f"未知類型 {body.kind}（deposit / withdraw）",
            field="kind"))
    if body.amount <= _ZERO:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", "金額必須大於 0", field="amount"))
    if not any(a.account_id == body.account_id for a in list_accounts(conn)):
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", f"帳戶 {body.account_id} 不存在", field="account_id"))
    return None


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
        hit = _negative_after(
            _balances(conn), [(body.account_id, body.ccy, -body.amount)])
        if hit is not None:
            return _negative_response(hit)
    move_id = insert_cash_movement(
        conn, account_id=body.account_id, move_date=body.date, kind=kind,
        ccy=body.ccy, amount=body.amount, note=body.note)
    return {"id": move_id}


def _movement_delta(kind: str, amount: Decimal) -> Decimal:
    return amount if kind == "DEPOSIT" else -amount


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
        # pure delta check BEFORE writing: reverse the old row, apply the new one
        hit = _negative_after(_balances(conn), [
            (existing.account_id, existing.ccy,
             -_movement_delta(existing.kind, existing.amount)),
            (body.account_id, body.ccy, _movement_delta(kind, body.amount)),
        ])
        if hit is not None:
            return _negative_response(hit)
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
        hit = _negative_after(_balances(conn), [
            (existing.account_id, existing.ccy,
             -_movement_delta(existing.kind, existing.amount)),
        ])
        if hit is not None:
            return _negative_response(hit)
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
    """FX conversion entry with the negative-pool guard (item 2).

    Writes the SAME fx_conversions ledger row the CSV path writes — one ledger,
    two doors; this door checks the pool first.
    """
    if body.from_amt <= _ZERO or body.to_amt <= _ZERO:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", "兩側金額必須大於 0", field="from_amt"))
    if body.from_ccy is body.to_ccy:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", "換出與換入幣別不可相同", field="to_ccy"))
    if not any(a.account_id == body.account_id for a in list_accounts(conn)):
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", f"帳戶 {body.account_id} 不存在", field="account_id"))
    if not body.ack_negative:
        hit = _negative_after(
            _balances(conn), [(body.account_id, body.from_ccy, -body.from_amt)])
        if hit is not None:
            return _negative_response(hit)
    fx_id = insert_fx_conversion(
        conn, account_id=body.account_id, date=body.date, from_ccy=body.from_ccy,
        from_amount=body.from_amt, to_ccy=body.to_ccy, to_amount=body.to_amt)
    return {"id": fx_id}
