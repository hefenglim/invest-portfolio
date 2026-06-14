"""Insight-composer API (spec 04.7 / 4.9 R1 / 4.2 / 4.6): the static composer surface.

Thin router over ``llm_insight.composer_store`` (+ ``scheduler`` binding helpers; api →
scheduler is allowed). It does CRUD + validation + serialize only — no LLM call, no
insight generation, no evaluation (those are 04b/04c). R1 (spec 4.9) reuses the single
``variables.validate_tokens`` core: a non-``per_symbol`` insight_type whose referenced
strategy bodies use a ``per_symbol`` variable is rejected at create/update with 422.
"""

import sqlite3
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from portfolio_dash.api.deps import get_conn, get_now
from portfolio_dash.api.errors import error_body
from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.llm_insight import variables as V
from portfolio_dash.scheduler.jobs import insight_job_id, unbind_insight_schedule

router = APIRouter()


# --- request bodies -----------------------------------------------------------


class StrategyIn(BaseModel):
    name: str
    body: str
    enabled: bool = True


class InsightTypeIn(BaseModel):
    name: str
    scope: str  # 'per_symbol' | 'portfolio' | 'on_alert'
    strategy_ids: list[int] = []
    use_system_prompt: bool = True
    self_correct: bool = False
    universe: dict[str, Any] | list[Any] | None = None
    alert_rules: dict[str, Any] | list[Any] | None = None
    enabled: bool | None = None  # None -> defaulted by scope (on_alert -> False, R7)


# --- serialization ------------------------------------------------------------


def _schedule_for(conn: sqlite3.Connection, insight_type_id: int) -> dict[str, Any] | None:
    """Read the kind=insight ``schedule_config`` binding (cron) for an insight_type, or None."""
    row = conn.execute(
        "SELECT cron FROM schedule_config WHERE job_id = ?",
        (insight_job_id(insight_type_id),),
    ).fetchone()
    return {"cron": row["cron"]} if row is not None else None


def _insight_type_wire(conn: sqlite3.Connection, it: cs.InsightType) -> dict[str, Any]:
    """The GET-shape view of one insight_type (strategies + schedule + calib summary).

    ``calib_summary`` is null in 04a (accumulated scores arrive in 04c). ``schedule`` is
    read live from the ``schedule_config`` binding row.
    """
    strategies = cs.get_strategies(conn, it.id)
    return {
        "id": it.id,
        "name": it.name,
        "scope": it.scope,
        "strategies": [
            {"id": s.id, "name": s.name, "position": s.position} for s in strategies
        ],
        "self_correct": it.self_correct,
        "use_system_prompt": it.use_system_prompt,
        "universe": it.universe,
        "alert_rules": it.alert_rules,
        "enabled": it.enabled,
        "schedule": _schedule_for(conn, it.id),
        "active_calibration_version": it.active_calibration_version,
        "calib_summary": None,  # populated by 04c
    }


# --- R1 (spec 4.9) ------------------------------------------------------------


def _r1_violations(conn: sqlite3.Connection, scope: str, strategy_ids: list[int]) -> list[str]:
    """Collect R1 scope violations across the referenced strategies' bodies.

    Reuses the single ``variables.validate_tokens`` core. A ``per_symbol`` scope accepts
    every variable (no violation possible); otherwise any ``per_symbol`` variable used in
    a referenced strategy body is a violation. De-duplicated, in first-seen order.
    """
    if scope == "per_symbol":
        return []
    seen: list[str] = []
    for sid in strategy_ids:
        sp = cs.get_strategy(conn, sid)
        if sp is None:
            continue
        for token in V.validate_tokens(sp.body, scope).scope_violations:
            if token not in seen:
                seen.append(token)
    return seen


def _r1_error_response(tokens: list[str]) -> JSONResponse:
    issues = [{"code": "scope_violation", "token": t} for t in tokens]
    return JSONResponse(
        status_code=422,
        content=error_body(
            "validation_error",
            "insight type scope conflicts with a strategy's per-symbol variable",
            issues=issues,
        ),
    )


# --- strategy-prompts CRUD ----------------------------------------------------


@router.get("/strategy-prompts")
def list_strategy_prompts(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict[str, Any]]:
    cs.ensure_seeded(conn)
    return [s.model_dump() for s in cs.list_strategies(conn)]


@router.post("/strategy-prompts")
def create_strategy_prompt(
    payload: StrategyIn,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    cs.ensure_seeded(conn)
    sp = cs.create_strategy(conn, name=payload.name, body=payload.body, now=now)
    return sp.model_dump()


@router.put("/strategy-prompts/{strategy_id}")
def update_strategy_prompt(
    strategy_id: int,
    payload: StrategyIn,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    cs.ensure_seeded(conn)
    sp = cs.update_strategy(
        conn, strategy_id, name=payload.name, body=payload.body,
        enabled=payload.enabled, now=now,
    )
    if sp is None:
        return JSONResponse(
            status_code=404,
            content=error_body("not_found", f"未知策略提示詞：{strategy_id}"),
        )
    return sp.model_dump()


@router.delete("/strategy-prompts/{strategy_id}")
def delete_strategy_prompt(
    strategy_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    """Spec-4.1 strategy delete: 409 with the referencing list while in use, else
    archive (if it has history) or hard-delete."""
    cs.ensure_seeded(conn)
    try:
        outcome = cs.delete_strategy(conn, strategy_id, now=now)
    except cs.StrategyInUseError as exc:
        # Spec 4.1: surface the referencing insight_type ids on the error envelope.
        envelope = error_body("conflict", "策略提示詞仍被洞察組合引用，無法刪除")
        envelope["error"]["referencing"] = exc.referencing_insight_type_ids
        return JSONResponse(status_code=409, content=envelope)
    if outcome is None:
        return JSONResponse(
            status_code=404,
            content=error_body("not_found", f"未知策略提示詞：{strategy_id}"),
        )
    return {"id": strategy_id, "outcome": outcome}


# --- insight-types CRUD -------------------------------------------------------


@router.get("/insight-types")
def list_insight_types(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict[str, Any]]:
    cs.ensure_seeded(conn)
    return [_insight_type_wire(conn, it) for it in cs.list_insight_types(conn)]


@router.post("/insight-types")
def create_insight_type(
    payload: InsightTypeIn,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    cs.ensure_seeded(conn)
    violations = _r1_violations(conn, payload.scope, payload.strategy_ids)
    if violations:
        return _r1_error_response(violations)
    # R7: a new on_alert insight_type defaults to disabled unless explicitly enabled.
    enabled = payload.enabled
    if enabled is None:
        enabled = payload.scope != "on_alert"
    it = cs.create_insight_type(
        conn, name=payload.name, scope=payload.scope,
        use_system_prompt=payload.use_system_prompt, self_correct=payload.self_correct,
        universe=payload.universe, alert_rules=payload.alert_rules, enabled=enabled,
        now=now,
    )
    cs.set_strategies(conn, it.id, [(sid, pos) for pos, sid in enumerate(payload.strategy_ids)])
    return _insight_type_wire(conn, it)


@router.put("/insight-types/{insight_type_id}")
def update_insight_type(
    insight_type_id: int,
    payload: InsightTypeIn,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    cs.ensure_seeded(conn)
    if cs.get_insight_type(conn, insight_type_id) is None:
        return JSONResponse(
            status_code=404,
            content=error_body("not_found", f"未知洞察組合：{insight_type_id}"),
        )
    violations = _r1_violations(conn, payload.scope, payload.strategy_ids)
    if violations:
        return _r1_error_response(violations)
    enabled = payload.enabled if payload.enabled is not None else True
    it = cs.update_insight_type(
        conn, insight_type_id, name=payload.name, scope=payload.scope,
        use_system_prompt=payload.use_system_prompt, self_correct=payload.self_correct,
        universe=payload.universe, alert_rules=payload.alert_rules, enabled=enabled,
        now=now,
    )
    assert it is not None  # existence checked above
    cs.set_strategies(
        conn, insight_type_id, [(sid, pos) for pos, sid in enumerate(payload.strategy_ids)]
    )
    return _insight_type_wire(conn, it)


@router.delete("/insight-types/{insight_type_id}")
def delete_insight_type(
    insight_type_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    """Spec-4.1 insight_type delete: archive + clear schedule binding + archive calib chain."""
    cs.ensure_seeded(conn)
    it = cs.delete_insight_type(conn, insight_type_id, now=now)
    if it is None:
        return JSONResponse(
            status_code=404,
            content=error_body("not_found", f"未知洞察組合：{insight_type_id}"),
        )
    unbind_insight_schedule(conn, insight_type_id)  # remove the schedule_config row (4.1)
    return _insight_type_wire(conn, it)
