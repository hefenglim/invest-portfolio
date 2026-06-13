"""LLM settings API (spec 16): model registry CRUD, role assignment, quota, usage.

Thin router. Model CRUD calls ``shared/llm_config`` helpers (no raw SQL for models);
usage aggregations come from ``shared/llm_usage_reads``. The raw ``api_key`` is
write-only — responses only ever expose ``api_key_masked``. Money is serialized as
Decimal strings. Errors use the common ``error_body`` envelope.
"""

import sqlite3
import time
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from portfolio_dash.api.deps import get_conn
from portfolio_dash.api.errors import error_body
from portfolio_dash.shared import llm
from portfolio_dash.shared.llm_config import (
    LLMRole,
    ModelConfig,
    add_topup,
    all_role_bindings,
    delete_model,
    get_alert_threshold,
    get_model,
    list_models,
    list_topups,
    litellm_model_string,
    quota_remaining,
    roles_using_model,
    set_alert_threshold,
    set_role,
    upsert_model,
)
from portfolio_dash.shared.llm_usage_reads import (
    model_health,
    usage_by_agent,
    usage_by_model,
    usage_daily,
)

router = APIRouter()

# Map the request/response role keys (spec 16 wire names) to LLMRole enum members.
_ROLE_FIELDS: dict[str, LLMRole] = {
    "default_model": LLMRole.DEFAULT,
    "default_fallback": LLMRole.DEFAULT_FALLBACK,
    "vision_model": LLMRole.VISION,
    "vision_fallback": LLMRole.VISION_FALLBACK,
    "master_model": LLMRole.MASTER,
    "master_fallback": LLMRole.MASTER_FALLBACK,
}
# (main, fallback) pairs for the "fallback must differ from main" check.
_ROLE_PAIRS = (
    ("default_model", "default_fallback"),
    ("vision_model", "vision_fallback"),
    ("master_model", "master_fallback"),
)


def _mask_key(key: str | None) -> str | None:
    """Mask an API key as ``prefix(3) + "•••" + suffix(3)``; ``None`` stays ``None``."""
    if not key:
        return None
    if len(key) <= 6:
        return "•••"
    return f"{key[:3]}•••{key[-3:]}"


def _model_wire(
    m: ModelConfig, health: dict[str, Any]
) -> dict[str, Any]:
    """Serialize one model to the spec-16 shape (key masked, money as strings)."""
    h = health.get(m.model_alias)
    return {
        "alias": m.model_alias,
        "provider": m.provider,
        "model_name": m.model_name,
        "api_base": m.api_base,
        "api_key_masked": _mask_key(m.api_key),
        "vision": m.vision,
        "price_in": str(m.input_price_per_mtok),
        "price_out": str(m.output_price_per_mtok),
        "context_window": m.context_window,
        "max_output_tokens": m.max_output_tokens,
        "timeout_seconds": m.timeout_seconds,
        "max_retries": m.max_retries,
        "enabled": m.enabled,
        "notes": m.notes,
        "health": h.health if h is not None else "unknown",
        "last_called": h.last_called if h is not None else None,
    }


def _roles_wire(conn: sqlite3.Connection) -> dict[str, str | None]:
    bound = all_role_bindings(conn)
    return {field: bound[role.value] for field, role in _ROLE_FIELDS.items()}


def _quota_wire(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "remaining_usd": str(quota_remaining(conn)),
        "alert_threshold_usd": str(get_alert_threshold(conn)),
        "topups": list_topups(conn),
    }


def _usage_wire(conn: sqlite3.Connection) -> dict[str, Any]:
    daily = usage_daily(conn)
    return {
        "by_model": [
            {
                "alias": u.alias,
                "calls": u.calls,
                "tokens_in": u.tokens_in,
                "tokens_out": u.tokens_out,
                "cost_usd": str(u.cost_usd),
            }
            for u in usage_by_model(conn)
        ],
        "by_agent": [
            {"agent": u.agent, "cost_usd": str(u.cost_usd)} for u in usage_by_agent(conn)
        ],
        "daily": {
            "dates": daily.dates,
            "series": [
                {"alias": s.alias, "costs": [str(c) for c in s.costs]}
                for s in daily.series
            ],
        },
    }


@router.get("/llm/config")
def get_config(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    health = model_health(conn)
    return {
        "models": [_model_wire(m, health) for m in list_models(conn)],
        "roles": _roles_wire(conn),
        "quota": _quota_wire(conn),
        "usage": _usage_wire(conn),
    }


# --- 16.2 model registry CRUD -------------------------------------------------


class ModelBody(BaseModel):
    """Create/update payload. ``api_key`` is write-only; alias is the identity."""

    alias: str | None = None  # required on POST, ignored on PUT (path is authoritative)
    provider: str | None = None
    model_name: str | None = None
    api_base: str | None = None
    api_key: str | None = None
    vision: bool | None = None
    price_in: Decimal | None = None
    price_out: Decimal | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    timeout_seconds: int | None = None
    max_retries: int | None = None
    enabled: bool | None = None
    notes: str | None = None


def _single_model_response(
    conn: sqlite3.Connection, alias: str, *, status_code: int
) -> JSONResponse:
    m = get_model(conn, alias)
    assert m is not None  # caller has just written it
    return JSONResponse(status_code=status_code, content=_model_wire(m, model_health(conn)))


@router.post("/llm/models")
def create_model(body: ModelBody, conn: sqlite3.Connection = Depends(get_conn)) -> Any:
    if not body.alias:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", "alias is required", field="alias"))
    if get_model(conn, body.alias) is not None:
        return JSONResponse(status_code=409, content=error_body(
            "duplicate_alias", f"alias '{body.alias}' already exists", field="alias"))
    model = ModelConfig(
        id=body.alias,
        model_alias=body.alias,
        provider=body.provider or "openai",
        model_name=body.model_name or "",
        api_base=body.api_base,
        api_key=body.api_key,
        vision=bool(body.vision),
        input_price_per_mtok=body.price_in if body.price_in is not None else Decimal("0"),
        output_price_per_mtok=body.price_out if body.price_out is not None else Decimal("0"),
        context_window=body.context_window,
        max_output_tokens=body.max_output_tokens,
        timeout_seconds=body.timeout_seconds,
        max_retries=body.max_retries,
        enabled=True if body.enabled is None else body.enabled,
        notes=body.notes,
    )
    upsert_model(conn, model)
    return _single_model_response(conn, body.alias, status_code=201)


@router.put("/llm/models/{alias}")
def update_model(
    alias: str, body: ModelBody, conn: sqlite3.Connection = Depends(get_conn)
) -> Any:
    existing = get_model(conn, alias)
    if existing is None:
        return JSONResponse(status_code=404, content=error_body(
            "not_found", f"model '{alias}' not found", field="alias"))
    # PUT is a subset update; alias is immutable (path wins). Unset fields keep value.
    updated = existing.model_copy(update={
        "provider": existing.provider if body.provider is None else body.provider,
        "model_name": existing.model_name if body.model_name is None else body.model_name,
        "api_base": existing.api_base if body.api_base is None else body.api_base,
        "api_key": existing.api_key if body.api_key is None else body.api_key,
        "vision": existing.vision if body.vision is None else body.vision,
        "input_price_per_mtok":
            existing.input_price_per_mtok if body.price_in is None else body.price_in,
        "output_price_per_mtok":
            existing.output_price_per_mtok if body.price_out is None else body.price_out,
        "context_window":
            existing.context_window if body.context_window is None else body.context_window,
        "max_output_tokens":
            existing.max_output_tokens
            if body.max_output_tokens is None else body.max_output_tokens,
        "timeout_seconds":
            existing.timeout_seconds if body.timeout_seconds is None else body.timeout_seconds,
        "max_retries": existing.max_retries if body.max_retries is None else body.max_retries,
        "enabled": existing.enabled if body.enabled is None else body.enabled,
        "notes": existing.notes if body.notes is None else body.notes,
    })
    upsert_model(conn, updated)
    return _single_model_response(conn, alias, status_code=200)


@router.delete("/llm/models/{alias}")
def remove_model(alias: str, conn: sqlite3.Connection = Depends(get_conn)) -> Any:
    if get_model(conn, alias) is None:
        return JSONResponse(status_code=404, content=error_body(
            "not_found", f"model '{alias}' not found", field="alias"))
    bound = roles_using_model(conn, alias)
    if bound:
        return JSONResponse(status_code=422, content=error_body(
            "model_in_use", f"{alias} 仍被 {', '.join(bound)} 角色指派"))
    delete_model(conn, alias)
    return {"ok": True, "alias": alias}


# --- 16.3 role assignment -----------------------------------------------------


class RolesBody(BaseModel):
    default_model: str | None = None
    default_fallback: str | None = None
    vision_model: str | None = None
    vision_fallback: str | None = None
    master_model: str | None = None
    master_fallback: str | None = None


@router.put("/llm/roles")
def put_roles(body: RolesBody, conn: sqlite3.Connection = Depends(get_conn)) -> Any:
    fields = body.model_dump()
    # Validate every referenced alias exists and is enabled before writing anything.
    for field, role in _ROLE_FIELDS.items():
        alias = fields[field]
        if alias is None:
            continue
        model = get_model(conn, alias)
        if model is None:
            return JSONResponse(status_code=400, content=error_body(
                "validation_error", f"角色 {field} 指向不存在的模型 '{alias}'", field=field))
        if not model.enabled:
            return JSONResponse(status_code=400, content=error_body(
                "validation_error", f"角色 {field} 指向已停用的模型 '{alias}'", field=field))
        if role is LLMRole.VISION and not model.vision:
            return JSONResponse(status_code=400, content=error_body(
                "validation_error",
                f"vision 角色不可指向不支援視覺的模型 '{alias}'", field=field))
    # Fallback must differ from its main model (a self-fallback is meaningless).
    for main, fb in _ROLE_PAIRS:
        if fields[main] is not None and fields[main] == fields[fb]:
            return JSONResponse(status_code=400, content=error_body(
                "validation_error", f"{fb} 不可與 {main} 相同", field=fb))
    for field, role in _ROLE_FIELDS.items():
        set_role(conn, role, fields[field])
    return _roles_wire(conn)


# --- 16.4 connection test / quota top-up / alert threshold --------------------


@router.post("/llm/models/{alias}/test")
def test_model(alias: str, conn: sqlite3.Connection = Depends(get_conn)) -> Any:
    model = get_model(conn, alias)
    if model is None:
        return JSONResponse(status_code=404, content=error_body(
            "not_found", f"model '{alias}' not found", field="alias"))
    started = time.monotonic()
    try:
        resp = llm.litellm.completion(
            model=litellm_model_string(model),
            api_base=model.api_base or None,
            api_key=model.api_key or None,
            messages=[{"role": "user", "content": "ping"}],
            timeout=model.timeout_seconds,
            max_tokens=8,
        )
    except Exception as exc:  # noqa: BLE001 - any provider failure is reported, not raised
        latency_ms = int((time.monotonic() - started) * 1000)
        return {"ok": False, "latency_ms": latency_ms, "error_detail": str(exc)}
    latency_ms = int((time.monotonic() - started) * 1000)
    content = (resp.choices[0].message.content or "")[:120]
    usage = getattr(resp, "usage", None)
    if usage is not None:  # honest accounting: a real test call costs tokens
        pricing = llm.ModelPricing(
            model=model.model_name,
            input_price_per_mtok=model.input_price_per_mtok,
            output_price_per_mtok=model.output_price_per_mtok,
        )
        llm.log_usage(
            conn,
            model=model.model_name,
            agent="llm_settings_test",
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
            cost=llm.cost_of(pricing, usage.prompt_tokens, usage.completion_tokens),
        )
    return {"ok": True, "latency_ms": latency_ms, "reply_snippet": content}


class TopupBody(BaseModel):
    amount_usd: Decimal
    note: str | None = None


@router.post("/llm/quota/topup")
def topup_quota(body: TopupBody, conn: sqlite3.Connection = Depends(get_conn)) -> Any:
    if body.amount_usd <= 0:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", "加值金額需大於 0", field="amount_usd"))
    add_topup(conn, body.amount_usd, body.note)
    return {"remaining_usd": str(quota_remaining(conn))}


class QuotaBody(BaseModel):
    alert_threshold_usd: Decimal


@router.put("/llm/quota")
def put_quota(body: QuotaBody, conn: sqlite3.Connection = Depends(get_conn)) -> Any:
    if body.alert_threshold_usd < 0:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", "警示閾值不可為負", field="alert_threshold_usd"))
    set_alert_threshold(conn, body.alert_threshold_usd)
    return {"alert_threshold_usd": str(get_alert_threshold(conn))}


__all__ = ["router"]
