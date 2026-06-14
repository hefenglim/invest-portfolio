"""Four append-only ledgers, read-only (spec 11). Thin over store.list_*; no writes.

Side/DividendType serialize lowercase (SR #1); Currency stays uppercase. The `total`
sign + `implied_rate` are presentation-level derived fields over stored ledger values.
"""

import sqlite3
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from portfolio_dash.api.deps import get_conn
from portfolio_dash.api.errors import error_body
from portfolio_dash.data_ingestion.store import (
    list_accounts,
    list_dividends,
    list_fx_conversions,
    list_instruments,
    list_opening,
    list_transactions,
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
