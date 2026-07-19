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
from decimal import Decimal, InvalidOperation
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
    running_job_ids,
    running_progress,
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


# FU-D46 — honest LLM-cost attribution per job kind. Each LLM-bearing STATIC job maps to
# the exact ``llm_usage.agent`` string(s) its run writes (they are 1:1 — no other code path
# logs those agents), so summing usage rows inside the run's [started_at, finished_at]
# window attributes that run's spend. Deliberate omissions:
#   * ``insight:*`` runs — their runner already records the EXACT per-run spend on the
#     ``job_runs.cost_usd`` column (served via the run-row path below; no window sum).
#   * ``alert_scan`` — its on_alert dispatch spawns ``insight:*`` runs which record their
#     own cost rows; window-summing ``insight_generate`` here would double-count them.
#   * everything else — no LLM involvement, no cost block. Never guessed.
_LLM_JOB_AGENTS: dict[str, list[str]] = {
    "news_daily": ["news_organize"],
    "digest_daily": ["digest_note"],
    "digest_weekly": ["digest_note"],
    "evaluate_insights": ["master_score"],
    "generate_calibrations": ["master_calibrate", "master_validate"],
}


def _usage_window_cost(
    conn: sqlite3.Connection, agents: list[str], started_at: str, finished_at: str
) -> dict[str, Any] | None:
    """Sum ``llm_usage`` calls/tokens/cost for *agents* within the run window, or None.

    Aggregation of already-recorded audit numbers (same altitude as ``_duration_s``) —
    the router computes no business numbers. Degrades to None (no cost block) on any
    unparseable timestamp / missing table / malformed cost string, and when the window
    saw ZERO calls (an LLM-capable run that made no calls honestly serves no block).
    Cost is summed as Decimal and serialized as a string.
    """
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(finished_at)
    except (TypeError, ValueError):
        return None
    placeholders = ",".join("?" for _ in agents)
    try:
        rows = conn.execute(
            f"SELECT ts, input_tokens, output_tokens, cost FROM llm_usage "
            f"WHERE agent IN ({placeholders})",
            tuple(agents),
        ).fetchall()
    except sqlite3.Error:  # table absent (partial bootstrap) — no block, never a 500
        return None
    calls = tokens_in = tokens_out = 0
    total = Decimal("0")
    for row in rows:
        try:
            ts = datetime.fromisoformat(row["ts"])
            in_window = start <= ts <= end
        except (TypeError, ValueError):  # unparseable / naive-vs-aware mix — skip row
            continue
        if not in_window:
            continue
        try:
            total += Decimal(row["cost"])
        except (InvalidOperation, TypeError):
            continue
        calls += 1
        tokens_in += int(row["input_tokens"] or 0)
        tokens_out += int(row["output_tokens"] or 0)
    if calls == 0:
        return None
    return {
        "cost_usd": str(total),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "calls": calls,
        "source": "usage_window",
    }


def _cost_block(conn: sqlite3.Connection, job_id: str, row: Any) -> dict[str, Any] | None:
    """The FU-D46 ``last_run.cost`` block for a completed run, or None (omitted).

    ``insight:*`` → the run row's own exact ``cost_usd`` (written by the insight runner;
    tokens are not recorded per-run, so those fields are null). Mapped static LLM jobs →
    the agent/window sum. Zero-cost run rows (skipped / no calls) serve no block.
    """
    if job_id.startswith("insight:"):
        cost_usd = row["cost_usd"] if "cost_usd" in row.keys() else None
        if cost_usd is None:
            return None
        try:
            if Decimal(cost_usd) == 0:
                return None
        except (InvalidOperation, TypeError):
            return None
        return {
            "cost_usd": cost_usd,
            "tokens_in": None,
            "tokens_out": None,
            "calls": None,
            "source": "run_row",
        }
    agents = _LLM_JOB_AGENTS.get(job_id)
    if not agents or not row["finished_at"]:
        return None
    return _usage_window_cost(conn, agents, row["started_at"], row["finished_at"])


def _status_last_run(conn: sqlite3.Connection, job_id: str, row: Any) -> dict[str, Any]:
    """Map a COMPLETED job_runs row to the FU-D36/D46 ``last_run`` shape.

    ``ok`` is the boolean the chip keys off (green 成功 / red 失敗); ``status`` is passed
    through raw so the frontend can render the non-terminal ``skipped`` (略過) distinctly.
    FU-D46 adds ``duration_seconds`` (server-computed) and the honest ``cost`` block
    (null unless the run is LLM-bearing AND its spend is attributable — see
    ``_LLM_JOB_AGENTS`` / ``_cost_block``).
    """
    return {
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "status": row["status"],
        "ok": row["status"] == "ok",
        "message": row["detail"] or "",
        "duration_seconds": _duration_s(row["started_at"], row["finished_at"]),
        "cost": _cost_block(conn, job_id, row),
    }


@router.get("/scheduler/status")
def job_status(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    """FU-D36/D46 — per-job live run status for the 排程中心 poll (needs 七).

    For every scheduled job → ``{running, queued, progress, last_run}``. ``running`` = the
    func is executing right now (the in-process registry). ``queued`` = a run row exists
    but is not yet marked running (the 已排入 window between enqueue and the worker picking
    it up). ``progress`` (FU-D46) = the running job's live stage message from the registry
    (null when idle/queued or the job has not reported yet). ``last_run`` = the latest
    COMPLETED run (finished_at set) so ok/message are always meaningful — with
    ``duration_seconds`` and the honest ``cost`` block (see ``_cost_block``); a job that
    never completed reports null. Shadow (Loop-4) and legacy ``export:*`` rows are
    excluded, matching the /runs view. Cheap by design — two windowed queries + a registry
    snapshot (+ a small usage-window sum only for the few LLM-kind jobs) — so the frontend
    polls it ONLY while something is active and stops when ``active`` goes false. The
    router derives; it computes no business numbers.
    """
    ensure_job_rows(conn)
    job_ids = [
        r["job_id"]
        for r in conn.execute("SELECT job_id FROM schedule_config ORDER BY job_id")
    ]
    # Latest row overall per job (finished or not) → detects an in-flight run (queued/running).
    latest_overall: dict[str, Any] = {
        r["job_id"]: r
        for r in conn.execute(
            "SELECT jr.job_id, jr.finished_at FROM job_runs jr "
            "JOIN (SELECT job_id, MAX(id) AS mid FROM job_runs "
            "      WHERE COALESCE(is_shadow, 0) = 0 AND job_id NOT LIKE 'export:%' "
            "      GROUP BY job_id) m ON jr.id = m.mid"
        )
    }
    # Latest COMPLETED row per job → the 上次結果 (ok / message) shown when the job is idle.
    latest_done: dict[str, Any] = {
        r["job_id"]: r
        for r in conn.execute(
            "SELECT jr.job_id, jr.started_at, jr.finished_at, jr.status, jr.detail, "
            "jr.cost_usd FROM job_runs jr "
            "JOIN (SELECT job_id, MAX(id) AS mid FROM job_runs "
            "      WHERE finished_at IS NOT NULL AND COALESCE(is_shadow, 0) = 0 "
            "      AND job_id NOT LIKE 'export:%' GROUP BY job_id) m ON jr.id = m.mid"
        )
    }
    running = running_job_ids()
    progress_by_job = running_progress()
    jobs: dict[str, Any] = {}
    active = False
    for jid in job_ids:
        overall = latest_overall.get(jid)
        unfinished = overall is not None and overall["finished_at"] is None
        is_running = jid in running
        is_queued = bool(unfinished and not is_running)
        if is_running or is_queued:
            active = True
        done = latest_done.get(jid)
        jobs[jid] = {
            "running": is_running,
            "queued": is_queued,
            "progress": progress_by_job.get(jid) if is_running else None,
            "last_run": _status_last_run(conn, jid, done) if done is not None else None,
        }
    return {"jobs": jobs, "active": active}


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
