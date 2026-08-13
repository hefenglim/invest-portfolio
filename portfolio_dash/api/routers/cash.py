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
from collections.abc import Sequence
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
from portfolio_dash.data_ingestion.validate import (
    CashMovementInput,
    CashPool,
    CashPoolFn,
    Issue,
    cash_movement_kind,
    resolve_acq_home_amount,
    validate_cash_movement,
)
from portfolio_dash.portfolio.cash import (
    CashLine,
    account_statement,
    cash_balances,
    pool_lines,
    running_min,
)
from portfolio_dash.pricing.store import get_fx, get_fx_on
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.fx import convert
from portfolio_dash.shared.models.assets import Account
from portfolio_dash.shared.wire import decimal_str

router = APIRouter()

_ZERO = Decimal("0")
_ONE = Decimal("1")


def _accounts(conn: sqlite3.Connection) -> dict[str, Account]:
    return {a.account_id: a for a in list_accounts(conn)}


def _allowed_ccys(account: Account) -> set[Currency]:
    """The currencies a pool may legitimately hold: settlement + funding (audit C2).

    Now the FX-leg half only: the movement half moved down to
    :func:`~data_ingestion.validate.validate_cash_movement`, which cannot import this module.
    The set is restated there, and the two are cross-referenced rather than shared because
    the only place both layers may import from is ``shared/`` — a two-element set literal is
    not worth a new home there, but a reader who changes one must find the other.
    """
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


def _synthetic(movement: CashMovementInput) -> StoredCashMovement:
    """A would-be movement as a stored row, so the pool arithmetic sees ONE row type."""
    return StoredCashMovement(
        id=0, account_id=movement.account_id, date=movement.date, kind=movement.kind,
        ccy=movement.ccy, amount=movement.amount, note=movement.note)


def cash_pool_fn(conn: sqlite3.Connection) -> CashPoolFn:
    """Bind ``portfolio/cash.py``'s pool arithmetic for the shared movement guard (D17 shape).

    ``data_ingestion`` may not import ``portfolio`` (``architecture.md`` — the arrow runs the
    other way, so the reverse would close a package-level cycle), so
    :func:`~data_ingestion.validate.validate_cash_movement` takes the arithmetic as a callable
    and THIS layer, which sits above both, is where the two meet. Exactly the resolution D17
    gives ``pricing/``'s split factor via ``scheduler/jobs.py::split_factor_fn``.

    **Bound ONCE per request/import, never per row.** The ledger reads happen here; the
    returned closure only re-runs the pure arithmetic over that in-memory snapshot. A 500-row
    cash CSV would otherwise issue 2,500 SELECTs to answer one question per row (trap #21).
    That also makes the manual door cheaper than the three separate loads it used to do.

    **The snapshot is taken at bind time, and that is safe by construction.** Every caller
    validates before it writes, and a preview writes nothing, so no caller can observe rows it
    has not committed. Would-be rows arrive through ``include`` instead — which is what lets a
    batch's own funding row count toward a later withdrawal in the same file.

    The two figures are computed by the SAME two expressions the manual door used before the
    extraction (``cash_balances`` for the balance, ``running_min(pool_lines(...))`` for the
    dip), so the guard's verdict is unchanged.
    """
    movements = list_cash_movements(conn)
    fx = list_fx_conversions(conn)
    txns = list_transactions(conn)
    divs = list_dividends(conn)
    insts = {i.symbol: i for i in list_instruments(conn)}

    def probe(
        account_id: str,
        ccy: Currency,
        *,
        include: Sequence[CashMovementInput] = (),
        exclude_id: int | None = None,
    ) -> CashPool:
        rows: list[StoredCashMovement] = [m for m in movements if m.id != exclude_id]
        rows.extend(_synthetic(m) for m in include)
        return CashPool(
            balance=cash_balances(rows, fx, txns, divs, insts).get(
                (account_id, ccy), _ZERO),
            low=running_min(
                pool_lines(account_id, ccy, rows, fx, txns, divs, insts)),
        )

    return probe


# The wire ``field`` for each hard issue :func:`validate_cash_movement` can raise. It lives
# HERE, not on the Issue, because a form-field name is presentation: the same finding has no
# field at all in the CSV preview, which shows a row, not an input box.
_MOVEMENT_ISSUE_FIELD: dict[str, str] = {
    "unknown_movement_kind": "kind",
    "non_positive_amount": "amount",
    "unknown_account": "account_id",
    "ccy_not_allowed": "ccy",
    "acq_cost_ambiguous": "acq_home_amount",
    "acq_cost_home_ccy": "acq_home_amount",
    "acq_cost_on_withdraw": "acq_home_amount",
    "acq_cost_not_an_acquisition": "acq_home_amount",
    "acq_cost_not_positive": "acq_home_amount",
    "acq_rate_not_positive": "acq_rate",
    "withdraw_insufficient_balance": "amount",
}


def _movement_error(issues: Sequence[Issue]) -> JSONResponse | None:
    """The first HARD issue as this door's error envelope, or ``None`` when clean.

    400 ``validation_error`` for the structural rejections; 422
    ``withdraw_insufficient_balance`` for the overdraft guard (FU-D43a — a distinct code
    because the frontend must NOT offer an ack for it).

    Soft issues are filtered out rather than raised: this door has no preview seam to show an
    advisory on, so returning one would turn a hint into a rejection. The extracted validator
    emits none today; the filter is what keeps that true the day it does.
    """
    hard = next((i for i in issues if not i.needs_confirm), None)
    if hard is None:
        return None
    field = _MOVEMENT_ISSUE_FIELD.get(hard.kind)
    if hard.kind == "withdraw_insufficient_balance":
        return JSONResponse(status_code=422, content=error_body(
            hard.kind, hard.message, field=field))
    return JSONResponse(status_code=400, content=error_body(
        "validation_error", hard.message, field=field))


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
                    # Acquisition cost of a foreign credit (spec F1): the AMOUNT is the
                    # stored authority; the rate is derived HERE so the frontend never
                    # divides two Decimal strings to show it.
                    "acq_home_amount": (None if m.acq_home_amount is None
                                        else decimal_str(m.acq_home_amount)),
                    "acq_home_ccy": (None if m.acq_home_amount is None
                                     else accounts[m.account_id].funding_ccy.value
                                     if m.account_id in accounts else None),
                    "acq_rate": (None if m.acq_home_amount is None or m.amount == _ZERO
                                 else decimal_str(m.acq_home_amount / m.amount)),
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
    # Any case; validated against ``CASH_MOVEMENT_KINDS`` (deposit / withdraw / opening /
    # rebate — the comment used to list three and had been stale since REBATE was added,
    # which is why the vocabulary now lives in ONE named constant instead of in prose).
    kind: str
    ccy: Currency
    amount: Decimal
    note: str | None = None
    ack_negative: bool = False
    # Acquisition cost of a FOREIGN-currency credit (spec 2026-07-30 F1). Supply EITHER
    # the home-currency amount OR the rate — the rate is a convenience for the form and is
    # converted here; only the AMOUNT is ever persisted. Omit both when the rate is genuinely
    # unknown: the movement then funds the pool but stays out of the weighted average
    # (never guessed) and is disclosed via ``fx_basis_gap``.
    acq_home_amount: Decimal | None = None
    acq_rate: Decimal | None = None


def _movement_input(body: MovementBody) -> CashMovementInput:
    """The wire body as the shared validator's input (``ack_negative`` is a door concern)."""
    return CashMovementInput(
        account_id=body.account_id, date=body.date, kind=body.kind, ccy=body.ccy,
        amount=body.amount, note=body.note,
        acq_home_amount=body.acq_home_amount, acq_rate=body.acq_rate)


def _synthetic_movement(body: MovementBody, kind: str) -> StoredCashMovement:
    """The wire body as a would-be stored row, with an explicit (already-normalized) kind.

    Shaped by :func:`_synthetic` so there is ONE place that turns a not-yet-written movement
    into a row the pool arithmetic will sign — the sign comes from the kind (via
    ``shared/cash_kinds.py``), and two shaping sites is how one of them ends up passing the
    un-normalized wire spelling.
    """
    return _synthetic(_movement_input(body).model_copy(update={"kind": kind}))


def movement_guard(
    conn: sqlite3.Connection, body: MovementBody, *, exclude_id: int | None = None
) -> JSONResponse | None:
    """Run the shared cash-movement guard for one wire body; ``None`` when it is clean.

    The single seam every manual door goes through — POST, PUT, and the rebate inbox's
    confirm (``api/routers/rebates.py``), which books a REBATE credit and must obey the same
    kind / amount / account / currency-coherence rules. The CSV door calls
    :func:`~data_ingestion.validate.validate_cash_movement` directly because it needs the
    Issues themselves (a preview shows every row, not the first error).

    ``exclude_id`` is the edited row's own id on a PUT (self-exclusion) — see
    :class:`~data_ingestion.validate.CashPoolFn`.
    """
    return _movement_error(validate_cash_movement(
        conn, _movement_input(body), pool=cash_pool_fn(conn),
        exclude_id=exclude_id, accounts=_accounts(conn)))


def _resolved_acq(conn: sqlite3.Connection, inp: CashMovementInput) -> Decimal | None:
    """The home-currency acquisition cost to persist, AFTER validation has passed.

    Calls :func:`resolve_acq_home_amount` a second time purely for its VALUE — the issue it
    can return has already been surfaced by :func:`validate_cash_movement`. The function is
    pure, so the second call is free; the alternative is a validator that returns a value
    beside its issues, which would make it the only one of the four with that shape.
    """
    account = _accounts(conn)[inp.account_id]  # exists (validation passed)
    amount, _issue = resolve_acq_home_amount(inp, funding_ccy=account.funding_ccy)
    return amount


@router.post("/cash/movements", status_code=201)
def add_movement(
    body: MovementBody,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    # ONE guard, shared with the CSV door: structural checks, the acquisition-cost rules,
    # and FU-D43a's HARD withdraw block (``ack_negative`` does NOT bypass it; deposit /
    # opening / rebate credits need no balance guard on the way in).
    inp = _movement_input(body)
    bad = movement_guard(conn, body)
    if bad is not None:
        return bad
    move_id = insert_cash_movement(
        conn, account_id=body.account_id, move_date=body.date,
        kind=cash_movement_kind(body.kind), ccy=body.ccy, amount=body.amount,
        note=body.note, acq_home_amount=_resolved_acq(conn, inp))
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
    # The SAME shared guard the POST door and the CSV door run, plus ``exclude_id``: the
    # edited row's own prior effect is stripped from the pool first (self-exclusion), so
    # raising a withdrawal within the headroom its OLD amount already consumed is not
    # falsely blocked — and no ack overrides what the withdrawal itself consumes (FU-D43a).
    inp = _movement_input(body)
    bad = movement_guard(conn, body, exclude_id=move_id)
    if bad is not None:
        return bad
    kind = cash_movement_kind(body.kind)
    acq = _resolved_acq(conn, inp)
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
        kind=kind, ccy=body.ccy, amount=body.amount, note=body.note,
        acq_home_amount=acq)
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


@router.get("/cash/acq-rate")
def cash_acq_rate(
    account_id: str = Query(...),
    ccy: Currency = Query(...),
    on: date = Query(...),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    """Point-in-time foreign->home rate to PRE-FILL the 取得成本 field (spec 2026-07-30).

    A REFERENCE value only: a broker's actual conversion rate is not the market mid, so the
    form labels it as such and the user may overwrite it. ``available: false`` (with a zh
    reason) whenever nothing is stored on or before that date — the field then stays blank
    and the user types the rate. Never interpolated, never substituted with today's spot;
    prod's fx_rates only reach back to 2026-07-01, so this path is routine, not an edge case.
    """
    acct = _accounts(conn).get(account_id)
    if acct is None:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", f"帳戶 {account_id} 不存在", field="account_id"))
    home = acct.funding_ccy
    if ccy == home:
        return {"available": False, "reason": "本幣入金不需要取得成本",
                "rate": None, "as_of": None, "home_ccy": home.value}
    direct = get_fx_on(conn, ccy, home, on=on)
    if direct is not None:
        rate, as_of = _cap_rate(direct.rate), direct.as_of
    else:
        inv = get_fx_on(conn, home, ccy, on=on)
        if inv is None or inv.rate == _ZERO:
            return {"available": False,
                    "reason": f"查無 {on.isoformat()} 或之前的 {ccy.value}/{home.value} 匯率，"
                              f"請手動輸入取得匯率",
                    "rate": None, "as_of": None, "home_ccy": home.value}
        rate, as_of = _cap_rate(_ONE / inv.rate), inv.as_of
    return {"available": True, "reason": None, "rate": decimal_str(rate),
            "as_of": as_of.isoformat(), "home_ccy": home.value}


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
