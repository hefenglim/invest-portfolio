"""Instruments registry API (spec 10): list (+ probe + register/update in later tasks).

Thin over data_ingestion.store + pricing.store reads. Computes nothing of record.
"""

import sqlite3
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends

from portfolio_dash.api.deps import get_conn, get_now
from portfolio_dash.data_ingestion.holdings import current_shares
from portfolio_dash.data_ingestion.store import list_accounts, list_instruments
from portfolio_dash.pricing.store import get_latest_price, get_price_history
from portfolio_dash.shared.models.assets import Instrument

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
    last = str(pr.value) if pr is not None else None
    chg_pct: str | None = None
    if pr is not None:
        hist = get_price_history(conn, inst.symbol, pr.as_of.replace(day=1), pr.as_of)
        if len(hist) >= 2 and hist[-2].value != 0:
            chg_pct = str((hist[-1].value - hist[-2].value) / hist[-2].value)
    return {
        "symbol": inst.symbol, "name": inst.name, "market": inst.market.value,
        "board": _board_wire(conn, inst), "sector": inst.sector,
        "ccy": inst.quote_ccy.value, "held": _held(conn, account_ids, inst.symbol),
        "last": last, "chg_pct": chg_pct,
        "target_low": str(inst.target_low) if inst.target_low is not None else None,
    }


@router.get("/instruments")
def list_all(
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    account_ids = [a.account_id for a in list_accounts(conn)]
    items = [_element(conn, inst, account_ids, now) for inst in list_instruments(conn)]
    return {"as_of": now.isoformat(), "list": items}
