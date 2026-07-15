"""Digest API (P3 batch 3 · Wave 1): latest / history / manual run / config.

Thin router over ``ops.digest`` (storage + config) and the scheduler run seam. READS
(latest / history / config GET) are open. The manual run (``POST /digest/run``) is ALSO open
in guest/demo mode (FU-D4 — a compute+cache action; the outbound push is separately
suppressed in ``digest_service._push`` when guest), while ``PUT /digest/config`` keeps its
**403** in guest/demo mode — mirroring ``api/routers/notify.py`` (``auth_store.is_protected``,
an app-level check independent of the global session gate). The manual run mirrors the
scheduler router's ``/run`` (async 202 + background thread + a ``job_runs`` row; 409 when a
run is already in flight).

Money / percentages in a payload are Decimal **strings** assembled by
``api/digest_service`` — this router serializes, never computes.
"""

import sqlite3
import threading
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from portfolio_dash.api import auth_store
from portfolio_dash.api.deps import get_conn, get_now
from portfolio_dash.api.errors import error_body
from portfolio_dash.ops import digest as digest_store
from portfolio_dash.scheduler.jobs import (
    latest_run_unfinished,
    run_job_func,
    start_job_run,
)

router = APIRouter()

# kind → the static scheduler job id whose runner assembles that edition.
_JOB_ID: dict[str, str] = {"daily": "digest_daily", "weekly": "digest_weekly"}


def _bad_kind() -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content=error_body("validation_error", "kind 需為 daily 或 weekly", field="kind"),
    )


def _guest_forbidden() -> JSONResponse:
    """403 for digest WRITES in guest/demo mode (mirrors the notify F1 lockdown)."""
    return JSONResponse(
        status_code=403,
        content=error_body("forbidden", "摘要設定僅於受保護模式開放（示範站不開放）"),
    )


@router.get("/digest/latest")
def get_latest(
    kind: str = "daily", conn: sqlite3.Connection = Depends(get_conn)
) -> Any:
    """Latest stored digest of *kind* (or ``null`` when none exists)."""
    if kind not in digest_store.VALID_KINDS:
        return _bad_kind()
    return digest_store.get_latest(conn, kind)


@router.get("/digest/history")
def get_history(
    kind: str = "daily",
    offset: int = 0,
    limit: int = 5,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    """Page stored digests (newest first). ``offset >= 0`` and ``1 <= limit <= 20``."""
    if kind not in digest_store.VALID_KINDS:
        return _bad_kind()
    if offset < 0:
        return JSONResponse(
            status_code=400,
            content=error_body("validation_error", "offset 不可為負數", field="offset"),
        )
    if limit < 1 or limit > 20:
        return JSONResponse(
            status_code=400,
            content=error_body("validation_error", "limit 必須介於 1 到 20", field="limit"),
        )
    total, rows = digest_store.get_history(conn, kind, offset=offset, limit=limit)
    return {"total": total, "offset": offset, "rows": rows}


class _RunBody(BaseModel):
    kind: str


@router.post("/digest/run")
def run_now(
    body: _RunBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    """Manually regenerate a digest (async 202); already-running → 409.

    Open in guest/demo mode (FU-D4): generation + caching is a compute action, so it is NOT
    gated — but ``digest_service._push`` suppresses the outbound push when the app is in guest
    mode, so no notification leaves the demo. ``PUT /digest/config`` keeps its 403.
    """
    if body.kind not in digest_store.VALID_KINDS:
        return _bad_kind()
    job_id = _JOB_ID[body.kind]
    if latest_run_unfinished(conn, job_id):
        return JSONResponse(
            status_code=409, content=error_body("already_running", f"{job_id} 執行中")
        )
    run_id = start_job_run(conn, job_id, now=now)
    thread = threading.Thread(
        target=run_job_func, kwargs={"job_id": job_id, "now": now}, daemon=True
    )
    thread.start()
    return JSONResponse(status_code=202, content={"run_id": run_id, "kind": body.kind})


class _ConfigBody(BaseModel):
    llm_summary_enabled: bool


@router.get("/digest/config")
def get_config(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    """The digest config: ``{llm_summary_enabled}`` (the optional AI one-liner switch)."""
    cfg = digest_store.load_config(conn)
    return {"llm_summary_enabled": cfg.llm_summary_enabled}


@router.put("/digest/config")
def put_config(
    body: _ConfigBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    """Set the AI one-liner switch (default off). Guest → 403."""
    if not auth_store.is_protected(conn):
        return _guest_forbidden()
    cfg = digest_store.load_config(conn)
    cfg.llm_summary_enabled = body.llm_summary_enabled
    digest_store.save_config(conn, cfg, now=now)
    return {"llm_summary_enabled": cfg.llm_summary_enabled}


__all__ = ["router"]
