"""資金管理 API (2026-07-03, R6 item 7): per-account cash pools on one seam.

GET /api/cash — balances per (account, ccy) + a best-effort reporting-ccy total
(skip-not-abort on a missing FX rate — a dust pool no longer nulls the whole total,
audit C6) + a negative-pool list (overdraft visibility, audit C1a) + the movements
ledger. Writes: deposits/withdrawals/openings and FX conversions. WITHDRAWALS (FU-D43a)
and FX conversions (FU-D34) are HARD-blocked when the pool cannot cover them — 422
``withdraw_insufficient_balance`` / ``fx_insufficient_balance``, NO ack override, no
financing. Deposit/opening-side mutations (edits/deletes that shrink funding) keep the
DATE-AWARE running-balance check (audit C3): a change that would drive the pool below
zero at ANY point in time answers 422 ``negative_cash`` until explicitly acked — a
negative pool almost always means a missed deposit/conversion, the cash analog of the
oversell guard. Currency↔account coherence is enforced too (audit C2): a movement/FX
leg must be in the account's {settlement, funding} currencies. GET /api/cash/statement
serves the merged, date-ordered flow timeline with a server-computed running balance
(audit C5). GET /api/cash/fx-estimate (FU-D43c) serves the SERVER-computed buy-amount
what-if from the latest stored rate — the frontend only displays it; the fx ledger
still records the user's actual amounts. Corrections (edit/delete) follow the ledger
discipline.
"""

import sqlite3
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
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
from portfolio_dash.portfolio.cash import (
    CashLine,
    account_statement,
    cash_balances,
    pool_lines,
    running_min,
)
from portfolio_dash.pricing.store import get_fx
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.fx import convert
from portfolio_dash.shared.models.assets import Account
from portfolio_dash.shared.wire import decimal_str

router = APIRouter()

_ZERO = Decimal("0")
# REBATE (退款／折讓): a deposit-like CREDIT booked by the rebate inbox confirm (FE-D1). It is
# an actual cash refund of record — NOT the forecast estimate — and never touches cost/P&L.
_KINDS = {"DEPOSIT", "WITHDRAW", "OPENING", "REBATE"}


def _accounts(conn: sqlite3.Connection) -> dict[str, Account]:
    return {a.account_id: a for a in list_accounts(conn)}


def _allowed_ccys(account: Account) -> set[Currency]:
    """The currencies a pool may legitimately hold: settlement + funding (audit C2)."""
    return {account.settlement_ccy, account.funding_ccy}


def _balances(
    conn: sqlite3.Connection,
    *,
    movements: list[StoredCashMovement] | None = None,
) -> dict[tuple[str, Currency], Decimal]:
    """Pool balances; ``movements`` overrides the stored movement ledger (would-be /
    excluding-a-row reads for the withdraw guard, FU-D43a)."""
    return cash_balances(
        movements if movements is not None else list_cash_movements(conn),
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


def _fx_insufficient_response(
    acct: Account, ccy: Currency, available: Decimal, requested: Decimal
) -> JSONResponse:
    """FU-D34 (需求五) HARD block:换匯不可透支 — no ack override, no financing."""
    return JSONResponse(status_code=422, content=error_body(
        "fx_insufficient_balance",
        f"換出金額 {decimal_str(requested)} {ccy.value} 超過 {acct.name} 的 "
        f"{ccy.value} 可用餘額 {decimal_str(available)} — 換匯不可透支（不提供融資）",
        field="from_amt"))


def _withdraw_insufficient_response(
    acct: Account, ccy: Currency, available: Decimal, requested: Decimal
) -> JSONResponse:
    """FU-D43a HARD block: 出金不可透支 — no ack override (mirrors the FX-center guard)."""
    return JSONResponse(status_code=422, content=error_body(
        "withdraw_insufficient_balance",
        f"出金金額 {decimal_str(requested)} {ccy.value} 超過 {acct.name} 的 "
        f"{ccy.value} 賬戶現金 {decimal_str(available)} — 出金不可透支"
        "（請先補登入金或換匯）",
        field="amount"))


def _withdraw_guard(
    conn: sqlite3.Connection,
    body: "MovementBody",
    acct: Account,
    *,
    exclude_id: int | None = None,
) -> JSONResponse | None:
    """FU-D43a: a withdrawal may NEVER overdraft its pool — HARD 422, no ack override.

    Primary check: the amount must be covered by the pool's CURRENT balance — the same
    ``cash_balances`` figure the 賬戶現金 line displays, so the frontend hint and the
    backend authority never disagree; an exact-balance withdrawal (== available) passes.
    For a PUT edit, ``exclude_id`` strips the edited row's own prior effect from the
    balance first, so raising a withdraw within the headroom its old amount already
    consumed is not falsely blocked.

    Date-aware check (audit C3, hardened for withdrawals): a withdraw that INTRODUCES or
    DEEPENS a below-zero dip in the pool's running timeline (e.g. back-dated before its
    funding) is blocked too — with the ack override removed, a missed deposit/conversion
    must be recorded first. A PRE-EXISTING dip this withdraw does not worsen never blocks
    it (scoped like the ledger-correction replay guard, audit H3).
    """
    without = [m for m in list_cash_movements(conn) if m.id != exclude_id]
    available = _balances(conn, movements=without).get(
        (body.account_id, body.ccy), _ZERO)
    if body.amount > available:
        return _withdraw_insufficient_response(acct, body.ccy, available, body.amount)
    would_be = [*without, _synthetic_movement(body, "WITHDRAW")]
    low_after = _pool_min(conn, body.account_id, body.ccy, movements=would_be)
    low_before = _pool_min(conn, body.account_id, body.ccy, movements=without)
    if low_after < min(low_before, _ZERO):
        return JSONResponse(status_code=422, content=error_body(
            "withdraw_insufficient_balance",
            f"此筆出金會使 {acct.name} 的 {body.ccy.value} 現金於某時點降至 "
            f"{decimal_str(low_after)}（出金日早於資金到位）— 出金不可透支，"
            "請先補登入金或換匯",
            field="amount"))
    return None


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


def _stmt_row_wire(ccy: Currency, ln: CashLine, bal: Decimal) -> dict[str, Any]:
    """One statement row: the existing keys (date/kind/ref/delta/balance) + the per-row
    ``ccy`` (needed by the combined view) + the OPTIONAL structured detail keys (null when
    the field does not apply to the kind). Every Decimal is a wire STRING."""
    def _d(value: Decimal | None) -> str | None:
        return decimal_str(value) if value is not None else None

    return {
        "date": ln.date.isoformat(),
        "ccy": ccy.value,
        "kind": ln.kind,
        "ref": ln.ref,
        "delta": decimal_str(ln.delta),
        "balance": decimal_str(bal),
        "symbol": ln.symbol,
        "name": ln.name,
        "qty": _d(ln.qty),
        "price": _d(ln.price),
        "fee": _d(ln.fee),
        "tax": _d(ln.tax),
        "fx_rate": _d(ln.fx_rate),
        "counter_ccy": ln.counter_ccy,
        "counter_amount": _d(ln.counter_amount),
    }


@router.get("/cash/statement")
def cash_statement(
    account: str = Query(...),
    ccy: Currency | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    """Merged, date-ordered flow timeline with a server-computed running balance (audit C5).

    ``ccy`` given → one (account, ccy) pool (``ccy`` echoed; ``current_balance`` set).
    ``ccy`` absent → the ACCOUNT-LEVEL all-currency view: every pool's rows merged
    newest-first, each row carrying its own ``ccy`` and its per-(account, ccy) running
    balance (balances are NEVER blended across currencies); envelope ``ccy`` is null and a
    per-ccy ``balances`` list is returned. Newest-first, paged; Decimal strings."""
    accounts = _accounts(conn)
    acct = accounts.get(account)
    if acct is None:
        return JSONResponse(status_code=404, content=error_body(
            "not_found", f"帳戶 {account} 不存在", field="account"))
    statements = account_statement(
        account, list_cash_movements(conn), list_fx_conversions(conn),
        list_transactions(conn), list_dividends(conn),
        {i.symbol: i for i in list_instruments(conn)}, ccy=ccy,
    )
    # Per-ccy current balances (last running balance in each pool; 0 for an empty pool).
    balances = [
        {"ccy": pool_ccy.value, "balance": decimal_str(stmt[-1][1] if stmt else _ZERO)}
        for pool_ccy, stmt in statements
    ]
    # Flatten every pool's rows, then sort newest-first for display. The key is the REVERSE
    # of the chronological order (running_statement's `_ordered` = date asc, credits-before-
    # debits), so same-day rows show end-of-day balance on top; each row keeps its OWN pool
    # balance and currencies are never interleaved into one running total.
    flat: list[tuple[Currency, CashLine, Decimal]] = [
        (pool_ccy, ln, bal) for pool_ccy, stmt in statements for ln, bal in stmt
    ]
    flat.sort(key=lambda item: (item[1].date, item[1].delta < _ZERO), reverse=True)
    page = flat[offset:offset + limit]
    single = ccy is not None
    current_balance = statements[0][1][-1][1] if single and statements[0][1] else _ZERO
    return {
        "account_id": account,
        "account": acct.name,
        "ccy": ccy.value if ccy is not None else None,
        "current_balance": decimal_str(current_balance) if single else None,
        "balances": balances,
        "rows": [_stmt_row_wire(c, ln, bal) for c, ln, bal in page],
        "total_count": len(flat),
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
            "validation_error", f"未知類型 {body.kind}（deposit / withdraw / opening / rebate）",
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
    if kind == "WITHDRAW":
        # FU-D43a: HARD block — ``ack_negative`` no longer bypasses a withdrawal that the
        # pool cannot cover (deposit/opening/rebate credits need no balance guard on POST).
        acct = _accounts(conn)[body.account_id]  # exists (checked in _movement_guard)
        blocked = _withdraw_guard(conn, body, acct)
        if blocked is not None:
            return blocked
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
    # FE-D1 (R8.1): a booked 折讓款 (REBATE) credit is the structural suppression anchor for its
    # trade month — api/rebates._confirmed_months maps it back by movement DATE (and, as a
    # secondary key, the note tag). Editing the note is safe (the date key still suppresses, and
    # a test relies on that), but changing the KIND (drops it from the confirmed set) or the DATE
    # (re-anchors it to a different month) would let the original month re-surface as pending and
    # be confirmed — and credited — a second time. Block those two; amount stays correctable. To
    # reverse a rebate, delete the row instead.
    if existing.kind.upper() == "REBATE" and (
        body.kind.strip().upper() != "REBATE"
        or body.date != existing.date
    ):
        return JSONResponse(status_code=400, content=error_body(
            "validation_error",
            "折讓款的類型與日期已鎖定以避免重複入帳(可修正金額或備註;如需撤銷請刪除此筆)",
            field="kind"))
    bad = _movement_guard(conn, body)
    if bad is not None:
        return bad
    kind = body.kind.strip().upper()
    if kind == "WITHDRAW":
        # FU-D43a: the edited withdraw is HARD-guarded on its target pool, with the
        # balance computed EXCLUDING this row's own prior effect (self-exclusion) —
        # no ack override for what the withdraw itself consumes.
        acct = _accounts(conn)[body.account_id]  # exists (checked in _movement_guard)
        blocked = _withdraw_guard(conn, body, acct, exclude_id=move_id)
        if blocked is not None:
            return blocked
    if not body.ack_negative:
        edited = _synthetic_movement(body, kind)
        would_be = [edited if m.id == move_id else m for m in list_cash_movements(conn)]
        without = [m for m in list_cash_movements(conn) if m.id != move_id]
        # Any pool the edit touches (old or new account/ccy) must stay non-negative.
        for account_id, ccy in {
            (existing.account_id, existing.ccy), (body.account_id, body.ccy)
        }:
            if kind == "WITHDRAW" and (account_id, ccy) == (body.account_id, body.ccy):
                # Target pool of a withdraw: the NEW withdraw is hard-guarded above and
                # must never resurface as an ack-able warning. What remains ack-able here
                # is only the effect of REMOVING the old row (e.g. a deposit edited into
                # a withdraw stranding later flows) — deposit-side semantics, untouched.
                low = _pool_min(conn, account_id, ccy, movements=without)
            else:
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


@router.post("/cash/fx", status_code=201)
def add_fx(
    body: CashFxBody,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    """FX conversion entry with a HARD balance guard (FU-D34, 需求五) and currency↔account
    coherence (audit C2).

    Writes the SAME fx_conversions ledger row the CSV path writes — one ledger,
    two doors; this door checks the pool first. Unlike movement withdrawals (which keep
    their date-aware ``negative_cash`` guard + ack override), a conversion may NEVER drive
    the from-pool below zero: the sell amount must be ≤ the pool's current balance (the
    ``cash_balances`` figure the 換匯中心 balance line shows). There is NO ack override —
    no financing / overdraft.
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
    # FU-D34 (需求五): the from-pool must cover the sell amount — HARD 422, no ack override.
    # Same cash_balances math the balance line displays (consistency), so frontend hint and
    # backend authority never disagree; exact-balance conversion (== available) still passes.
    available = _balances(conn).get((body.account_id, body.from_ccy), _ZERO)
    if body.from_amt > available:
        return _fx_insufficient_response(acct, body.from_ccy, available, body.from_amt)
    fx_id = insert_fx_conversion(
        conn, account_id=body.account_id, date=body.date, from_ccy=body.from_ccy,
        from_amount=body.from_amt, to_ccy=body.to_ccy, to_amount=body.to_amt)
    return {"id": fx_id}


_RATE_MAX_DP = 6  # FX-rate precision cap (matches the pricing write seam's 6-dp rule)


def _cap_rate(rate: Decimal) -> Decimal:
    """Cap a derived rate at 6 dp (ROUND_HALF_UP) — cap only, never pad.

    Stored direct rates are already ≤ 6 dp (the pricing write-seam cap); only the
    trivial inverse division (1/rate) can grow a 28-digit tail, which is representation
    noise, not information (same posture as ``pricing/store._cap_dp``).
    """
    exp = rate.as_tuple().exponent
    if isinstance(exp, int) and exp < -_RATE_MAX_DP:
        return rate.quantize(Decimal(1).scaleb(-_RATE_MAX_DP), rounding=ROUND_HALF_UP)
    return rate


@router.get("/cash/fx-estimate")
def cash_fx_estimate(
    from_ccy: Currency = Query(...),
    to_ccy: Currency = Query(...),
    amount: Decimal = Query(...),
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    """FU-D43c: SERVER-computed buy-amount what-if for the 換匯中心 form.

    Resolves the LATEST stored rate for ``from_ccy``→``to_ccy`` (direct pair, else the
    trivial inverse — exactly the dashboard RateResolver's semantics) and converts via
    the single shared FX helper, quantized to the buy currency's minor unit. Pure
    display aid: the frontend fills the buy field with the returned STRING and never
    computes; the fx ledger still records the user's ACTUAL entered amounts (the
    implied actual rate stays authoritative). No stored rate → ``available: false``
    with a zh reason (degrade, never guess).
    """
    if amount <= _ZERO:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", "金額必須大於 0", field="amount"))
    if from_ccy is to_ccy:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", "換出與換入幣別不可相同", field="to_ccy"))
    read = get_fx(conn, from_ccy, to_ccy, now=now)
    if read is not None:
        rate, as_of = read.rate, read.as_of
    else:
        inv = get_fx(conn, to_ccy, from_ccy, now=now)
        if inv is None or inv.rate == _ZERO:
            return {
                "available": False,
                "reason": f"尚無 {from_ccy.value}/{to_ccy.value} 匯率資料，無法試算",
            }
        rate, as_of = _cap_rate(Decimal("1") / inv.rate), inv.as_of
    estimate = convert(amount, rate, to_currency=to_ccy)
    return {
        "available": True,
        "estimate": decimal_str(estimate),
        "rate": decimal_str(rate),
        "rate_as_of": as_of.isoformat(),
    }
