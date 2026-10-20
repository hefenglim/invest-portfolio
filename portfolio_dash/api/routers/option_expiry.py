"""待確認到期 API (options spec §7.2, OPT-D13) — mirrors ``/api/dividend-inbox/*``.

Thin: every decision is ``api/option_expiry.py``'s. GETs compute on read and never write
(DEF-065); confirm acts on the server's recomputed items only and writes through the manual
door's path. Wired into ``api/app.py`` (router + ``ensure_tables`` at boot) by the main session.
"""

import sqlite3
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from portfolio_dash.api import option_expiry as inbox
from portfolio_dash.api.deps import get_conn, get_now
from portfolio_dash.api.errors import error_body
from portfolio_dash.shared.wire import to_wire

router = APIRouter()


class ItemKey(BaseModel):
    """One item: an account × contract."""

    model_config = {"extra": "forbid"}

    account_id: str
    symbol: str


@router.get("/option-expiry")
def list_items(
    conn: sqlite3.Connection = Depends(get_conn, scope="function"),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    return {"items": [to_wire(i.model_dump()) for i in inbox.detect(conn, now=now)]}


@router.get("/option-expiry/count")
def count(
    conn: sqlite3.Connection = Depends(get_conn, scope="function"),
    now: datetime = Depends(get_now),
) -> dict[str, int]:
    """The sidebar badge's share (added to the other inboxes' counts by the shell)."""
    return {"count": inbox.pending_count(conn, now=now)}


@router.post("/option-expiry/confirm")
def confirm(
    body: ItemKey,
    conn: sqlite3.Connection = Depends(get_conn, scope="function"),
    now: datetime = Depends(get_now),
) -> Any:
    try:
        txn_id = inbox.confirm(conn, body.account_id, body.symbol, now=now)
    except inbox.ExpiryConfirmError as exc:
        return JSONResponse(status_code=422, content=error_body(
            exc.code, exc.message, issues=exc.issues_wire() or None))
    return {"ok": True, "txn_id": txn_id}


@router.post("/option-expiry/confirm-all")
def confirm_all(
    conn: sqlite3.Connection = Depends(get_conn, scope="function"),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    results = inbox.confirm_all(conn, now=now)
    out: list[dict[str, Any]] = []
    for r in results:
        # Spec §7.2: `txn_id` on a success, `error` on a refusal — one or the other.
        entry: dict[str, Any] = {"account_id": r.account_id, "symbol": r.symbol, "ok": r.ok}
        if r.ok:
            entry["txn_id"] = r.txn_id
        else:
            entry["error"] = r.error
        out.append(entry)
    return {"results": out}


@router.post("/option-expiry/skip")
def skip(
    body: ItemKey,
    conn: sqlite3.Connection = Depends(get_conn, scope="function"),
    now: datetime = Depends(get_now),
) -> Any:
    try:
        inbox.mark_skipped(conn, body.account_id, body.symbol, now=now)
    except ValueError as exc:
        return JSONResponse(status_code=422, content=error_body(
            "validation_error", str(exc), field="symbol"))
    return {"ok": True}


@router.post("/option-expiry/unskip")
def unskip(
    body: ItemKey,
    conn: sqlite3.Connection = Depends(get_conn, scope="function"),
) -> dict[str, bool]:
    inbox.unskip(conn, body.account_id, body.symbol)
    return {"ok": True}


@router.get("/option-expiry/skipped")
def list_skipped(
    conn: sqlite3.Connection = Depends(get_conn, scope="function"),
) -> dict[str, Any]:
    return {"items": [to_wire(s.model_dump()) for s in inbox.list_skipped(conn)]}
