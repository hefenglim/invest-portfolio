"""Instruments registry API (spec 10): list (+ probe + register/update in later tasks).

Thin over data_ingestion.store + pricing.store reads. Computes nothing of record.
"""

import sqlite3
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from portfolio_dash.api.deps import get_conn, get_now
from portfolio_dash.api.errors import error_body
from portfolio_dash.data_ingestion.holdings import current_shares
from portfolio_dash.data_ingestion.register import register_instrument
from portfolio_dash.data_ingestion.store import (
    get_instrument,
    list_accounts,
    list_instruments,
    upsert_instrument,
)
from portfolio_dash.pricing.board import probe_tw_board
from portfolio_dash.pricing.store import get_latest_price, get_price_history
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.wire import decimal_str

router = APIRouter()


def _held(conn: sqlite3.Connection, account_ids: list[str], symbol: str) -> bool:
    return any(current_shares(conn, aid, symbol) > 0 for aid in account_ids)


def _board_wire(conn: sqlite3.Connection, inst: Instrument) -> str | None:
    """TW + board_status='unresolved' -> null; otherwise the stored board string."""
    row = conn.execute("SELECT board_status FROM instruments WHERE symbol=?",
                       (inst.symbol,)).fetchone()
    status = row["board_status"] if row is not None else "resolved"
    if inst.market.value == "TW" and status == "unresolved":
        return None
    return inst.board


def _element(conn: sqlite3.Connection, inst: Instrument, account_ids: list[str],
             now: datetime) -> dict[str, Any]:
    pr = get_latest_price(conn, inst.symbol, now=now)
    last = decimal_str(pr.value) if pr is not None else None
    chg_pct: str | None = None
    if pr is not None:
        hist = get_price_history(conn, inst.symbol, pr.as_of.replace(day=1), pr.as_of)
        if len(hist) >= 2 and hist[-2].value != 0:
            chg_pct = decimal_str((hist[-1].value - hist[-2].value) / hist[-2].value)
    return {
        "symbol": inst.symbol, "name": inst.name, "market": inst.market.value,
        "board": _board_wire(conn, inst), "sector": inst.sector,
        "ccy": inst.quote_ccy.value, "held": _held(conn, account_ids, inst.symbol),
        "last": last, "chg_pct": chg_pct,
        "target_low": decimal_str(inst.target_low) if inst.target_low is not None else None,
    }


@router.get("/instruments")
def list_all(
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    account_ids = [a.account_id for a in list_accounts(conn)]
    items = [_element(conn, inst, account_ids, now) for inst in list_instruments(conn)]
    return {"as_of": now.isoformat(), "list": items}


_BOARD_LABEL = {"TWSE": "TWSE 上市", "TPEx": "TPEx 上櫃"}


class ProbeBody(BaseModel):
    symbol: str


@router.post("/instruments/probe")
def probe(body: ProbeBody) -> dict[str, Any]:
    """Registration step 1: guess the TW board for a symbol (user confirms next)."""
    sym = body.symbol.strip()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol 不可為空")
    board = probe_tw_board(sym)
    return {"symbol": sym, "name": None, "board": board,
            "board_label": _BOARD_LABEL.get(board or "", "未解析")}


_DEFAULT_CCY = {Market.TW: Currency.TWD, Market.US: Currency.USD, Market.MY: Currency.MYR}
_TW_BOARDS = {"TWSE", "TPEx"}


class RegisterBody(BaseModel):
    symbol: str
    market: Market
    name: str = ""
    sector: str = ""
    board: str | None = None
    quote_ccy: Currency | None = None
    target_low: Decimal | None = None
    is_etf: bool = False


class UpdateBody(BaseModel):
    name: str | None = None
    sector: str | None = None
    board: str | None = None
    target_low: Decimal | None = None
    is_etf: bool | None = None


@router.post("/instruments", status_code=201)
def register(
    body: RegisterBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    if get_instrument(conn, body.symbol) is not None:
        return JSONResponse(status_code=409,
                            content=error_body("duplicate_symbol", f"{body.symbol} 已註冊"))
    if body.market in (Market.US, Market.MY) and (body.board or "") in _TW_BOARDS:
        return JSONResponse(status_code=400,
                            content=error_body("validation_error", "US/MY 不可帶台股板別",
                                               field="board"))
    ccy = body.quote_ccy or _DEFAULT_CCY[body.market]
    inst = Instrument(symbol=body.symbol, market=body.market, quote_ccy=ccy,
                      sector=body.sector, name=body.name, board=body.board or "",
                      target_low=body.target_low, is_etf=body.is_etf)
    register_instrument(conn, inst, prober=probe_tw_board, confirm=True)
    saved = get_instrument(conn, body.symbol)
    assert saved is not None
    account_ids = [a.account_id for a in list_accounts(conn)]
    return _element(conn, saved, account_ids, now)


@router.put("/instruments/{symbol}")
def update(
    symbol: str,
    body: UpdateBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    existing = get_instrument(conn, symbol)
    if existing is None:
        return JSONResponse(status_code=404,
                            content=error_body("not_found", f"{symbol} 不存在"))
    if existing.market in (Market.US, Market.MY) and (body.board or "") in _TW_BOARDS:
        return JSONResponse(status_code=400,
                            content=error_body("validation_error", "US/MY 不可帶台股板別",
                                               field="board"))
    fields = body.model_dump(exclude_none=True)
    updated = existing.model_copy(update=fields)
    upsert_instrument(conn, updated)
    saved = get_instrument(conn, symbol)
    assert saved is not None
    account_ids = [a.account_id for a in list_accounts(conn)]
    return _element(conn, saved, account_ids, now)
