"""/api/settlement/* — the settlement status, run reports, change trail and period closes.

PERF spec 2026-10-18 §15. A thin router over ``api/settlement_service.py`` (decision B: the
service orchestrates, the store computes nothing here): every number is a Decimal string,
every user-facing word is the server's. ``/periods*`` are package G's (wave 3).

- ``GET /settlement/status`` — the chip's read; never writes, never starts a run.
- ``GET /settlement/runs`` · ``/runs/{id}`` (404 when pruned) · ``/runs/{id}/diff`` ·
  ``/changes?state=pending|run:<id>`` — the kept reports and the trail, paged.
- ``POST /settlement/run`` ``{scope: "dirty"|"full"}`` → 202 ``{run_id}``; 409
  ``already_running`` / ``settle_off``. ``dirty`` is the prompt's confirmation (reason
  ``prompt``), ``full`` the 重新結算 button (reason ``button``).
- ``POST /settlement/cancel`` → 200; 409 ``not_running`` / ``cannot_cancel``.
- ``GET /settlement/periods?kind=M|Y&limit=`` — the month / year closes, newest first, the
  current period (live) included: ``{rows: PeriodClose[], total_count}``;
  ``/periods/{kind}/{period}`` — one close with its holdings and cash at its day; 404 for a
  period the ledger does not have. Both read ``api/snapshots.py`` (the rule for which close is
  served from the store and which live) and never write.

The two POSTs carry the action-log labels 「重新結算」 / 「取消重新結算」 (``api/action_log.py``).
"""

import re
import sqlite3
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from portfolio_dash.api import settlement_service as service
from portfolio_dash.api import snapshots
from portfolio_dash.api.deps import get_conn, get_now, get_reporting, request_actor
from portfolio_dash.api.errors import error_body
from portfolio_dash.shared.enums import Currency

router = APIRouter()

_RUNS_MAX = 100
_PERIODS_MAX = 120
_ROWS_MAX = 500
_STATE = re.compile(r"^(pending|run:[0-9]+)$")
_DIFF_FIELDS = frozenset({"total_value", "net_invested", "net_worth", "incomplete"})
_DIFF_KINDS = frozenset({"changed", "value_to_none", "none_to_value", "flag_flip",
                         "new_day", "dropped_day"})


def _bad(message: str, field: str) -> JSONResponse:
    return JSONResponse(status_code=400,
                        content=error_body("validation_error", message, field=field))


def _paging(limit: int, offset: int, maximum: int) -> JSONResponse | None:
    if not 1 <= limit <= maximum:
        return _bad(f"limit 必須介於 1 與 {maximum} 之間", "limit")
    if offset < 0:
        return _bad("offset 不可小於 0", "offset")
    return None


def _not_found(run_id: int) -> JSONResponse:
    return JSONResponse(status_code=404, content=error_body(
        "not_found", f"找不到重新結算紀錄 #{run_id}（可能已超過保留期限）"))


def _refused(exc: service.SettlementError) -> JSONResponse:
    return JSONResponse(status_code=409, content=error_body(exc.code, exc.message))


@router.get("/settlement/status")
def settlement_status(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn, scope="function"),
    now: datetime = Depends(get_now),
    reporting: Currency = Depends(get_reporting),
) -> dict[str, Any]:
    """The status chip and panel's read (spec §15). Reads only."""
    result = service.status(conn, viewer=request_actor(conn, request), now=now,
                            reporting=reporting)
    return result.model_dump(mode="json", by_alias=True)


@router.get("/settlement/runs")
def settlement_runs(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn, scope="function"),
    limit: int = 20,
    offset: int = 0,
    only_changes: bool = False,
) -> Any:
    """The kept run reports' summaries, newest first (``only_changes``: runs that settled at
    least one change — the panel's filter hides the nightly runs that settled nothing)."""
    refused = _paging(limit, offset, _RUNS_MAX)
    if refused is not None:
        return refused
    return service.runs_page(conn, viewer=request_actor(conn, request), limit=limit,
                             offset=offset, only_changes=only_changes)


@router.get("/settlement/runs/{run_id}")
def settlement_run(
    run_id: int,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn, scope="function"),
) -> Any:
    """One run's full report (spec §9.6) with its restatements; 404 when pruned."""
    body = service.run_report(conn, run_id, viewer=request_actor(conn, request))
    return _not_found(run_id) if body is None else body


@router.get("/settlement/runs/{run_id}/diff")
def settlement_run_diff(
    run_id: int,
    conn: sqlite3.Connection = Depends(get_conn, scope="function"),
    field: str | None = None,
    kind: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> Any:
    """The run's per-day changes (``settle_run_diff``), paged."""
    refused = _paging(limit, offset, _ROWS_MAX)
    if refused is not None:
        return refused
    if field is not None and field not in _DIFF_FIELDS:
        return _bad("field 必須是 total_value、net_invested、net_worth 或 incomplete", "field")
    if kind is not None and kind not in _DIFF_KINDS:
        return _bad("kind 不是可接受的變動類別", "kind")
    body = service.diff_rows(conn, run_id, field_name=field, kind=kind, limit=limit,
                             offset=offset)
    return _not_found(run_id) if body is None else body


@router.get("/settlement/changes")
def settlement_changes(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn, scope="function"),
    state: str = "pending",
    limit: int = 50,
    offset: int = 0,
) -> Any:
    """The change trail: the pending changes, or what one run claimed / settled / handed
    back — every record, paged, never truncated (spec §9.6)."""
    refused = _paging(limit, offset, _ROWS_MAX)
    if refused is not None:
        return refused
    if not _STATE.match(state):
        return _bad("state 必須是 pending 或 run:<編號>", "state")
    body = service.changes_page(conn, state=state, viewer=request_actor(conn, request),
                                limit=limit, offset=offset)
    if body is None:
        return _not_found(int(state.split(":", 1)[1]))
    return body


@router.get("/settlement/periods")
def settlement_periods(
    conn: sqlite3.Connection = Depends(get_conn, scope="function"),
    now: datetime = Depends(get_now),
    reporting: Currency = Depends(get_reporting),
    kind: str = "M",
    limit: int = 24,
) -> Any:
    """The period closes (KPI + activity), newest first — the current period live, the past
    ones settled or computed live (spec §11.4). ``total_count`` counts every period the ledger
    has."""
    if kind not in ("M", "Y"):
        return _bad("kind 必須是 M（月）或 Y（年）", "kind")
    refused = _paging(limit, 0, _PERIODS_MAX)
    if refused is not None:
        return refused
    return snapshots.period_rows(conn, kind="M" if kind == "M" else "Y", limit=limit,
                                 now=now, reporting=reporting)


@router.get("/settlement/periods/{kind}/{period}")
def settlement_period(
    kind: str,
    period: str,
    conn: sqlite3.Connection = Depends(get_conn, scope="function"),
    now: datetime = Depends(get_now),
    reporting: Currency = Depends(get_reporting),
) -> Any:
    """One close with its holdings and its cash at its day (spec §11.3)."""
    parsed = snapshots.parse_period(kind, period)
    if parsed is None:
        return _bad("期間必須是 M／YYYY-MM（月）或 Y／YYYY（年）", "period")
    body = snapshots.period_detail(conn, kind=parsed[0], period=parsed[1], now=now,
                                   reporting=reporting)
    if body is None:
        return JSONResponse(status_code=404, content=error_body(
            "not_found", f"沒有 {parsed[1]} 的期間資料：早於帳本第一筆紀錄，或尚未開始"))
    return body


class _RunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Literal["dirty", "full"]


@router.post("/settlement/run", status_code=202)
def settlement_run_start(
    body: _RunBody,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn, scope="function"),
    now: datetime = Depends(get_now),
) -> Any:
    """Start a run (spec §14.3 / §14.4): ``dirty`` = the prompt's 立即重新結算, ``full`` =
    重新結算（驗證並重建歷史）. 202 with the run id; in inline mode (tests, Windows
    development) the call returns after the run (plan P5)."""
    started_by = request_actor(conn, request)
    reason = "prompt" if body.scope == "dirty" else "button"
    try:
        started = service.start_run(scope=body.scope, reason=reason, started_by=started_by,
                                    now=now)
    except (service.SettlementOff, service.AlreadyRunning, service.Stopping) as exc:
        return _refused(exc)
    return JSONResponse(status_code=202, content={"run_id": started.run_id})


@router.post("/settlement/cancel")
def settlement_cancel(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn, scope="function"),
) -> Any:
    """取消（保留目前版本）: terminate, wait, end as cancelled — unless the run already
    published (``cancelled: false``, the published version stands)."""
    try:
        result = service.cancel(by=request_actor(conn, request))
    except (service.NotRunning, service.CannotCancel) as exc:
        return _refused(exc)
    return {"run_id": result.run_id, "status": result.status, "cancelled": result.cancelled}


__all__ = ["router"]
