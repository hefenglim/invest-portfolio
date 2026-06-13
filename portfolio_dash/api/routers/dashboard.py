"""GET /api/dashboard — serialize build_dashboard output + spark_30d + llm_quota.

Pure read. The router calls the calc core and serializes; it computes nothing.
Add-on fields owned by later specs (alerts -> 03, dividend_projection -> 05) are added
when those specs land; Phase 0 serves the core payload + spark_30d + llm_quota.
"""

import sqlite3
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query

from portfolio_dash.api.deps import get_conn, get_now, get_reporting
from portfolio_dash.api.serialize import to_wire
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.pricing.store import get_price_history
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.llm_config import budget_remaining

router = APIRouter()

_SPARK_DAYS = 30


@router.get("/dashboard")
def dashboard(
    trend_days: int = Query(90, ge=1, le=3650),
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
    reporting: Currency = Depends(get_reporting),
) -> dict[str, Any]:
    data = build_dashboard(conn, now=now, reporting=reporting)
    payload: dict[str, Any] = to_wire(data.model_dump())

    # spark_30d: recent daily closes per held symbol (spec 01 add-on; batch-friendly read).
    end = now.date()
    start = end - timedelta(days=_SPARK_DAYS)
    for row in payload["holdings"]:
        history = get_price_history(conn, row["symbol"], start, end)
        row["spark_30d"] = [str(p.value) for p in history]

    # Single source of truth (Σ top-ups − Σ usage); never None, $0 when nothing funded.
    payload["llm_quota"] = {"remaining_usd": str(budget_remaining(conn))}
    return payload
