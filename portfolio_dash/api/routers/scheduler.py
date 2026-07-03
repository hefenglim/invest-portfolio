"""Scheduler management API (spec 15): list jobs, edit schedule, run now, run history.

Thin router (decision B) over ``scheduler/jobs.py`` (registry, schedule_config/job_runs)
and the ``runtime.reschedule_job`` dynamic seam. The APScheduler singleton lives in
``app.state.scheduler`` (``None`` when ``PD_DISABLE_SCHEDULER=1``, e.g. tests) — every
route degrades gracefully when it is absent. Money/cost is a Decimal **string**; the
router computes no business numbers.
"""

import sqlite3
import threading
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.base import BaseScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from portfolio_dash.api.deps import get_conn, get_now
from portfolio_dash.api.errors import error_body
from portfolio_dash.scheduler.jobs import (
    JOBS,
    ensure_job_rows,
    latest_run_unfinished,
    run_job_func,
    start_job_run,
)
from portfolio_dash.scheduler.runtime import reschedule_job

router = APIRouter()

_MAX_RUNS_LIMIT = 500


def get_scheduler(request: Request) -> BaseScheduler | None:
    """The live APScheduler singleton, or None (no lifespan / PD_DISABLE_SCHEDULER=1)."""
    sched = getattr(request.app.state, "scheduler", None)
    return sched if isinstance(sched, BaseScheduler) else None


def _desc(job_id: str) -> str:
    """Registry description by id; fall back to the id for non-registry (future) jobs."""
    for spec in JOBS:
        if spec.id == job_id:
            return spec.description
    return job_id


def _duration_s(started_at: str | None, finished_at: str | None) -> float | None:
    if not started_at or not finished_at:
        return None
    delta = datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)
    return delta.total_seconds()


def _last_run(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    """Latest job_runs row mapped to the §15.1 ``last`` shape, or None when no runs."""
    row = conn.execute(
        "SELECT started_at, finished_at, status, detail FROM job_runs "
        "WHERE job_id = ? ORDER BY id DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "status": row["status"],
        "at": row["started_at"],
        "detail": row["detail"],
        "duration_s": _duration_s(row["started_at"], row["finished_at"]),
    }


def _next_fire(scheduler: BaseScheduler | None, job_id: str) -> str | None:
    """Next fire time from the live scheduler, or None (no scheduler / disabled job)."""
    if scheduler is None:
        return None
    job = scheduler.get_job(job_id)
    if job is None or job.next_run_time is None:
        return None
    return str(job.next_run_time.isoformat())


def _job_element(
    conn: sqlite3.Connection, row: sqlite3.Row, scheduler: BaseScheduler | None
) -> dict[str, Any]:
    job_id = row["job_id"]
    return {
        "id": job_id,
        "desc": _desc(job_id),
        "cron": row["cron"],
        "tz": row["timezone"],
        "enabled": bool(row["enabled"]),
        "last": _last_run(conn, job_id),
        "next": _next_fire(scheduler, job_id),
    }


@router.get("/scheduler/jobs")
def list_jobs(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """§15.1 — schedule config + latest run + next fire for every scheduled job."""
    ensure_job_rows(conn)  # idempotent seed so registry jobs always have a config row
    scheduler = get_scheduler(request)
    rows = conn.execute(
        "SELECT job_id, enabled, cron, timezone FROM schedule_config ORDER BY job_id"
    ).fetchall()
    return {"jobs": [_job_element(conn, r, scheduler) for r in rows]}


class _PutBody(BaseModel):
    cron: str | None = None
    tz: str | None = None
    enabled: bool | None = None


@router.put("/scheduler/jobs/{job_id}")
def update_job(
    job_id: str,
    body: _PutBody,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    """§15.2 — edit cron/tz/enabled (subset merge), validate, persist + live reschedule."""
    ensure_job_rows(conn)
    current = conn.execute(
        "SELECT job_id, enabled, cron, timezone FROM schedule_config WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    if current is None:
        raise HTTPException(status_code=404, detail=f"{job_id} 不存在")

    cron = body.cron if body.cron is not None else current["cron"]
    tz = body.tz if body.tz is not None else current["timezone"]
    enabled = body.enabled if body.enabled is not None else bool(current["enabled"])

    # Validate tz then cron SEPARATELY so the 400 `field` points at the real offender
    # (not "tz" merely because tz was present in the body). Any failure → NO DB write.
    try:
        ZoneInfo(tz)
    except Exception as exc:  # noqa: BLE001 — unknown/invalid timezone
        return JSONResponse(
            status_code=400,
            content=error_body("invalid_cron", f"時區無效：{exc}", field="tz"),
        )
    try:
        CronTrigger.from_crontab(cron, timezone=tz)
    except Exception as exc:  # noqa: BLE001 — surface any builder failure as 400
        return JSONResponse(
            status_code=400,
            content=error_body("invalid_cron", f"cron 表達式無效：{exc}", field="cron"),
        )

    conn.execute(
        "UPDATE schedule_config SET cron = ?, timezone = ?, enabled = ? WHERE job_id = ?",
        (cron, tz, 1 if enabled else 0, job_id),
    )
    conn.commit()
    reschedule_job(get_scheduler(request), job_id, cron=cron, tz=tz, enabled=enabled)

    updated = conn.execute(
        "SELECT job_id, enabled, cron, timezone FROM schedule_config WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    return _job_element(conn, updated, get_scheduler(request))


@router.post("/scheduler/jobs/{job_id}/run")
def run_job_now(
    job_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    """§15.3 — fire a job once now (async 202; background thread opens its own session)."""
    ensure_job_rows(conn)
    known = conn.execute(
        "SELECT 1 FROM schedule_config WHERE job_id = ?", (job_id,)
    ).fetchone()
    if known is None:
        raise HTTPException(status_code=404, detail=f"{job_id} 不存在")
    if latest_run_unfinished(conn, job_id):
        return JSONResponse(
            status_code=409,
            content=error_body("already_running", f"{job_id} 執行中"),
        )
    run_id = start_job_run(conn, job_id, now=now)
    thread = threading.Thread(
        target=run_job_func, kwargs={"job_id": job_id, "now": now}, daemon=True
    )
    thread.start()
    return JSONResponse(status_code=202, content={"run_id": run_id, "job_id": job_id})


def _run_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "job_id": row["job_id"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "status": row["status"],
        "detail": row["detail"],
        "duration_s": _duration_s(row["started_at"], row["finished_at"]),
        "cost_usd": row["cost_usd"],
    }


@router.get("/scheduler/runs")
def list_runs(
    conn: sqlite3.Connection = Depends(get_conn),
    job_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> Any:
    """§15.4 — run history (settings page + 15.3 completion polling), started_at DESC.

    Excludes ``export:*`` rows (2026-07-03, human decision): exports are user
    actions recorded by the 系統操作記錄, not scheduler work — legacy rows written
    before this change stay in the table but out of this view.
    """
    if limit > _MAX_RUNS_LIMIT:
        return JSONResponse(
            status_code=400,
            content=error_body("validation_error", f"limit 不可超過 {_MAX_RUNS_LIMIT}",
                               field="limit"),
        )
    where = "WHERE job_id NOT LIKE 'export:%'"
    params: tuple[Any, ...] = ()
    if job_id is not None:
        where += " AND job_id = ?"
        params = (job_id,)
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM job_runs {where}", params
    ).fetchone()["n"]
    rows = conn.execute(
        f"SELECT id, job_id, started_at, finished_at, status, detail, cost_usd "
        f"FROM job_runs {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    return {"rows": [_run_row(r) for r in rows], "total_count": total}
