"""Four ledgers: reads (spec 11) + explicit row corrections (edit/delete, 2026-07-02).

Reads are thin over store.list_*. Corrections stay within the "append-only in
spirit" rule: they are EXPLICIT user actions via PUT/DELETE (never silent
mutation), validated by replaying the WOULD-BE ledger through build_book before
anything is written — an edit/delete that would strand a later sell (oversell)
is refused with 422 unless the user explicitly acks it (mirroring manual entry;
the dashboard degrades an acked oversold book to a flagged 賣超 holding).

Side/DividendType serialize lowercase (SR #1); Currency stays uppercase. The `total`
sign + `implied_rate` are presentation-level derived fields over stored ledger values.
"""

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from portfolio_dash.api.deps import get_conn
from portfolio_dash.api.errors import error_body
from portfolio_dash.api.wire import parse_side
from portfolio_dash.data_ingestion.store import (
    StoredDividend,
    StoredOpening,
    StoredTransaction,
    delete_dividend,
    delete_fx_conversion,
    delete_opening,
    delete_transaction,
    get_dividend,
    get_fx_conversion,
    get_instrument,
    get_opening,
    get_transaction,
    list_accounts,
    list_dividends,
    list_fx_conversions,
    list_instruments,
    list_opening,
    list_transactions,
    update_dividend,
    update_fx_conversion,
    update_transaction,
    upsert_opening,
)
from portfolio_dash.portfolio.cost_basis import OversellError, build_book
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.enums import DividendType
from portfolio_dash.shared.models.ledger import (
    Dividend,
    OpeningInventory,
    Transaction,
)
from portfolio_dash.shared.wire import decimal_str

router = APIRouter()


def _names(conn: sqlite3.Connection) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    accts = {a.account_id: a.name for a in list_accounts(conn)}
    insts = list_instruments(conn)
    names = {i.symbol: i.name for i in insts}
    ccys = {i.symbol: i.quote_ccy.value for i in insts}
    return accts, names, ccys


def _page(rows: list[dict[str, Any]], limit: int, offset: int) -> dict[str, Any]:
    desc = list(reversed(rows))  # rows arrive ASC; present desc by recency
    return {"rows": desc[offset:offset + limit], "total_count": len(desc)}


def _check_dates(frm: str | None, to: str | None) -> JSONResponse | None:
    if frm and to and frm > to:
        return JSONResponse(status_code=400,
                            content=error_body("validation_error", "日期區間無效", field="from"))
    return None


def _in_range(d: date, frm: str | None, to: str | None) -> bool:
    if frm and d.isoformat() < frm:
        return False
    if to and d.isoformat() > to:
        return False
    return True


@router.get("/ledgers/transactions")
def transactions(
    account_id: str | None = None, symbol: str | None = None,
    frm: str | None = Query(None, alias="from"), to: str | None = None,
    limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    bad = _check_dates(frm, to)
    if bad is not None:
        return bad
    accts, names, ccys = _names(conn)
    out: list[dict[str, Any]] = []
    for t in list_transactions(conn, account_id=account_id, symbol=symbol):
        if not _in_range(t.trade_date, frm, to):
            continue
        gross = t.quantity * t.price
        total = -(gross + t.fees + t.tax) if t.side.value == "BUY" else (gross - t.fees - t.tax)
        out.append({
            "id": t.id, "date": t.trade_date.isoformat(), "account_id": t.account_id,
            "account": accts.get(t.account_id, t.account_id), "symbol": t.symbol,
            "name": names.get(t.symbol, ""), "side": t.side.value.lower(),
            "shares": decimal_str(t.quantity), "price": decimal_str(t.price),
            "fee": decimal_str(t.fees), "tax": decimal_str(t.tax),
            "total": decimal_str(total), "ccy": ccys.get(t.symbol, ""),
            "fee_snapshot": (t.fee_rule_snapshot or None), "note": t.note,
        })
    return _page(out, limit, offset)


@router.get("/ledgers/dividends")
def dividends(
    account_id: str | None = None, symbol: str | None = None,
    frm: str | None = Query(None, alias="from"), to: str | None = None,
    limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    bad = _check_dates(frm, to)
    if bad is not None:
        return bad
    accts, names, ccys = _names(conn)
    out: list[dict[str, Any]] = []
    for d in list_dividends(conn, account_id=account_id, symbol=symbol):
        if not _in_range(d.date, frm, to):
            continue
        out.append({
            "id": d.id, "date": d.date.isoformat(), "account_id": d.account_id,
            "account": accts.get(d.account_id, d.account_id), "symbol": d.symbol,
            "name": names.get(d.symbol, ""), "type": d.type.lower(),
            "gross": decimal_str(d.gross), "withhold": decimal_str(d.withholding),
            "net": decimal_str(d.net),
            "reinvest_shares": (
                decimal_str(d.reinvest_shares) if d.reinvest_shares is not None else None
            ),
            "reinvest_price": (
                decimal_str(d.reinvest_price) if d.reinvest_price is not None else None
            ),
            "ccy": ccys.get(d.symbol, ""),
        })
    return _page(out, limit, offset)


@router.get("/ledgers/fx")
def fx(
    account_id: str | None = None,
    frm: str | None = Query(None, alias="from"), to: str | None = None,
    limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    bad = _check_dates(frm, to)
    if bad is not None:
        return bad
    accts, _names_map, _ccys = _names(conn)
    out: list[dict[str, Any]] = []
    for c in list_fx_conversions(conn, account_id=account_id):
        if not _in_range(c.date, frm, to):
            continue
        out.append({
            "id": c.id, "date": c.date.isoformat(), "account_id": c.account_id,
            "account": accts.get(c.account_id, c.account_id),
            "from_ccy": c.from_ccy.value, "from_amt": decimal_str(c.from_amount),
            "to_ccy": c.to_ccy.value, "to_amt": decimal_str(c.to_amount),
            "implied_rate": decimal_str(c.implied_rate),
        })
    return _page(out, limit, offset)


@router.get("/ledgers/openings")
def openings(
    account_id: str | None = None, symbol: str | None = None,
    limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    accts, names, ccys = _names(conn)
    out: list[dict[str, Any]] = []
    for o in list_opening(conn, account_id=account_id):
        if symbol is not None and o.symbol != symbol:
            continue
        out.append({
            "date": o.build_date.isoformat(), "account_id": o.account_id,
            "account": accts.get(o.account_id, o.account_id), "symbol": o.symbol,
            "name": names.get(o.symbol, ""), "shares": decimal_str(o.shares),
            "avg": decimal_str(o.original_avg_cost),
            "total": decimal_str(o.original_cost_total),
            "ccy": ccys.get(o.symbol, ""),
        })
    paged = _page(out, limit, offset)
    for i, row in enumerate(paged["rows"], start=1):
        row["id"] = i  # openings has no DB id; synthetic 1-based display key
    return paged


# ---------------------------------------------------------------------------
# Row corrections: edit / delete (2026-07-02)
# ---------------------------------------------------------------------------


def _replay_error(
    conn: sqlite3.Connection,
    *,
    txs: list[StoredTransaction] | None = None,
    divs: list[StoredDividend] | None = None,
    opening: list[StoredOpening] | None = None,
) -> str | None:
    """Replay the WOULD-BE ledger through build_book; the oversell message or None.

    Callers pass the mutated list(s); unspecified ledgers load from the store.
    Rows whose symbol is unregistered are excluded (same degradation as the
    dashboard) so one legacy bad row cannot block corrections to healthy rows.
    """
    s_txs = txs if txs is not None else list_transactions(conn)
    s_divs = divs if divs is not None else list_dividends(conn)
    s_open = opening if opening is not None else list_opening(conn)
    instruments = {i.symbol: i for i in list_instruments(conn)}
    t_models = [
        Transaction(account_id=s.account_id, symbol=s.symbol, side=s.side,
                    quantity=s.quantity, price=s.price, fees=s.fees, tax=s.tax,
                    trade_date=s.trade_date)
        for s in s_txs if s.symbol in instruments
    ]
    d_models = [
        Dividend(account_id=s.account_id, symbol=s.symbol, date=s.date,
                 type=DividendType(s.type), gross=s.gross, withholding=s.withholding,
                 net=s.net, reinvest_shares=s.reinvest_shares,
                 reinvest_price=s.reinvest_price)
        for s in s_divs if s.symbol in instruments
    ]
    o_models = [
        OpeningInventory(account_id=s.account_id, symbol=s.symbol, shares=s.shares,
                         original_avg_cost=s.original_avg_cost,
                         original_cost_total=s.original_cost_total,
                         build_date=s.build_date)
        for s in s_open if s.symbol in instruments
    ]
    try:
        build_book(t_models, d_models, o_models, instruments)
    except OversellError as exc:
        return str(exc)
    return None


def _account_exists(conn: sqlite3.Connection, account_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM accounts WHERE account_id=?", (account_id,)
    ).fetchone() is not None


def _mutation_guard(
    conn: sqlite3.Connection, *, account_id: str, symbol: str | None
) -> JSONResponse | None:
    """Shared field checks for row corrections: account known, symbol registered."""
    if not _account_exists(conn, account_id):
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", f"帳戶 {account_id} 不存在", field="account_id"))
    if symbol is not None and get_instrument(conn, symbol) is None:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error",
            f"未註冊標的 {symbol} — 請先至「標的管理」註冊", field="symbol"))
    return None


def _oversell_response(msg: str) -> JSONResponse:
    return JSONResponse(status_code=422, content=error_body(
        "oversell",
        f"此更正將造成賣超（{msg}）— 確認後可強制寫入（儀表板將標示賣超待釐清）"))


class TxEditBody(BaseModel):
    account_id: str
    symbol: str
    side: str
    date: date
    shares: Decimal
    price: Decimal
    fee: Decimal
    tax: Decimal
    note: str | None = None
    ack_oversell: bool = False


@router.put("/ledgers/transactions/{txn_id}")
def edit_transaction(
    txn_id: int,
    body: TxEditBody,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    existing = get_transaction(conn, txn_id)
    if existing is None:
        return JSONResponse(status_code=404,
                            content=error_body("not_found", f"交易 #{txn_id} 不存在"))
    guard = _mutation_guard(conn, account_id=body.account_id, symbol=body.symbol)
    if guard is not None:
        return guard
    if body.shares <= 0 or body.price <= 0:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", "股數與價格必須大於 0", field="shares"))
    if body.fee < 0 or body.tax < 0:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", "費用與稅不可為負", field="fee"))
    edited = existing.model_copy(update={
        "account_id": body.account_id, "symbol": body.symbol,
        "side": parse_side(body.side), "quantity": body.shares, "price": body.price,
        "fees": body.fee, "tax": body.tax, "trade_date": body.date, "note": body.note,
    })
    would_be = [edited if t.id == txn_id else t for t in list_transactions(conn)]
    msg = _replay_error(conn, txs=would_be)
    if msg is not None and not body.ack_oversell:
        return _oversell_response(msg)
    update_transaction(
        conn, txn_id, account_id=body.account_id, symbol=body.symbol,
        side=parse_side(body.side), quantity=body.shares, price=body.price,
        fees=body.fee, tax=body.tax, trade_date=body.date, note=body.note,
    )
    return {"ok": True, "id": txn_id}


@router.delete("/ledgers/transactions/{txn_id}")
def remove_transaction(
    txn_id: int,
    ack_oversell: bool = False,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    if get_transaction(conn, txn_id) is None:
        return JSONResponse(status_code=404,
                            content=error_body("not_found", f"交易 #{txn_id} 不存在"))
    would_be = [t for t in list_transactions(conn) if t.id != txn_id]
    msg = _replay_error(conn, txs=would_be)
    if msg is not None and not ack_oversell:
        return _oversell_response(msg)
    delete_transaction(conn, txn_id)
    return {"ok": True, "id": txn_id}


_DIV_TYPES = {t.value for t in DividendType}


class DivEditBody(BaseModel):
    account_id: str
    symbol: str
    date: date
    type: str
    gross: Decimal
    withhold: Decimal
    net: Decimal
    reinvest_shares: Decimal | None = None
    reinvest_price: Decimal | None = None
    ack_oversell: bool = False


@router.put("/ledgers/dividends/{div_id}")
def edit_dividend(
    div_id: int,
    body: DivEditBody,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    existing = get_dividend(conn, div_id)
    if existing is None:
        return JSONResponse(status_code=404,
                            content=error_body("not_found", f"股利 #{div_id} 不存在"))
    guard = _mutation_guard(conn, account_id=body.account_id, symbol=body.symbol)
    if guard is not None:
        return guard
    div_type = body.type.strip().upper()
    if div_type not in _DIV_TYPES:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", f"未知股利類型 {body.type}", field="type"))
    if body.gross < 0 or body.withhold < 0 or body.net < 0:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", "股利金額不可為負", field="gross"))
    edited = existing.model_copy(update={
        "account_id": body.account_id, "symbol": body.symbol, "date": body.date,
        "type": div_type, "gross": body.gross, "withholding": body.withhold,
        "net": body.net, "reinvest_shares": body.reinvest_shares,
        "reinvest_price": body.reinvest_price,
    })
    would_be = [edited if d.id == div_id else d for d in list_dividends(conn)]
    msg = _replay_error(conn, divs=would_be)
    if msg is not None and not body.ack_oversell:
        return _oversell_response(msg)
    update_dividend(
        conn, div_id, account_id=body.account_id, symbol=body.symbol,
        div_date=body.date, div_type=div_type, gross=body.gross,
        withholding=body.withhold, net=body.net,
        reinvest_shares=body.reinvest_shares, reinvest_price=body.reinvest_price,
    )
    return {"ok": True, "id": div_id}


@router.delete("/ledgers/dividends/{div_id}")
def remove_dividend(
    div_id: int,
    ack_oversell: bool = False,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    if get_dividend(conn, div_id) is None:
        return JSONResponse(status_code=404,
                            content=error_body("not_found", f"股利 #{div_id} 不存在"))
    would_be = [d for d in list_dividends(conn) if d.id != div_id]
    msg = _replay_error(conn, divs=would_be)
    if msg is not None and not ack_oversell:
        return _oversell_response(msg)
    delete_dividend(conn, div_id)
    return {"ok": True, "id": div_id}


class FxEditBody(BaseModel):
    account_id: str
    date: date
    from_ccy: Currency
    from_amt: Decimal
    to_ccy: Currency
    to_amt: Decimal


@router.put("/ledgers/fx/{fx_id}")
def edit_fx(
    fx_id: int,
    body: FxEditBody,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    if get_fx_conversion(conn, fx_id) is None:
        return JSONResponse(status_code=404,
                            content=error_body("not_found", f"換匯 #{fx_id} 不存在"))
    guard = _mutation_guard(conn, account_id=body.account_id, symbol=None)
    if guard is not None:
        return guard
    if body.from_amt <= 0 or body.to_amt <= 0:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", "換匯金額必須大於 0", field="from_amt"))
    if body.from_ccy is body.to_ccy:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", "換出與換入幣別不可相同", field="to_ccy"))
    update_fx_conversion(
        conn, fx_id, account_id=body.account_id, date=body.date,
        from_ccy=body.from_ccy, from_amount=body.from_amt,
        to_ccy=body.to_ccy, to_amount=body.to_amt,
    )
    return {"ok": True, "id": fx_id}


@router.delete("/ledgers/fx/{fx_id}")
def remove_fx(
    fx_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    if get_fx_conversion(conn, fx_id) is None:
        return JSONResponse(status_code=404,
                            content=error_body("not_found", f"換匯 #{fx_id} 不存在"))
    delete_fx_conversion(conn, fx_id)
    return {"ok": True, "id": fx_id}


class OpeningEditBody(BaseModel):
    shares: Decimal
    avg: Decimal
    date: date
    ack_oversell: bool = False


@router.put("/ledgers/openings/{account_id}/{symbol}")
def edit_opening(
    account_id: str,
    symbol: str,
    body: OpeningEditBody,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    existing = get_opening(conn, account_id, symbol)
    if existing is None:
        return JSONResponse(status_code=404, content=error_body(
            "not_found", f"期初 {account_id}/{symbol} 不存在"))
    if body.shares <= 0 or body.avg < 0:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", "股數必須大於 0、均價不可為負", field="shares"))
    total = body.avg * body.shares
    edited = existing.model_copy(update={
        "shares": body.shares, "original_avg_cost": body.avg,
        "original_cost_total": total, "build_date": body.date,
    })
    would_be = [edited if (o.account_id == account_id and o.symbol == symbol) else o
                for o in list_opening(conn)]
    msg = _replay_error(conn, opening=would_be)
    if msg is not None and not body.ack_oversell:
        return _oversell_response(msg)
    upsert_opening(
        conn, account_id=account_id, symbol=symbol, shares=body.shares,
        original_avg_cost=body.avg, original_cost_total=total, build_date=body.date,
    )
    return {"ok": True}


@router.delete("/ledgers/openings/{account_id}/{symbol}")
def remove_opening(
    account_id: str,
    symbol: str,
    ack_oversell: bool = False,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    if get_opening(conn, account_id, symbol) is None:
        return JSONResponse(status_code=404, content=error_body(
            "not_found", f"期初 {account_id}/{symbol} 不存在"))
    would_be = [o for o in list_opening(conn)
                if not (o.account_id == account_id and o.symbol == symbol)]
    msg = _replay_error(conn, opening=would_be)
    if msg is not None and not ack_oversell:
        return _oversell_response(msg)
    delete_opening(conn, account_id, symbol)
    return {"ok": True}
