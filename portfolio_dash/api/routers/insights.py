"""Insight-composer API (spec 04.7 / 4.9 R1 / 4.2 / 4.6): the static composer surface.

Thin router over ``llm_insight.composer_store`` (+ ``scheduler`` binding helpers; api →
scheduler is allowed). It does CRUD + validation + serialize only — no LLM call, no
insight generation, no evaluation (those are 04b/04c). R1 (spec 4.9) reuses the single
``variables.validate_tokens`` core: a non-``per_symbol`` insight_type whose referenced
strategy bodies use a ``per_symbol`` variable is rejected at create/update with 422.
"""

import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, TypeVar

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from portfolio_dash.api import insight_service
from portfolio_dash.api.deps import get_conn, get_now, get_reporting
from portfolio_dash.api.errors import error_body
from portfolio_dash.api.routers.scheduler import get_scheduler
from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.llm_insight import evaluations_store as es
from portfolio_dash.llm_insight import insights_store as istore
from portfolio_dash.llm_insight import official_templates
from portfolio_dash.llm_insight import variables as V
from portfolio_dash.scheduler.jobs import (
    bind_insight_schedule,
    insight_job_id,
    latest_run_unfinished,
    run_insight_func,
    start_insight_run,
    unbind_insight_schedule,
)
from portfolio_dash.scheduler.runtime import remove_job, reschedule_job
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.wire import decimal_str

# Insight-type schedule bindings are Asia/Taipei by definition (spec 4.2 —
# ``bind_insight_schedule``'s default); the live-trigger mount uses the same tz.
_INSIGHT_TZ = "Asia/Taipei"

router = APIRouter()

_MAX_RUNS_LIMIT = 500

# spec 07 §7.0: ``/api/insight-tasks/*`` is a FULL alias of ``/api/insight-types/*`` —
# both paths reach the SAME handler (one resource, no logic duplication). The old
# ``insight_types`` UI name maps to the new ``insight task`` name; the data table is NOT
# renamed (only the route + UI text). The decorators below register each insight-type
# resource route under BOTH path prefixes by giving the decorator a tuple of paths.
_ALIAS_PREFIXES = ("/insight-types", "/insight-tasks")

_F = TypeVar("_F", bound=Callable[..., Any])


def _dual(method: str, suffix: str) -> Callable[[_F], _F]:
    """Register one handler under both the ``/insight-types`` and ``/insight-tasks`` prefixes.

    ``suffix`` is the path AFTER the resource prefix (e.g. ``""`` for the collection,
    ``"/{insight_type_id}/runs"`` for a sub-resource). The handler is added once per
    prefix via ``router.add_api_route`` — the SAME function object both times, so there is
    no logic duplication and the two paths are a true alias of one resource (§7.0).
    """

    def decorator(fn: _F) -> _F:
        verb = method.lower()
        for prefix in _ALIAS_PREFIXES:
            router.add_api_route(f"{prefix}{suffix}", fn, methods=[verb])
        return fn

    return decorator


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
    universe: dict[str, Any] | list[Any] | str | None = None
    alert_rules: dict[str, Any] | list[Any] | str | None = None  # 'all' | [rule_ids]
    enabled: bool | None = None  # None -> defaulted by scope (on_alert -> False, R7)
    horizon_days: int = 5  # task-default prediction horizon (spec 04.10)
    eval_prompt: str | None = None  # optional custom self-evaluation prompt (spec 04.10)


class ScheduleIn(BaseModel):
    cron: str


class ActiveCalibrationIn(BaseModel):
    version: int | None


class EvolutionConfigIn(BaseModel):
    auto_promote: bool
    shadow_batches: int
    min_samples: int
    max_shadows: int
    gap_alert_pp: str  # percentage-points Decimal STRING (never float)
    # spec 04.10 new knobs (defaulted so existing callers stay back-compatible).
    defer_limit_days: int = 5
    horizon_basis: str = "trading_days"
    shadow_on_alert: bool = False


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
        "horizon_days": it.horizon_days,
        "eval_prompt": it.eval_prompt,
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


class FromTemplateIn(BaseModel):
    name: str  # the official template's name (official_templates.STRATEGY_TEMPLATES)


@router.post("/strategy-prompts/from-template")
def create_strategy_from_template(
    payload: FromTemplateIn,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    """Copy an official library strategy into the user's editable set (2026-07-05 program).

    The copy is the user's own row (editing it never touches the library). A taken name
    gets an「（官方vX）」suffix so re-adding after customization is always possible.
    """
    cs.ensure_seeded(conn)
    tpl = next(
        (t for t in official_templates.STRATEGY_TEMPLATES if t["name"] == payload.name),
        None,
    )
    if tpl is None:
        return JSONResponse(
            status_code=404,
            content=error_body("not_found", f"官方模板庫沒有：{payload.name}"),
        )
    taken = {s.name for s in cs.list_strategies(conn)}
    name = tpl["name"]
    if name in taken:
        name = f"{tpl['name']}（官方{tpl['version']}）"
        n = 2
        while name in taken:
            name = f"{tpl['name']}（官方{tpl['version']}·{n}）"
            n += 1
    sp = cs.create_strategy(conn, name=name, body=tpl["body"], now=now)
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


@_dual("GET", "")
def list_insight_types(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict[str, Any]]:
    cs.ensure_seeded(conn)
    return [_insight_type_wire(conn, it) for it in cs.list_insight_types(conn)]


@_dual("POST", "")
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
        horizon_days=payload.horizon_days, eval_prompt=payload.eval_prompt,
        now=now,
    )
    cs.set_strategies(conn, it.id, [(sid, pos) for pos, sid in enumerate(payload.strategy_ids)])
    return _insight_type_wire(conn, it)


@_dual("PUT", "/{insight_type_id}")
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
        horizon_days=payload.horizon_days, eval_prompt=payload.eval_prompt,
        now=now,
    )
    assert it is not None  # existence checked above
    cs.set_strategies(
        conn, insight_type_id, [(sid, pos) for pos, sid in enumerate(payload.strategy_ids)]
    )
    return _insight_type_wire(conn, it)


@_dual("DELETE", "/{insight_type_id}")
def delete_insight_type(
    insight_type_id: int,
    request: Request,
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
    # H1 fix: also drop the LIVE trigger — a deleted task's cron must stop firing now,
    # not at the next restart (no-op when the scheduler is absent, e.g. tests).
    remove_job(get_scheduler(request), insight_job_id(insight_type_id))
    return _insight_type_wire(conn, it)


# --- one-click official pack (usability decision ①, 2026-07-05) ----------------


@_dual("POST", "/official-pack")
def enable_official_pack(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """Create every official task preset in one click (strategy + knobs + schedule).

    Idempotent: a non-archived insight task carrying a preset's ``preset_key`` (the
    provenance stamp, M3 fix) — or, for installs that predate the column, its exact
    name — is skipped (a second click reports it under ``skipped``), so a RENAMED
    official task is never re-created (no double cron, no double cost). A same-name
    strategy is REUSED — the user's customized copy wins over the library body; only
    a missing strategy is created from the template.
    """
    cs.ensure_seeded(conn)
    live_types = cs.list_insight_types(conn)  # non-archived only
    existing_names = {t.name for t in live_types}
    existing_keys = {t.preset_key for t in live_types if t.preset_key}
    strategies_by_name = {s.name: s for s in cs.list_strategies(conn) if not s.archived}
    created: list[dict[str, Any]] = []
    skipped: list[str] = []
    templates_by_name = {t["name"]: t for t in official_templates.STRATEGY_TEMPLATES}
    scheduler = get_scheduler(request)
    for preset in official_templates.TASK_PRESETS:
        if preset["preset_key"] in existing_keys or preset["name"] in existing_names:
            skipped.append(preset["name"])
            continue
        tpl = templates_by_name[preset["strategy"]]
        sp = strategies_by_name.get(tpl["name"])
        strategy_reused = sp is not None
        if sp is None:
            sp = cs.create_strategy(conn, name=tpl["name"], body=tpl["body"], now=now)
            strategies_by_name[sp.name] = sp
        it = cs.create_insight_type(
            conn, name=preset["name"], scope=preset["scope"],
            use_system_prompt=preset["use_system_prompt"],
            self_correct=preset["self_correct"],
            universe=None, alert_rules=None, enabled=True,
            horizon_days=preset["horizon_days"], eval_prompt=None,
            preset_key=preset["preset_key"], now=now,
        )
        cs.set_strategies(conn, it.id, [(sp.id, 0)])
        job_id = bind_insight_schedule(conn, it.id, cron=preset["suggested_cron"])
        cs.set_job_id(conn, it.id, job_id)
        # H1 fix: mount the live trigger now (was: only loaded at restart).
        reschedule_job(
            scheduler, job_id, cron=preset["suggested_cron"], tz=_INSIGHT_TZ, enabled=True
        )
        created.append({
            "id": it.id, "name": it.name, "cron": preset["suggested_cron"],
            "strategy": sp.name, "strategy_reused": strategy_reused,
        })
    return {"created": created, "skipped": skipped}


# --- schedule mount (spec 4.2) ------------------------------------------------


@_dual("POST", "/{insight_type_id}/schedule")
def mount_schedule(
    insight_type_id: int,
    payload: ScheduleIn,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    """Bind/update the insight_type's schedule (kind=insight). on_alert combos reject (400).

    on_alert insight_types are event-triggered (spec 03), not scheduled. Persists the
    binding AND mounts/updates the LIVE APScheduler trigger immediately (H1 fix — the
    old behavior loaded new bindings only at restart, so a fresh schedule never fired).
    The cron is validated BEFORE any write (an invalid stored cron would crash the
    scheduler build at the next startup).
    """
    cs.ensure_seeded(conn)
    it = cs.get_insight_type(conn, insight_type_id)
    if it is None:
        return JSONResponse(
            status_code=404,
            content=error_body("not_found", f"未知洞察組合：{insight_type_id}"),
        )
    if it.scope == "on_alert":
        return JSONResponse(
            status_code=400,
            content=error_body(
                "validation_error", "on_alert 洞察組合由預警事件觸發，不可排程",
                field="cron",
            ),
        )
    try:
        CronTrigger.from_crontab(payload.cron, timezone=_INSIGHT_TZ)
    except Exception as exc:  # noqa: BLE001 — surface any builder failure as 400
        return JSONResponse(
            status_code=400,
            content=error_body("invalid_cron", f"cron 表達式無效：{exc}", field="cron"),
        )
    job_id = bind_insight_schedule(conn, insight_type_id, cron=payload.cron)
    cs.set_job_id(conn, insight_type_id, job_id)  # mirror onto the insight_type row
    reschedule_job(
        get_scheduler(request), job_id, cron=payload.cron, tz=_INSIGHT_TZ, enabled=True
    )
    return {"job_id": job_id}


@_dual("DELETE", "/{insight_type_id}/schedule")
def unmount_schedule(
    insight_type_id: int,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    cs.ensure_seeded(conn)
    if cs.get_insight_type(conn, insight_type_id) is None:
        return JSONResponse(
            status_code=404,
            content=error_body("not_found", f"未知洞察組合：{insight_type_id}"),
        )
    unbind_insight_schedule(conn, insight_type_id)
    cs.set_job_id(conn, insight_type_id, None)
    # H1 fix: drop the live trigger too (no-op when the scheduler is absent).
    remove_job(get_scheduler(request), insight_job_id(insight_type_id))
    return {"job_id": None}


# --- active-calibration selector (spec 4.6) -----------------------------------


@_dual("PUT", "/{insight_type_id}/active-calibration")
def set_active_calibration(
    insight_type_id: int,
    payload: ActiveCalibrationIn,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    """Manually select (or clear with null) the active calibration version.

    A non-null version must exist (non-archived) for this insight_type, else 400.
    """
    cs.ensure_seeded(conn)
    if cs.get_insight_type(conn, insight_type_id) is None:
        return JSONResponse(
            status_code=404,
            content=error_body("not_found", f"未知洞察組合：{insight_type_id}"),
        )
    if payload.version is not None:
        existing = {
            c.version for c in cs.list_calibrations(conn, insight_type_id)
        }
        if payload.version not in existing:
            return JSONResponse(
                status_code=400,
                content=error_body(
                    "validation_error",
                    f"該洞察組合無校正版本 {payload.version}",
                    field="version",
                ),
            )
    cs.set_active_calibration(conn, insight_type_id, payload.version)
    return {"id": insight_type_id, "active_calibration_version": payload.version}


# --- calibrations (spec 4.7) --------------------------------------------------


@router.get("/calibrations")
def list_calibrations(
    insight_type: int,
    include_archived: bool = False,
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict[str, Any]]:
    cs.ensure_seeded(conn)
    rows = cs.list_calibrations(conn, insight_type, include_archived=include_archived)
    return [c.model_dump() for c in rows]


@router.post("/calibrations/{calibration_id}/archive")
def archive_calibration(
    calibration_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    """Soft-delete a calibration version; clears the active selection if it was active."""
    cs.ensure_seeded(conn)
    cal = cs.archive_calibration(conn, calibration_id)
    if cal is None:
        return JSONResponse(
            status_code=404,
            content=error_body("not_found", f"未知校正版本：{calibration_id}"),
        )
    return cal.model_dump()


@router.get("/calibrations/{calibration_id}/samples")
def calibration_samples(
    calibration_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict[str, Any]]:
    """The miss-evaluation samples that drove a calibration version (spec 4.7).

    Looks up the calibration's insight_type + version, then returns its miss samples from
    ``insight_evaluations`` (04c). An unknown id → ``[]`` (the contract shape the frontend
    version manager consumes).
    """
    cs.ensure_seeded(conn)
    es.ensure_tables(conn)
    cal = cs.get_calibration(conn, calibration_id)
    if cal is None:
        return []
    return es.miss_samples_for_version(
        conn, insight_type_id=cal.insight_type_id, version=cal.version
    )


# --- ai-score battle record (spec 4.7) ----------------------------------------


@router.get("/ai-score")
def get_ai_score(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """The AI battle-record table: ``{totals, by_combo[], calibration_bins[], rows[]}``.

    Active (non-shadow) scored rows drive the displayed totals/by_combo; shadow rows are
    kept in ``rows`` for the promotion view. Empty DB → zeroed/[] (CSV export is a frontend
    concern over this payload). WPE (2026-07-07): the previously-unbounded ``rows`` pages
    via ``limit``/``offset`` (+ ``rows_total_count``); the aggregates stay whole-set.
    """
    es.ensure_tables(conn)
    cs.ensure_seeded(conn)
    return es.ai_score(
        conn, exclude_type_ids=cs.archived_type_ids(conn),
        rows_limit=limit, rows_offset=offset,
    )


# --- evolution-config (spec 4.6) ----------------------------------------------


@router.get("/evolution-config")
def get_evolution_config(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    cs.ensure_seeded(conn)
    return cs.get_evolution_config(conn)


@router.put("/evolution-config")
def put_evolution_config(
    payload: EvolutionConfigIn,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    """Upsert the evolution knobs.

    ``gap_alert_pp`` must parse as a Decimal and ``horizon_basis`` must be one of
    :data:`composer_store.HORIZON_BASIS_VALUES` (else 400).
    """
    cs.ensure_seeded(conn)
    try:
        gap = Decimal(payload.gap_alert_pp)
    except (InvalidOperation, ValueError):
        return JSONResponse(
            status_code=400,
            content=error_body(
                "validation_error", f"gap_alert_pp 非有效數值：{payload.gap_alert_pp}",
                field="gap_alert_pp",
            ),
        )
    if payload.horizon_basis not in cs.HORIZON_BASIS_VALUES:
        return JSONResponse(
            status_code=400,
            content=error_body(
                "validation_error",
                f"horizon_basis 非有效值：{payload.horizon_basis}",
                field="horizon_basis",
            ),
        )
    return cs.set_evolution_config(
        conn,
        auto_promote=payload.auto_promote,
        shadow_batches=payload.shadow_batches,
        min_samples=payload.min_samples,
        max_shadows=payload.max_shadows,
        gap_alert_pp=gap,
        defer_limit_days=payload.defer_limit_days,
        horizon_basis=payload.horizon_basis,
        shadow_on_alert=payload.shadow_on_alert,
    )


# --- manual run (spec 4.2 / 4.10 — async 202 + poll) --------------------------


@_dual("POST", "/{insight_type_id}/run")
def run_insight_now(
    insight_type_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    """Fire one insight generation now (async 202; the bg thread opens its own session).

    Inserts a 'running' ``job_runs`` row synchronously (returning its id for polling), then
    dispatches the registered insight runner in a daemon thread. Mirrors spec-15 ``/run``:
    progress is polled via ``GET /api/insight-types/{id}/runs`` (running/ok/error/skipped).
    A disabled or archived task rejects with 409 (H2 fix, decision Q2a — ``get_insight_type``
    returns archived rows, which is how an archived re-run used to slip through).
    """
    cs.ensure_seeded(conn)
    it = cs.get_insight_type(conn, insight_type_id)
    if it is None:
        return JSONResponse(
            status_code=404,
            content=error_body("not_found", f"未知洞察組合：{insight_type_id}"),
        )
    if it.archived:
        return JSONResponse(
            status_code=409,
            content=error_body("task_archived", "任務已刪除，無法執行"),
        )
    if not it.enabled:
        return JSONResponse(
            status_code=409,
            content=error_body("task_disabled", "任務已停用，請先啟用再執行"),
        )
    if latest_run_unfinished(conn, insight_job_id(insight_type_id)):
        return JSONResponse(
            status_code=409,
            content=error_body("already_running", f"洞察組合 {insight_type_id} 執行中"),
        )
    run_id = start_insight_run(conn, insight_type_id, now=now)
    thread = threading.Thread(
        target=run_insight_func,
        kwargs={"insight_type_id": insight_type_id, "now": now, "run_id": run_id},
        daemon=True,
    )
    thread.start()
    return JSONResponse(
        status_code=202, content={"run_id": run_id, "insight_type_id": insight_type_id}
    )


def _insight_run_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "insight_type_id": int(row["payload"]) if row["payload"] is not None else None,
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "status": row["status"],
        "detail": row["detail"],
        "reason": row["reason"],
        "cost_usd": row["cost_usd"],
    }


@_dual("GET", "/{insight_type_id}/runs")
def list_insight_runs(
    insight_type_id: int,
    limit: int = 50,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    """Run history for one insight_type (job_runs filtered by its kind=insight job_id).

    Newest-first; each row carries the 3-state status (running=null finished_at / ok /
    error / skipped) + the skip ``reason`` enum (R1..R8) for the polling UI (spec 04.10).
    Shadow runs (``is_shadow=1``, Loop 4) are internal and EXCLUDED here (spec 04 fix #3).
    """
    if limit > _MAX_RUNS_LIMIT:
        return JSONResponse(
            status_code=400,
            content=error_body(
                "validation_error", f"limit 不可超過 {_MAX_RUNS_LIMIT}", field="limit"
            ),
        )
    rows = conn.execute(
        "SELECT id, payload, started_at, finished_at, status, detail, reason, cost_usd "
        "FROM job_runs WHERE job_id = ? AND is_shadow = 0 ORDER BY id DESC LIMIT ?",
        (insight_job_id(insight_type_id), limit),
    ).fetchall()
    return {"rows": [_insight_run_row(r) for r in rows]}


# --- spec 07 §7.1: pipeline-hub task status -----------------------------------


@router.get("/insight-tasks/status")
def insight_tasks_status(
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
    reporting: Currency = Depends(get_reporting),
) -> dict[str, Any]:
    """The converged single-source-of-truth task-status payload (spec 07 §7.1).

    Read-only observability: the health bar + one pipeline card per task (five node states
    + aggregate level). The fact-gathering lives in ``insight_service`` (it may read
    pricing/portfolio) and feeds the PURE ``pipeline_status.derive_node_states``. No LLM is
    called here. Empty DB → ``tasks: []`` + an AI-off health bar.
    """
    return insight_service.build_status(conn, now=now, reporting=reporting)


# --- spec 07 §7.2: dry-run preflight (shared 04 gate + 06 preview, zero-cost) --


@router.post("/insight-tasks/{insight_type_id}/preflight")
def insight_task_preflight(
    insight_type_id: int,
    draft: insight_service.PreflightDraft | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
    reporting: Currency = Depends(get_reporting),
) -> Any:
    """Dry-run preflight for a task (or an unsaved draft via ``draft`` body) — spec 07 §7.2.

    Calls the SAME runtime gate as execution (``gating.evaluate_gates`` via the SAME
    ``generate._gate_context`` builder), wraps it with G0/G1/G7, computes the verdict, and
    attaches the 06 assembled preview (layers + est_tokens + est_cost). NEVER calls the LLM
    and NEVER writes a job_runs row. An unknown saved id with no draft → 404.
    """
    cs.ensure_seeded(conn)
    if draft is not None and not draft.model_fields_set:
        # An empty JSON body ({}) means "preflight the saved task" — otherwise the
        # all-defaults draft (strategy_ids=[]) silently shadows the saved combo and
        # every gate reports a bogus R3 "no live templates".
        draft = None
    payload = insight_service.build_preflight(
        conn, insight_type_id, now=now, reporting=reporting, draft=draft,
    )
    if payload is None:
        return JSONResponse(
            status_code=404,
            content=error_body("not_found", f"未知洞察任務：{insight_type_id}"),
        )
    return payload


# --- spec 07 §7.3: diagnose ("why didn't it run") -----------------------------


@router.get("/insight-tasks/{insight_type_id}/diagnose")
def insight_task_diagnose(
    insight_type_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
    reporting: Currency = Depends(get_reporting),
) -> Any:
    """Diagnose why a task did not run (spec 07 §7.3) — read-only, zero-cost.

    Returns the SAME shared-gate gates as preflight (no preview) + ``first_blocker`` (the
    first failing gate id, or null) + ``recent_skips`` (the last 5 skipped runs with the
    04b reason enum). Unknown id → 404.
    """
    cs.ensure_seeded(conn)
    payload = insight_service.build_diagnose(
        conn, insight_type_id, now=now, reporting=reporting,
    )
    if payload is None:
        return JSONResponse(
            status_code=404,
            content=error_body("not_found", f"未知洞察任務：{insight_type_id}"),
        )
    return payload


# --- stored cards list (spec 4.10) --------------------------------------------


def _card_wire(rec: istore.InsightRecord) -> dict[str, Any]:
    pred = rec.card.prediction
    return {
        "id": rec.id,
        "insight_type_id": rec.insight_type_id,
        "symbol": rec.symbol,
        "is_shadow": rec.is_shadow,
        "calibration_version": rec.calibration_version,
        "title": rec.card.title,
        "summary": rec.card.summary,
        "body_md": rec.card.body_md,
        "tags": rec.card.tags,
        "confidence": rec.card.confidence,
        "prediction": (
            {
                "metric": pred.metric,
                "direction": pred.direction,
                "target_pct": (
                    None if pred.target_pct is None else decimal_str(pred.target_pct)
                ),
                "horizon_days": pred.horizon_days,
            }
            if pred is not None
            else None
        ),
        "horizon_days": rec.horizon_days,
        "due_at": rec.due_at,
        "model": rec.model,
        "cost_usd": rec.cost_usd,
        "tokens_in": rec.tokens_in,
        "tokens_out": rec.tokens_out,
        "created_at": rec.created_at,
    }


@router.get("/insights")
def list_insights(
    insight_type: int | None = None,
    symbol: str | None = None,
    scope: str | None = None,
    group: str | None = None,
    history_limit: int = Query(5, ge=1, le=20),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    """List stored insight cards (newest first) — paginated (WPE, 2026-07-07).

    Flat shape: ``{rows, total_count, limit, offset}`` filtered by ``insight_type`` /
    ``symbol`` / ``scope`` ('portfolio' = portfolio + per-market cards; 'symbol' =
    per-symbol health cards — the market-code-in-symbol wire convention is encoded once
    in the store). Grouped shape (``group=symbol``, the 持倉健診 treatment): pagination
    runs over SYMBOLS — ``{groups: [{symbol, total, cards<=history_limit}], total_count,
    limit, offset, history_limit}``. Grouping is server-side because a client slice of a
    flat feed cannot know symbol boundaries; the cards keep coming from the same
    ``_card_wire`` serializer (no client-computed numbers either way).

    Archived (deleted) tasks' cards are hidden — history stays in the table (spec 4.1)
    but stops surfacing. Empty DB → empty rows/groups. Money/target_pct is a Decimal
    STRING (the frontend never computes).
    """
    istore.ensure_tables(conn)
    cs.ensure_seeded(conn)
    if scope not in (None, "portfolio", "symbol"):
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", f"scope 非有效值：{scope}", field="scope"))
    if group not in (None, "symbol"):
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", f"group 非有效值：{group}", field="group"))
    excluded = cs.archived_type_ids(conn)
    if group == "symbol":
        groups, total_symbols = istore.list_symbol_groups(
            conn, history_limit=history_limit, limit=limit, offset=offset,
            exclude_type_ids=excluded,
        )
        return {
            "groups": [
                {"symbol": sym, "total": total, "cards": [_card_wire(c) for c in cards]}
                for sym, total, cards in groups
            ],
            "total_count": total_symbols,
            "limit": limit,
            "offset": offset,
            "history_limit": history_limit,
        }
    rows = [
        _card_wire(rec)
        for rec in istore.list_cards(
            conn, insight_type_id=insight_type, symbol=symbol,
            exclude_type_ids=excluded, scope=scope, limit=limit, offset=offset,
        )
    ]
    total = istore.count_cards(
        conn, insight_type_id=insight_type, symbol=symbol,
        exclude_type_ids=excluded, scope=scope,
    )
    return {"rows": rows, "total_count": total, "limit": limit, "offset": offset}
