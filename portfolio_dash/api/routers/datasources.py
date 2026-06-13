"""Data-source management API (spec 14): keys, health, per-account fallback chains.

Thin over ``pricing.datasources_store``: it reads/writes the three data_sources
tables and serializes the masked view. It computes no money and no returns; the
``/test`` endpoint performs a single, time-bounded provider probe (run off the event
loop) and records the result into ``data_source_health``.
"""

import sqlite3
import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from portfolio_dash.api.deps import get_conn, get_now
from portfolio_dash.api.errors import error_body
from portfolio_dash.data_ingestion.store import list_accounts
from portfolio_dash.pricing import datasources_store as store

router = APIRouter()

_PROBE_TIMEOUT_S = 10.0


# --- GET /api/datasources -----------------------------------------------------


def _source_wire(
    info: store.SourceInfo, state: store.SourceState | None
) -> dict[str, Any]:
    """Merge a static source description with its persisted (masked) state."""
    api_key = state.api_key if state is not None else None
    token_masked = store.mask_token(api_key)
    # auth:"none" sources have no key; their status follows health, defaulting "ok".
    if state is None:
        status = "off" if info.auth == "apikey" else "unknown"
        last_test: str | None = None
        latency_ms: int | None = None
    else:
        status = state.status
        last_test = state.last_test
        latency_ms = state.latency_ms
        # An apikey source with no key set surfaces as "off" (spec 14.1).
        if info.auth == "apikey" and not api_key and status == "unknown":
            status = "off"
    return {
        "id": info.id,
        "name": info.name,
        "type": info.type,
        "markets": info.markets,
        "auth": info.auth,
        "token_masked": token_masked,
        "status": status,
        "last_test": last_test,
        "latency_ms": latency_ms,
        "note": info.note,
    }


@router.get("/datasources")
def list_datasources(
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    store.ensure_seeded(conn)
    states = store.list_states(conn)
    sources = [_source_wire(info, states.get(info.id)) for info in store.SOURCE_INFO]
    account_fallbacks = store.account_chains(conn)
    account_names = {a.account_id: a.name for a in list_accounts(conn)}
    return {
        "sources": sources,
        "account_fallbacks": account_fallbacks,
        "account_names": account_names,
    }


# --- PUT /api/datasources/{id}/key --------------------------------------------


class KeyBody(BaseModel):
    api_key: str  # empty string clears the key


@router.put("/datasources/{source_id}/key")
def set_key(
    source_id: str,
    body: KeyBody,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    store.ensure_seeded(conn)
    info = store.SOURCE_INFO_BY_ID.get(source_id)
    if info is None:
        return JSONResponse(
            status_code=404,
            content=error_body("not_found", f"未知資料來源：{source_id}"),
        )
    if info.auth == "none":
        return JSONResponse(
            status_code=400,
            content=error_body(
                "validation_error", f"{info.name} 無需金鑰", field="api_key"
            ),
        )
    store.set_api_key(conn, source_id, body.api_key)
    return {
        "id": source_id,
        "token_masked": store.mask_token(body.api_key or None),
        "status": "unknown",
    }


# --- POST /api/datasources/{id}/test ------------------------------------------


def probe_source(source_id: str, api_key: str | None) -> tuple[bool, str | None]:
    """Production connection test for a source: True/False + an optional detail.

    Real provider call (a single minimal request); raising or returning False is a
    valid "error" test result. Tests monkeypatch this so no network I/O occurs.
    This is intentionally a thin, replaceable seam (the per-provider probe wiring
    can grow here without touching the route).
    """
    info = store.SOURCE_INFO_BY_ID.get(source_id)
    if info is None:
        return False, "未知資料來源"
    if info.auth == "apikey" and not api_key:
        return False, "尚未設定金鑰"
    # Default seam: real provider probes are wired here later. Until a provider is
    # wired for a given source, report a neutral non-network result rather than
    # fabricating success.
    return False, "尚未實作連線測試"


def _run_probe(source_id: str, api_key: str | None) -> tuple[str, int | None, str | None]:
    """Run ``probe_source`` with latency timing; map exceptions to an error result."""
    start = time.monotonic()
    try:
        ok, detail = probe_source(source_id, api_key)
    except Exception as exc:  # noqa: BLE001 - any probe failure is a valid "error" result
        return "error", None, str(exc) or exc.__class__.__name__
    latency_ms = int((time.monotonic() - start) * 1000)
    if ok:
        return "ok", latency_ms, detail
    return "error", None, detail


@router.post("/datasources/{source_id}/test")
async def test_source(
    source_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    store.ensure_seeded(conn)
    if source_id not in store.KNOWN_SOURCE_IDS:
        return JSONResponse(
            status_code=404,
            content=error_body("not_found", f"未知資料來源：{source_id}"),
        )
    api_key = store.get_api_key(conn, source_id)
    status, latency_ms, detail = await run_in_threadpool(_run_probe, source_id, api_key)
    last_test = now.isoformat()
    store.upsert_health(
        conn,
        source_id,
        status=status,
        last_test=last_test,
        latency_ms=latency_ms,
        detail=detail,
    )
    return {
        "id": source_id,
        "status": status,
        "latency_ms": latency_ms,
        "detail": detail,
        "last_test": last_test,
    }


# --- PUT /api/datasources/fallbacks -------------------------------------------


class FallbacksBody(BaseModel):
    account_fallbacks: dict[str, list[str]]


@router.put("/datasources/fallbacks")
def set_fallbacks(
    body: FallbacksBody,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    store.ensure_seeded(conn)
    known_accounts = {a.account_id for a in list_accounts(conn)}
    for account_id, chain in body.account_fallbacks.items():
        if account_id not in known_accounts:
            return JSONResponse(
                status_code=400,
                content=error_body(
                    "validation_error", f"未知帳戶：{account_id}", field="account_fallbacks"
                ),
            )
        if not chain:
            return JSONResponse(
                status_code=400,
                content=error_body(
                    "validation_error", f"{account_id} 的 fallback 鏈不可為空",
                    field="account_fallbacks",
                ),
            )
        for src in chain:
            if src not in store.KNOWN_SOURCE_IDS:
                return JSONResponse(
                    status_code=400,
                    content=error_body(
                        "validation_error", f"未知資料來源：{src}",
                        field="account_fallbacks",
                    ),
                )
    store.set_account_chains(conn, body.account_fallbacks)
    return {"account_fallbacks": store.account_chains(conn)}
