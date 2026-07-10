"""GET /api/signals — per-held-symbol rule-engine signals (P2 batch 2).

Thin router (decision B): it reads prices + calls the pure rule engine via the api seam
(``signals_service``) and serializes. It computes no numbers of record; scores/ratios go
out as Decimal STRINGS (display quantization happens in the seam, the engine stays full
precision). The single-symbol variant lets the drawer render ONE symbol without fetching
the whole held universe.
"""

import sqlite3
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends

from portfolio_dash.api import signals_service
from portfolio_dash.api.deps import get_conn, get_now, get_reporting
from portfolio_dash.shared.enums import Currency

router = APIRouter()


@router.get("/signals")
def signals(
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
    reporting: Currency = Depends(get_reporting),
) -> dict[str, Any]:
    """Rule-engine signals for every current holding (honest nulls where data is thin)."""
    results = signals_service.evaluate_held(conn, now=now, reporting=reporting)
    return {
        "as_of": now.date().isoformat(),
        "evaluated_at": now.isoformat(),
        "signals": [signals_service.to_wire(sym, sig, now=now) for sym, sig in results],
    }


@router.get("/signals/{symbol}")
def signal(
    symbol: str,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """One symbol's rule-engine signals — the drawer path (no holdings enumeration)."""
    result = signals_service.evaluate_symbol(conn, symbol, now=now)
    return signals_service.to_wire(symbol, result, now=now)
