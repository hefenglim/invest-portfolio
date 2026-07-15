"""待確認退款（折讓款）API (Wave B, FE-D1): a compute-on-read forecast + confirm inbox.

GET computes the pending-rebate list fresh (mirrors the dividend inbox — self-healing, no
pending rows stored); confirm books a cash-pool CREDIT (movement kind ``rebate``) through
the SAME guards as ``POST /api/cash/movements``, with an EDITABLE amount (the estimate is
only a prefill — the actual refund wins; the estimate is never money of record, FE-D1).
Ungated in guest mode, matching the dividend-inbox siblings.
"""

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from portfolio_dash.api import rebates as svc
from portfolio_dash.api.deps import get_conn, get_now
from portfolio_dash.api.errors import error_body
from portfolio_dash.api.routers.cash import MovementBody, _movement_guard
from portfolio_dash.data_ingestion.config_seed import get_fee_rule_set
from portfolio_dash.data_ingestion.store import insert_cash_movement, list_accounts
from portfolio_dash.shared.wire import decimal_str, to_wire

router = APIRouter()

_ZERO = Decimal("0")


@router.get("/rebates")
def list_rebates(
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    pending = svc.detect(conn, now=now)
    skipped = svc.list_skipped(conn, now=now)
    return {
        "rows": [to_wire(p.model_dump()) for p in pending],
        "total_count": len(pending),
        "skipped": [to_wire(s.model_dump()) for s in skipped],
    }


@router.get("/rebates/count")
def rebates_count(
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> dict[str, int]:
    """Pending-rebate count for the sidebar badge (summed with the dividend inbox)."""
    return {"count": svc.pending_count(conn, now=now)}


def _next_month_first(month: str) -> date:
    """First day of the month AFTER *month* — when the refund is expected/booked."""
    my, mm = int(month[:4]), int(month[5:7])
    return date(my + 1, 1, 1) if mm == 12 else date(my, mm + 1, 1)


class ConfirmBody(BaseModel):
    account_id: str
    month: str
    amount: Decimal  # the EDITABLE actual amount (estimate is only the prefill)


@router.post("/rebates/confirm")
def confirm(
    body: ConfirmBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    """Book a confirmed rebate as a cash-pool credit; recompute/validate server-side.

    400s on: unknown / non-rebate account, a month that is not currently pending, or a
    non-positive amount. The note tag is RE-DERIVED here (client copy is display only) so the
    booked month suppresses itself on the next detect.
    """
    accounts = {a.account_id: a for a in list_accounts(conn)}
    acct = accounts.get(body.account_id)
    rule_row = conn.execute(
        "SELECT fee_rule_set FROM accounts WHERE account_id=?", (body.account_id,)
    ).fetchone()
    rebate_rate = (
        get_fee_rule_set(rule_row["fee_rule_set"], conn).rebate_rate if rule_row is not None
        else _ZERO
    )
    if acct is None or rebate_rate <= _ZERO:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", f"帳戶 {body.account_id} 無折讓款設定", field="account_id"))

    pending_months = {p.month for p in svc.detect(conn, now=now)
                      if p.account_id == body.account_id}
    if body.month not in pending_months:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", f"{body.month} 尚無待確認折讓款（可能已入帳、已略過或未到次月）",
            field="month"))
    if body.amount <= _ZERO:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", "折讓金額必須大於 0", field="amount"))

    note = svc.month_tag(body.month)
    refund_date = _next_month_first(body.month)
    # Reuse the cash-movements door guards (kind whitelist / amount / account / ccy coherence).
    guard = MovementBody(
        account_id=body.account_id, date=refund_date, kind="rebate",
        ccy=acct.settlement_ccy, amount=body.amount, note=note)
    bad = _movement_guard(conn, guard)
    if bad is not None:
        return bad
    move_id = insert_cash_movement(
        conn, account_id=body.account_id, move_date=refund_date, kind=svc.REBATE_KIND,
        ccy=acct.settlement_ccy, amount=body.amount, note=note)
    return {
        "id": move_id, "account_id": body.account_id, "month": body.month,
        "amount": decimal_str(body.amount), "ccy": acct.settlement_ccy.value,
    }


class MonthBody(BaseModel):
    account_id: str
    month: str


@router.post("/rebates/skip")
def skip(
    body: MonthBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> dict[str, int]:
    svc.mark_skipped(conn, body.account_id, body.month, now=now)
    return {"skipped": 1}


@router.post("/rebates/unskip")
def unskip(
    body: MonthBody,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, int]:
    """Un-skip a month so it re-surfaces (mirrors the dividend-inbox unskip — guest-open)."""
    removed = svc.unskip(conn, [(body.account_id, body.month)])
    return {"unskipped": removed}
