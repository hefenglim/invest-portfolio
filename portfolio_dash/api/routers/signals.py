"""GET /api/signals — per-symbol rule-engine signals (P2 batch 2; watchlist P2 batch 3).

Thin router (decision B): it reads prices + calls the pure rule engine via the api seam
(``signals_service``) and serializes. It computes no numbers of record; scores/ratios go
out as Decimal STRINGS (display quantization happens in the seam, the engine stays full
precision). The universe is every REGISTERED instrument (held + watchlist) — each entry is
tagged ``held`` so the frontend/LLM knows whether it is a position or an entry candidate.
The single-symbol variant lets the drawer render ONE symbol without enumerating the rest.
"""

import sqlite3
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends

from portfolio_dash.api import signals_service
from portfolio_dash.api.deps import get_conn, get_now

router = APIRouter()


@router.get("/signals")
def signals(
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """Rule-engine signals for every registered symbol (held + watchlist; honest nulls
    where data is thin). Each entry carries a ``held`` flag."""
    results = signals_service.evaluate_all(conn, now=now)
    return {
        "as_of": now.date().isoformat(),
        "evaluated_at": now.isoformat(),
        "signals": [
            signals_service.to_wire(sym, sig, now=now, held=held)
            for sym, sig, held in results
        ],
    }


@router.get("/signals/{symbol}")
def signal(
    symbol: str,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """One symbol's rule-engine signals — the drawer path (no holdings enumeration). Any
    registered or ad-hoc symbol resolves; the ``held`` flag marks whether it is a position."""
    result = signals_service.evaluate_symbol(conn, symbol, now=now)
    return signals_service.to_wire(
        symbol, result, now=now, held=signals_service.is_held(conn, symbol)
    )
