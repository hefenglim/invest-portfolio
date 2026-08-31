"""Top-bar actions (spec 08 §8.2-8.3): refresh quotes, recompute."""

import sqlite3
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from portfolio_dash.api.deps import get_conn, get_now
from portfolio_dash.api.errors import error_body
from portfolio_dash.data_ingestion.store import load_ledger_bundle
from portfolio_dash.portfolio.cost_basis import (
    OversellError,
    UnbookableLedgerError,
    build_book,
)
from portfolio_dash.scheduler.jobs import backfill_history_all, run_job

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


class BackfillBody(BaseModel):
    days: int | None = None  # None = smart windows (12mo / first-acquisition / ledger)


@router.post("/actions/backfill-history", status_code=200)
def backfill_history(
    body: BackfillBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    """Backfill price + FX history for ALL instruments (manual, idempotent).

    Default (days omitted): SMART windows — the config-driven floor
    (``history_backfill_days``, 5y default since owner 2026-07-08), extended per
    symbol to its first acquisition date when older, and FX pairs from the earliest
    ledger flow date. Explicit ``days`` = uniform window, clamped to [1, 3650].
    """
    days = max(1, min(body.days, 3650)) if body.days is not None else None
    detail = backfill_history_all(conn, days=days, now=now)
    return {"days": days, "detail": detail}


@router.post("/actions/recompute", status_code=200)
def recompute(
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    """Re-validate the ledgers by replaying them (read-only; append-only honored)."""
    bundle = load_ledger_bundle(conn)
    # Unregistered symbols make the ledger un-bookable (no quote ccy) — report them
    # explicitly instead of letting build_book KeyError into a 500 (2026-07-02).
    unregistered = bundle.unregistered_symbols
    if unregistered:
        return JSONResponse(status_code=422, content=error_body(
            "unregistered_symbol",
            f"帳本含未註冊標的：{', '.join(unregistered)} — 請先至「標的管理」註冊後再重算"))
    try:
        build_book(bundle)
    except OversellError as exc:
        # The wording, the code and the ``issues`` shape are ``api/routers/export.py``'s and
        # ``strategy/whatif.py``'s, on purpose. This arm answered ``error_body("oversell",
        # str(exc))``, and ``str(exc)`` is written for a developer by design:
        # ``sell 9999 > held 10 for AAPL``. ``message`` is rendered VERBATIM as a red toast, so
        # that sentence went straight to the owner — on the one door whose whole job is to say
        # the ledger does not replay. The English detail still travels, in ``issues[].text``.
        #
        # ``"oversold_position"`` also unifies the code: 試算, the tax package and
        # ``api/errors.py``'s handler all use it, so a frontend branch written for them fires
        # here too. ⚠ ``"oversell"`` stays correct for a DIFFERENT door — ``ledgers.py``'s
        # mutation guard uses it as a retryable-with-ack signal that ``web/inbox.js`` and
        # ``web/ledger.js`` branch on (「賣超確認」 → re-send with ``ack_oversell``). 重算 has no
        # ack to offer, so it was never that signal.
        return JSONResponse(status_code=422, content=error_body(
            "oversold_position",
            f"帳本中有賣超部位待釐清（{exc.account_id}／{exc.symbol}，"
            f"{exc.trade_date.isoformat()}）— 無法重算，請先修正該筆交易",
            issues=[{
                "sev": "error",
                "code": "oversold_position",
                "text": str(exc),
                "field": None,
                "account_id": exc.account_id,
                "symbol": exc.symbol,
                "trade_date": exc.trade_date.isoformat(),
            }]))
    except UnbookableLedgerError as exc:
        # never-500 at EVERY build_book call site: the strict replay refuses an event it
        # cannot book honestly (e.g. a dividend inside an open-short window), and 重算
        # must say so rather than return an internal error.
        return JSONResponse(status_code=422, content=error_body(
            "unbookable_ledger", str(exc)))
    return {"as_of": now.isoformat(), "rebuilt": True}
