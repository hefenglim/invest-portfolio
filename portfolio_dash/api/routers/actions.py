"""Top-bar actions (spec 08 §8.2-8.3): refresh quotes, recompute."""

import sqlite3
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from portfolio_dash.api.deps import get_conn, get_now
from portfolio_dash.api.errors import error_body
from portfolio_dash.data_ingestion.store import (
    list_dividends,
    list_instruments,
    list_opening,
    list_transactions,
)
from portfolio_dash.portfolio.cost_basis import OversellError, build_book
from portfolio_dash.scheduler.jobs import run_job
from portfolio_dash.shared.models.enums import DividendType
from portfolio_dash.shared.models.ledger import Dividend, OpeningInventory, Transaction

router = APIRouter()

_MARKET_JOB = {"TW": "quotes_tw", "US": "quotes_us", "MY": "quotes_my"}


class RefreshBody(BaseModel):
    markets: list[str] | None = None


@router.post("/actions/refresh-quotes", status_code=200)
def refresh_quotes_action(
    body: RefreshBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    markets = body.markets if body.markets else list(_MARKET_JOB)
    unknown = [m for m in markets if m not in _MARKET_JOB]
    if unknown:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", f"未知市場代碼 {unknown[0]}", field="markets"))
    jobs = [_MARKET_JOB[m] for m in markets]
    run_ids = [run_job(conn, job_id, now=now) for job_id in jobs]
    return {"run_ids": run_ids, "jobs": jobs}


@router.post("/actions/recompute", status_code=200)
def recompute(
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    """Re-validate the ledgers by replaying them (read-only; append-only honored)."""
    txs = [
        Transaction(account_id=s.account_id, symbol=s.symbol, side=s.side,
                    quantity=s.quantity, price=s.price, fees=s.fees, tax=s.tax,
                    trade_date=s.trade_date)
        for s in list_transactions(conn)
    ]
    divs = [
        Dividend(account_id=s.account_id, symbol=s.symbol, date=s.date,
                 type=DividendType(s.type), gross=s.gross, withholding=s.withholding,
                 net=s.net, reinvest_shares=s.reinvest_shares,
                 reinvest_price=s.reinvest_price)
        for s in list_dividends(conn)
    ]
    opening = [
        OpeningInventory(account_id=s.account_id, symbol=s.symbol, shares=s.shares,
                         original_avg_cost=s.original_avg_cost,
                         original_cost_total=s.original_cost_total,
                         build_date=s.build_date)
        for s in list_opening(conn)
    ]
    instruments = {i.symbol: i for i in list_instruments(conn)}
    # Unregistered symbols make the ledger un-bookable (no quote ccy) — report them
    # explicitly instead of letting build_book KeyError into a 500 (2026-07-02).
    ledger_syms = ({t.symbol for t in txs} | {d.symbol for d in divs}
                   | {o.symbol for o in opening})
    unregistered = sorted(ledger_syms - instruments.keys())
    if unregistered:
        return JSONResponse(status_code=422, content=error_body(
            "unregistered_symbol",
            f"帳本含未註冊標的：{', '.join(unregistered)} — 請先至「標的管理」註冊後再重算"))
    try:
        build_book(txs, divs, opening, instruments)
    except OversellError as exc:
        return JSONResponse(status_code=422, content=error_body("oversell", str(exc)))
    return {"as_of": now.isoformat(), "rebuilt": True}
