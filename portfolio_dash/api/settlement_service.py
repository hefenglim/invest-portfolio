"""The settlement service: starts, watches, cancels and reports settlement runs (PERF).

Spec ``docs/spec/2026-10-18-settlement-store.md`` §9.1, §9.4, §9.6, §15; plan
``docs/superpowers/plans/2026-10-18-settlement-store.md`` §6.2 (package E), P4, P5, P7, P12–P14.
The PARENT of every run:

- :func:`final_through` and :func:`build_request` compute every input that depends on the
  schedule or ``job_runs`` (P12) — the child has no scheduler knowledge;
- :func:`start_run` writes the run's ``job_runs`` row (``job_id = 'settlement'``) and its
  ``preparing`` report in ONE commit, then starts the worker: a child process through
  ``shared/child_runner`` (``PD_SETTLE_MODE=child``) or, inline, ``settle_worker.run``
  synchronously in the caller's thread (P5: a request that starts an inline run returns after
  it; inline is never handed to a background thread);
- the waiter (:func:`_after_exit`) finalises the report when the child did not (a kill, an
  OOM, a child that died before it claimed), finalises ``job_runs`` per DEF-067
  (``settlement_models.job_status_for``), pushes on a failed AUTOMATIC run (P4, Q-P1 = (a)),
  calls ``flight.note_write()``, pre-warms, then starts the follow-up (chained / resume /
  queued);
- :func:`cancel` terminates, waits, and ends the run as cancelled unless it published;
- :func:`on_boot` + :func:`start_after_boot` are the boot hook (see their docstrings);
- :func:`status` is the chip's read — it never writes and never starts anything.

**Liveness is the OS lock** (``proc_lock.is_held(settlement_lock_path())``), never the
heartbeat: a silent heartbeat only reads 「進行緩慢」 after :data:`SLOW_AFTER_S`. A ``preparing``
report reads 準備中 whatever the lock says (the child takes the lock first, then claims).

**What this module assumes of the worker** (``portfolio/settle_worker.py``, package D): it
reads its :class:`RunRequest` from the ``preparing`` report by run id; it takes
``<data>/settlement.lock`` FIRST and holds it for its whole life; it writes
``<data>/settlement-progress.json`` (:func:`progress_path`, a :class:`RunProgress`) with its
OWN ``os.getpid()`` (a Windows venv's ``Popen.pid`` is the launcher's); it claims, then ends
every run it claimed through ``swap`` or ``end_without_publish`` and freezes its report. A run
whose report is still ``preparing`` / ``running`` when the worker exits is ended here.

Layering: the top layer (L6). ``scheduler/`` reaches this module only through the two seams
it registers (``register_settlement_runner`` → :func:`scheduled_run`,
``register_after_job_hook`` → :func:`after_job_hook`, plan P13).
"""

from __future__ import annotations

import bisect
import contextlib
import json
import logging
import os
import re
import signal
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

from portfolio_dash.api import auth_store
from portfolio_dash.ops import notify
from portfolio_dash.portfolio import settle_worker
from portfolio_dash.portfolio import settlement_store as store
from portfolio_dash.portfolio.settlement_models import (
    ACTIVE_RUN_STATUSES,
    ACTOR_BOOT,
    CHANGE_OP_ZH,
    CHANGE_TABLE_ZH,
    JOB_REASON_PREFIX,
    PHASE_LABELS,
    PHASES,
    SELFCHECK_DIFF_REASON,
    ChangeRecord,
    PendingChanges,
    RunChanges,
    RunFailure,
    RunPhase,
    RunProgress,
    RunRange,
    RunReport,
    RunRequest,
    RunScope,
    RunStatus,
    RunSummary,
    RunVersions,
    SettlementStatus,
    SettlePrompt,
    SettleState,
    actor_text,
    job_status_for,
)
from portfolio_dash.portfolio.settlement_store import TriggerCheck
from portfolio_dash.scheduler import jobs
from portfolio_dash.scheduler.jobs import JobOutcome
from portfolio_dash.shared import child_runner, flags, flight, proc_lock, result_cache
from portfolio_dash.shared.account_ref import account_ref
from portfolio_dash.shared.clock import app_now
from portfolio_dash.shared.config import get_settings
from portfolio_dash.shared.db import session
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.llm import UsageTally

logger = logging.getLogger(__name__)

_TPE = ZoneInfo("Asia/Taipei")
_ONE_DAY = timedelta(days=1)

#: The worker entry the child runs (``python -m <module> --run <id>``). Tests point it at a
#: stub module; nothing else changes it.
WORKER_MODULE = "portfolio_dash.portfolio.settle_worker"
#: ``<data>/settlement-progress.json`` (spec §9.5), written by the worker.
PROGRESS_FILE = "settlement-progress.json"
#: A run with no progress for this long reads 「進行緩慢」 (spec §9.4: never 已中斷).
SLOW_AFTER_S = 60.0
#: A day older than this qualifies for ``final_through`` regardless of its quote verdict —
#: the live staleness age (``pricing/store.py`` ``_DEFAULT_MAX_AGE``; a test pins them equal).
STALE_AGE_DAYS = 4
#: How long a cancel waits for the child after ``terminate`` before it kills it.
TERMINATE_TIMEOUT_S = 10.0
#: How long boot waits for an orphan child's lock to free after terminating it.
ORPHAN_LOCK_WAIT_S = 15.0
#: The pre-warm's quiet delay after a run (debounced by ``result_cache``).
PREWARM_DELAY_S = 1.0
#: The one-shot scheduler job that sends a push held through quiet hours (Q-P1 = (a)).
NOTIFY_JOB_ID = "settlement_notify_pending"
#: Runs a person started; every other reason is automatic (spec §9.1, Q2).
USER_REASONS: frozenset[str] = frozenset({"prompt", "button"})
#: The placeholder a stored trigger sentence carries for the person who started the run; it
#: renders as 「你」 only for that person (spec §9.5).
STARTED_BY_TOKEN = "{started_by}"


# =============================================================================================
# Errors (each with its zh sentence for the API's PdApiError envelope)
# =============================================================================================


class SettlementError(RuntimeError):
    """A refused settlement operation; ``code`` / ``message`` are the API error's."""

    code = "settlement_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class SettlementOff(SettlementError):
    code = "settle_off"

    def __init__(self) -> None:
        super().__init__("重新結算已停用，歷史照常即時計算")


class AlreadyRunning(SettlementError):
    code = "already_running"

    def __init__(self, run_id: int) -> None:
        super().__init__(f"重新結算 #{run_id} 正在進行，請等它完成或先取消")
        self.run_id = run_id


class NotRunning(SettlementError):
    code = "not_running"

    def __init__(self) -> None:
        super().__init__("目前沒有進行中的重新結算")


class CannotCancel(SettlementError):
    code = "cannot_cancel"

    def __init__(self) -> None:
        super().__init__("這次重新結算在同步模式下執行，無法中途取消")


# =============================================================================================
# Small helpers
# =============================================================================================


def _now() -> datetime:
    return app_now()


def _today(now: datetime) -> date:
    return now.astimezone(_TPE).date()


def _reporting() -> Currency:
    return get_settings().reporting_currency


def progress_path(data: Path | None = None) -> Path:
    """``<data>/settlement-progress.json`` — the worker's progress file (spec §9.5)."""
    return (data if data is not None else proc_lock.data_dir()) / PROGRESS_FILE


def _lock_path() -> Path:
    return proc_lock.settlement_lock_path()


def read_progress(run_id: int | None = None) -> RunProgress | None:
    """The progress file, when it describes *run_id* (any run when ``None``). Never raises:
    a missing, half-written or foreign file reads as ``None``."""
    try:
        raw = progress_path().read_text(encoding="utf-8")
        progress = RunProgress.model_validate(json.loads(raw))
    except (OSError, ValueError):
        return None
    if run_id is not None and progress.run_id != run_id:
        return None
    return progress


def who_text(started_by: str | None, viewer: str | None) -> str:
    """Who started a run, for *viewer* (spec §9.5): 「你」 only for that same person."""
    if started_by is None:
        return "系統"
    if viewer is not None and started_by == viewer:
        return "你"
    return actor_text(started_by)


def render_trigger(text: str, started_by: str | None, viewer: str | None) -> str:
    """A stored trigger sentence with ``{started_by}`` rendered for *viewer*."""
    if STARTED_BY_TOKEN not in text:
        return text
    return text.replace(STARTED_BY_TOKEN, who_text(started_by, viewer))


def _render_summary(summary: RunSummary, viewer: str | None) -> RunSummary:
    return summary.model_copy(update={
        "trigger_text": render_trigger(summary.trigger_text, summary.started_by, viewer)})


def _render_report(report: RunReport, viewer: str | None) -> RunReport:
    return report.model_copy(update={"summary": _render_summary(report.summary, viewer)})


def _render_progress(progress: RunProgress, viewer: str | None) -> RunProgress:
    return progress.model_copy(update={
        "trigger_text": render_trigger(progress.trigger_text, progress.started_by, viewer)})


def _seconds_text(seconds: float | None) -> str:
    if seconds is None:
        return "耗時不明"
    total = int(round(seconds))
    if total < 1:
        return "耗時不到 1 秒"
    minutes, secs = divmod(total, 60)
    if minutes:
        return f"耗時 {minutes} 分 {secs} 秒" if secs else f"耗時 {minutes} 分"
    return f"耗時 {secs} 秒"


def _no_ai() -> UsageTally:
    """The spend of a settlement run's ``job_runs`` row: none. The worker never imports the
    LLM client (spec §9.2), so an empty tally records NULL spend — "no AI here" — the way
    every run closer records its own (``tests/scheduler/test_run_records_its_spend.py``)."""
    return UsageTally()


def _is_automatic(reason: str) -> bool:
    return reason not in USER_REASONS


# =============================================================================================
# final_through (spec §9.1)
# =============================================================================================


@dataclass(frozen=True)
class FinalThrough:
    """The last day a run may settle (spec §9.1).

    ``through``: the day before the earliest NON-qualifying day, or yesterday. ``blocked_on``
    / ``blocked_jobs``: that day and the quote jobs that kept it from qualifying.
    ``unconfirmed_closes``: for each day that qualified only by the 4-day rule, the held
    symbols of its run's ``held_failed`` (an empty list when the fire wrote no row).
    """

    through: date
    blocked_on: date | None = None
    blocked_jobs: tuple[str, ...] = ()
    unconfirmed_closes: dict[date, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class _JobRun:
    started: datetime
    run_id: int
    finished: bool
    status: str | None
    held_failed: tuple[str, ...]


def _aware(text: str) -> datetime | None:
    try:
        value = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=_TPE)


def _held_failed(results: object) -> tuple[str, ...]:
    if not results:
        return ()
    try:
        data = json.loads(str(results))
    except ValueError:
        return ()
    held = data.get("held_failed") if isinstance(data, dict) else None
    return tuple(str(s) for s in held) if isinstance(held, list) else ()


def _quote_runs(conn: sqlite3.Connection,
                since: date) -> tuple[dict[str, list[_JobRun]], dict[str, datetime]]:
    """The quote jobs' ``job_runs`` rows started from two days before *since*, oldest first,
    and each job's go-live (its FIRST row's start). ``results`` may be absent: a database whose
    boot migration never ran reads as "no list". The ISO ``started_at`` compares as text on
    its date prefix; the two days' margin covers any offset."""
    marks = ", ".join("?" for _ in jobs.QUOTE_JOB_IDS)
    params = (*jobs.QUOTE_JOB_IDS, (since - timedelta(days=2)).isoformat())
    where = f"WHERE job_id IN ({marks}) AND started_at >= ? ORDER BY id"
    try:
        rows = conn.execute(
            "SELECT id, job_id, started_at, finished_at, status, results FROM job_runs "
            + where, params).fetchall()
        with_results = True
    except sqlite3.OperationalError:
        try:
            rows = conn.execute(
                "SELECT id, job_id, started_at, finished_at, status FROM job_runs " + where,
                params).fetchall()
        except sqlite3.OperationalError:
            return {}, {}
        with_results = False
    go_live: dict[str, datetime] = {}
    for first in conn.execute(
            f"SELECT job_id, started_at FROM job_runs WHERE id IN (SELECT MIN(id) FROM job_runs "
            f"WHERE job_id IN ({marks}) GROUP BY job_id)", jobs.QUOTE_JOB_IDS):
        started = _aware(str(first[1]))
        if started is not None:
            go_live[str(first[0])] = started
    out: dict[str, list[_JobRun]] = {}
    for row in rows:
        started = _aware(str(row[2]))
        if started is None:
            continue
        out.setdefault(str(row[1]), []).append(_JobRun(
            started=started, run_id=int(row[0]), finished=row[3] is not None,
            status=None if row[4] is None else str(row[4]),
            held_failed=_held_failed(row[5]) if with_results else ()))
    for runs in out.values():
        runs.sort(key=lambda r: (r.started, r.run_id))
    return out, go_live


def _fires(trigger: CronTrigger, start: datetime, last_day: date,
           tz: ZoneInfo) -> Iterator[datetime]:
    """The trigger's fire times from *start* whose date in *tz* is on or before *last_day*."""
    fire = trigger.get_next_fire_time(None, start)
    while fire is not None and fire.astimezone(tz).date() <= last_day:
        yield fire
        fire = trigger.get_next_fire_time(fire, fire + timedelta(seconds=1))


def _verdict(runs: Sequence[_JobRun], starts: Sequence[datetime], fire: datetime,
             day: date, tz: ZoneInfo) -> _JobRun | None:
    """The run that answers *fire*: the LATEST finished row started at or after the fire on
    the fire's own local date (a same-day re-run after a failed fire counts)."""
    found: _JobRun | None = None
    for i in range(bisect.bisect_left(starts, fire), len(runs)):
        run = runs[i]
        if run.started.astimezone(tz).date() != day:
            break
        if run.finished:
            found = run if found is None or run.run_id > found.run_id else found
    return found


#: ``final_through``'s *since* for "every quote run since go-live" (``job_runs`` is never
#: pruned, so the whole history is there and small: about 250 rows per job and year).
SINCE_GO_LIVE = date(1900, 1, 1)


def _schedule_rows(conn: sqlite3.Connection,
                   now: datetime) -> list[tuple[str, bool, str, str, datetime | None]]:
    """The quote jobs' schedules and when each last changed: ``updated_at`` (stamped by the
    排程 page or a boot), or *now* when the schedule differs from its stamp — edited by hand
    since the last boot, at a moment nobody recorded, so no past fire can be trusted to the
    current cron. ``None`` = never changed (or a database whose boot migration has not run)."""
    marks = ", ".join("?" for _ in jobs.QUOTE_JOB_IDS)
    try:
        rows = conn.execute(
            f"SELECT job_id, enabled, cron, timezone, updated_at, stamped_schedule "
            f"FROM schedule_config WHERE job_id IN ({marks}) ORDER BY job_id",
            jobs.QUOTE_JOB_IDS).fetchall()
    except sqlite3.OperationalError:
        try:
            rows = [(*r, None, None) for r in conn.execute(
                f"SELECT job_id, enabled, cron, timezone FROM schedule_config "
                f"WHERE job_id IN ({marks}) ORDER BY job_id", jobs.QUOTE_JOB_IDS).fetchall()]
        except sqlite3.OperationalError:
            return []
    out: list[tuple[str, bool, str, str, datetime | None]] = []
    for r in rows:
        job_id, enabled, cron, tz = str(r[0]), bool(r[1]), str(r[2]), str(r[3])
        changed = None if r[4] is None else _aware(str(r[4]))
        if r[5] is not None and r[5] != jobs.schedule_key(cron, tz, enabled):
            changed = now
        out.append((job_id, enabled, cron, tz, changed))
    return out


def final_through(conn: sqlite3.Connection, now: datetime, *,
                  since: date | None = None) -> FinalThrough:
    """The last day a run may settle (spec §9.1), from ``schedule_config`` and ``job_runs``,
    never from the store. Reads only.

    A day D qualifies when every market quote job (``jobs.QUOTE_JOB_IDS``) that was DUE on D
    ended ``ok``. Due = a fire of the job's cron, enabled, with the scheduler on, whose date
    in the job's OWN timezone is D (``quotes_us`` at 16:30 New York judges D although it fires
    on D + 1 in Taipei), at or after the job's first ``job_runs`` row (go-live). The fire's
    verdict is the latest finished row of that job started at or after the fire on the same
    local date (a refresh earlier that day does not answer it); a fire still in the future,
    or one with no finished row, is not ``ok``. A day older than :data:`STALE_AGE_DAYS`
    qualifies regardless and lists its row's ``held_failed`` in ``unconfirmed_closes``.
    Nothing is due with ``PD_DISABLE_SCHEDULER=1``.

    **A schedule change never rewrites history** (review E #2). The CURRENT cron says nothing
    about the fires before the schedule last changed (``schedule_config.updated_at``, stamped
    by ``jobs.update_schedule`` and by a boot after a hand edit; a hand edit not yet seen by a
    boot counts as changed now): before that moment a day is judged by the latest finished
    row of the job on that local date, whatever time it ran, and a day with no row at all was
    not due.

    *since* bounds the fires examined (:data:`SINCE_GO_LIVE` = every quote run since go-live,
    what a run's ``unconfirmed_closes`` needs); without it only the days that can still block
    are examined (the status poll).
    """
    today = _today(now)
    yesterday = today - _ONE_DAY
    if os.environ.get("PD_DISABLE_SCHEDULER") == "1":
        return FinalThrough(through=yesterday)
    window_from = since if since is not None else today - timedelta(days=STALE_AGE_DAYS + 1)
    schedule = _schedule_rows(conn, now)
    if not schedule:
        return FinalThrough(through=yesterday)
    runs_by_job, go_lives = _quote_runs(conn, window_from)
    blocked: dict[date, set[str]] = {}
    unconfirmed: dict[date, set[str]] = {}

    def judge(job_id: str, day: date, answer: _JobRun | None) -> None:
        if answer is not None and answer.status == "ok":
            return
        if (today - day).days > STALE_AGE_DAYS:
            unconfirmed.setdefault(day, set()).update(
                answer.held_failed if answer is not None else ())
        else:
            blocked.setdefault(day, set()).add(job_id)

    for job_id, enabled, cron, tz_name, changed_at in schedule:
        if not enabled:
            continue  # a market job the owner switched off: nothing is due
        first_run = go_lives.get(job_id)
        if first_run is None:
            continue  # before go-live: nothing is due
        runs = runs_by_job.get(job_id, [])
        try:
            tz = ZoneInfo(tz_name)
            trigger = CronTrigger.from_crontab(cron, timezone=tz)
        except Exception:  # noqa: BLE001 — an unusable schedule row fires nothing
            logger.warning("final_through: cannot read the schedule of %s", job_id)
            continue
        starts = [r.started for r in runs]
        # Go-live: the first row's own local DATE — the fire that wrote that row (it starts a
        # moment after its fire time) is due, every fire before that day is not.
        go_live = datetime.combine(first_run.astimezone(tz).date(), dtime.min, tz)
        start = max(go_live, datetime.combine(window_from, dtime.min, tz))
        strict_days: set[date] = set()
        for fire in _fires(trigger, start, yesterday, tz):
            if changed_at is not None and fire < changed_at:
                continue  # the current cron was not in effect then
            day = fire.astimezone(tz).date()
            strict_days.add(day)
            judge(job_id, day, None if fire > now else _verdict(runs, starts, fire, day, tz))
        if changed_at is None:
            continue
        # Before the change: every day the job ran, by its latest finished row that day.
        before: dict[date, _JobRun | None] = {}
        for run in runs:
            if run.started < start or run.started >= changed_at:
                continue
            day = run.started.astimezone(tz).date()
            if day > yesterday or day in strict_days:
                continue
            seen = before.get(day)
            if run.finished and (seen is None or run.run_id > seen.run_id):
                before[day] = run
            else:
                before.setdefault(day, None)
        for day, answer in before.items():
            judge(job_id, day, answer)
    if blocked:
        first = min(blocked)
        through = first - _ONE_DAY
        return FinalThrough(
            through=through, blocked_on=first, blocked_jobs=tuple(sorted(blocked[first])),
            unconfirmed_closes={d: sorted(s) for d, s in sorted(unconfirmed.items())
                                if d <= through})
    return FinalThrough(through=yesterday, unconfirmed_closes={
        d: sorted(s) for d, s in sorted(unconfirmed.items())})


# =============================================================================================
# The run request and the trigger sentence (P12, spec §9.5)
# =============================================================================================


def build_request(conn: sqlite3.Connection, *, scope: RunScope, reason: str,
                  started_by: str | None, now: datetime) -> RunRequest:
    """The run's inputs (P12): ``through`` = :func:`final_through`, never moved below the
    days the store already holds for a claiming scope (a job just enabled must not move
    ``settled_through`` back — the claim clamps the same way), and the unconfirmed closes of
    every day up to ``through``."""
    state = store.store_state(conn, reporting=_reporting())
    # Every quote run since go-live, never only the run's first day on (review E #10): the
    # claim may start earlier than any day known now (a back-dated change before it), and a
    # full run or a self-check re-derives the marks of every old close. ``job_runs`` is never
    # pruned, so the map is complete.
    ft = final_through(conn, now, since=SINCE_GO_LIVE)
    through = ft.through
    if scope != "selfcheck":
        floor = max((d for d in (state.settled_through, state.held_through) if d is not None),
                    default=None)
        if floor is not None and floor > through:
            through = floor
    closes = {d: syms for d, syms in ft.unconfirmed_closes.items() if d <= through}
    return RunRequest(scope=scope, reason=reason, started_by=started_by, through=through,
                      unconfirmed_closes=closes)


def _pending_cause(conn: sqlite3.Connection) -> str:
    tables = store.pending_changes(conn, limit=0).tables
    labels = list(dict.fromkeys(t.label for t in tables))
    return "、".join(labels) + "變更" if labels else ""


def trigger_text(reason: str, *, now: datetime, cause: str = "",
                 previous_run_id: int | None = None, why: str | None = None) -> str:
    """The stored trigger sentence (spec §9.5 / §9.6 Summary). A run a person started keeps
    :data:`STARTED_BY_TOKEN`, rendered per viewer by :func:`render_trigger`."""
    hm = now.astimezone(_TPE).strftime("%H:%M")
    if reason == "prompt":
        tail = f"確認提示：{cause}" if cause else "確認提示"
        return f"{STARTED_BY_TOKEN} 於 {hm} 啟動（{tail}）"
    if reason == "button":
        return f"{STARTED_BY_TOKEN} 於 {hm} 啟動（重新結算：驗證並重建歷史）"
    if reason == "boot":
        return f"系統啟動時自動執行（{why or '啟動檢查'}）"
    if reason == "nightly":
        return f"每晚例行結算（{hm}）"
    if reason == "selfcheck":
        return f"每週自檢（{hm}）"
    if reason == "chained":
        return (f"接續 #{previous_run_id} 結算期間新增的變更" if previous_run_id is not None
                else "接續結算期間新增的變更")
    if reason == "resume":
        return (f"續跑中斷的結算 #{previous_run_id}" if previous_run_id is not None
                else "續跑中斷的結算")
    if reason.startswith(JOB_REASON_PREFIX):
        job_id = reason[len(JOB_REASON_PREFIX):]
        return f"排程工作「{job_id}」變更了更早的歷史資料，{hm} 自動執行"
    return reason


# =============================================================================================
# The in-process state: the active run, the automatic queue, the deferred push
# =============================================================================================


@dataclass
class _Active:
    run_id: int
    scope: RunScope
    reason: str
    inline: bool
    handle: child_runner.ChildHandle | None = None
    cancel_by: str | None = None
    cancel_requested: bool = False


@dataclass(frozen=True)
class _Pending:
    """An automatic run waiting for the active one (nightly, self-check, a job's)."""

    scope: RunScope
    reason: str
    why: str | None = None
    previous_run_id: int | None = None
    mark_resumed: bool = False


class _State:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.active: _Active | None = None
        self.queue: list[_Pending] = []
        self.scheduler: Any = None
        self.timer: threading.Timer | None = None


_S = _State()


def reset_for_tests() -> None:
    """Forget the in-process state (tests only; a run in flight is not touched)."""
    with _S.lock:
        if _S.timer is not None:
            _S.timer.cancel()
        _S.active = None
        _S.queue = []
        _S.scheduler = None
        _S.timer = None


def bind_scheduler(scheduler: Any) -> None:
    """The live APScheduler (``None`` when disabled): a push held through quiet hours is
    sent by a one-shot job on it; without one, by a daemon timer."""
    with _S.lock:
        _S.scheduler = scheduler


def shutdown() -> None:
    """App shutdown: stop the deferred push timer. A settle child is a separate process and
    is left alone — boot finalises whatever it leaves behind (spec §9.4)."""
    with _S.lock:
        if _S.timer is not None:
            _S.timer.cancel()
            _S.timer = None
        _S.scheduler = None


def active_run_id() -> int | None:
    """The run this process started that has not been finalised yet."""
    with _S.lock:
        return None if _S.active is None else _S.active.run_id


def _in_process_alive(run_id: int) -> bool:
    with _S.lock:
        active = _S.active
        if active is None or active.run_id != run_id:
            return False
        if active.inline:
            return True
        return active.handle is not None and active.handle.state != "exited"


def _enqueue(pending: _Pending) -> None:
    with _S.lock:
        if all((p.scope, p.reason) != (pending.scope, pending.reason) for p in _S.queue):
            _S.queue.append(pending)


# =============================================================================================
# Starting a run
# =============================================================================================


@dataclass(frozen=True)
class RunEnd:
    """How a run ended, as the waiter recorded it."""

    run_id: int
    status: RunStatus
    job_status: str
    detail: str
    reason: str
    scope: RunScope
    resumed: bool = False
    dirty_after: date | None = None


@dataclass(frozen=True)
class Started:
    """A started run; ``ended`` is set when it ran inline (it has finished by then)."""

    run_id: int
    ended: RunEnd | None = None


@dataclass(frozen=True)
class AutoStart:
    """An automatic start: started, queued behind the active run, or nothing (settling off)."""

    run_id: int | None = None
    queued_behind: int | None = None
    ended: RunEnd | None = None


def _active_run_ids(conn: sqlite3.Connection) -> list[int]:
    """Every ``preparing`` / ``running`` run, oldest first, from the COLUMNS alone: a report
    whose JSON no longer reads (``active_or_unfinished_runs`` validates every one) must not
    stop the cleanup that ends it (review E #8)."""
    marks = ", ".join("?" for _ in ACTIVE_RUN_STATUSES)
    return [int(r[0]) for r in conn.execute(
        f"SELECT run_id FROM settle_run WHERE status IN ({marks}) ORDER BY run_id",
        sorted(ACTIVE_RUN_STATUSES))]


def _db_active_run(conn: sqlite3.Connection) -> int | None:
    ids = _active_run_ids(conn)
    return ids[-1] if ids else None


_STALE_REASON = "結算程序已不在執行，紀錄未能結束"


def _end_stale(conn: sqlite3.Connection, run_id: int, now: datetime, *,
               reason_text: str) -> None:
    """End a ``preparing`` / ``running`` run that nobody will finish — no waiter in this
    process and its lock free — as 中斷, the way boot ends an orphan (review E #4). A report
    that no longer reads ends through the columns alone (``end_without_publish``)."""
    try:
        with _savepoint(conn, "settle_stale"):
            report = store.get_run(conn, run_id)
            phase = _current_phase(report) if report is not None else None
            _finalise(conn, run_id, exit_info=None, cancel_by=None, now=now,
                      failure_override=RunFailure(phase=phase, reason_text=reason_text))
    except Exception:  # noqa: BLE001 — never let one broken report wedge the service
        logger.exception("settlement run %s could not be ended normally", run_id)
        with _savepoint(conn, "settle_stale_columns"):
            store.end_without_publish(conn, run_id, "interrupted")
            jobs.finish_job_run(conn, run_id, status="error",
                                detail=f"重新結算中斷：{reason_text}", reason="interrupted",
                                now=now, usage=_no_ai(), commit=False)


def _clear_stale(conn: sqlite3.Connection, now: datetime) -> int | None:
    """The run still active, or ``None`` after ending every stale one (§9.4 liveness: the OS
    lock, and this process's own waiter)."""
    for run_id in _active_run_ids(conn):
        if _in_process_alive(run_id) or proc_lock.is_held(_lock_path()):
            return run_id
        _end_stale(conn, run_id, now, reason_text=_STALE_REASON)
    return None


def start_run(*, scope: RunScope, reason: str, started_by: str | None = None,
              now: datetime | None = None, previous_run_id: int | None = None,
              why: str | None = None, mark_resumed: bool = False) -> Started:
    """Start a run (plan §6.2 "Lifecycle"): its ``job_runs`` row and ``preparing`` report are
    COMMITTED together on a session of this function's own, then the worker starts — a child
    (``PD_SETTLE_MODE=child``, the call returns at once) or inline (the call returns after the
    run and its follow-ups). Raises :class:`SettlementOff` with ``PD_SETTLE=off`` and
    :class:`AlreadyRunning` while a run is active (this process's, or any ``preparing`` /
    ``running`` report — boot ends every orphan first)."""
    if not flags.settle_enabled():
        raise SettlementOff
    now = now or _now()
    with _S.lock:
        if _S.active is not None:
            raise AlreadyRunning(_S.active.run_id)
        with session(actor=started_by or ACTOR_BOOT) as conn:
            # A report left active by a waiter that could not finish it (its lock free, no
            # run here) is ended 中斷 first, never refused or queued behind (review E #4).
            busy = _clear_stale(conn, now)
            if busy is not None:
                raise AlreadyRunning(busy)
            request = build_request(conn, scope=scope, reason=reason, started_by=started_by,
                                    now=now)
            text = trigger_text(reason, now=now, cause=_pending_cause(conn),
                                previous_run_id=previous_run_id, why=why)
            run_id = jobs.start_job_run(conn, jobs.SETTLEMENT_JOB_ID, now=now, commit=False)
            store.create_run(conn, request, run_id=run_id, trigger_text=text, started_at=now,
                             previous_run_id=previous_run_id)
            if mark_resumed and previous_run_id is not None:
                # 「中斷，已續跑」 only when a resume actually started (spec §9.4).
                store.update_run(conn, previous_run_id, resumed=True)
        inline = flags.settle_mode() == "inline"
        _S.active = _Active(run_id=run_id, scope=scope, reason=reason, inline=inline)
        if not inline:
            handle = child_runner.spawn(
                WORKER_MODULE, ["--run", str(run_id)], heavy=True,
                on_exit=lambda result: _child_exited(run_id, result))
            if _S.active is not None and _S.active.run_id == run_id:
                _S.active.handle = handle
            return Started(run_id=run_id)
    # Inline (P5): synchronous, in the caller's thread, outside the lock.
    escaped: BaseException | None = None
    try:
        settle_worker.run(run_id, db_path=get_settings().db_path)
        exit_info = _Exit(clean=True)
    except Exception as exc:  # noqa: BLE001 — recorded on the run, never raised to the caller
        logger.warning("settlement run %s raised", run_id, exc_info=True)
        exit_info = _Exit(clean=False, error_class=type(exc).__name__)
    except BaseException as exc:  # an interrupt: end the run, then let it through
        exit_info = _Exit(clean=False, error_class=type(exc).__name__)
        escaped = exc
    ended = _after_exit(run_id, exit_info)
    if escaped is not None:
        raise escaped
    return Started(run_id=run_id, ended=ended)


def start_automatic(*, scope: RunScope, reason: str, now: datetime | None = None,
                    previous_run_id: int | None = None, why: str | None = None,
                    mark_resumed: bool = False) -> AutoStart:
    """An automatic start (boot, nightly, self-check, a job's, chained, resume): queued behind
    the active run instead of refused, nothing at all with settling off."""
    try:
        started = start_run(scope=scope, reason=reason, started_by=None, now=now,
                            previous_run_id=previous_run_id, why=why,
                            mark_resumed=mark_resumed)
    except SettlementOff:
        return AutoStart()
    except AlreadyRunning as busy:
        _enqueue(_Pending(scope=scope, reason=reason, why=why,
                          previous_run_id=previous_run_id, mark_resumed=mark_resumed))
        return AutoStart(queued_behind=busy.run_id)
    return AutoStart(run_id=started.run_id, ended=started.ended)


# =============================================================================================
# The waiter: what happens when a worker exits
# =============================================================================================


@dataclass(frozen=True)
class _Exit:
    clean: bool
    error_class: str | None = None
    never_started: bool = False
    terminated: bool = False


_EXC_LINE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt))\b")


def _exception_class(stderr_tail: str) -> str | None:
    """The class of the exception a child's traceback ends with (``job_runs.reason``)."""
    for line in reversed(stderr_tail.strip().splitlines()):
        m = _EXC_LINE.match(line.strip())
        if m:
            return m.group(1).rsplit(".", 1)[-1]
    return None


def _child_exited(run_id: int, result: child_runner.ChildResult) -> None:
    exit_info = _Exit(clean=result.ok, error_class=_exception_class(result.stderr_tail),
                      never_started=not result.started, terminated=result.terminated)
    _after_exit(run_id, exit_info)


def _current_phase(report: RunReport) -> str | None:
    """The phase a run stopped in: the progress file's running phase, else the report's."""
    progress = read_progress(report.summary.run_id)
    for phases in ((progress.phases if progress is not None else []), report.steps):
        for phase in phases:
            if phase.status in ("running", "failed"):
                return phase.key
        done = [p for p in phases if p.status == "done"]
        if done:
            keys = [k for k, _ in PHASES]
            last = keys.index(done[-1].key) if done[-1].key in keys else -1
            return keys[last + 1] if 0 <= last < len(keys) - 1 else done[-1].key
    return None


def _consequence(conn: sqlite3.Connection, run_id: int) -> str:
    """What stays after a run that did not publish (spec §9.5 ``on_cancel``), derived from the
    actual state, never fixed text."""
    state = store.store_state(conn, reporting=_reporting())
    page = store.run_changes(conn, run_id, limit=0)
    handed = page.total_count + sum(c.changes for c in page.counters)
    if state.generation == 0 or state.settled_through is None:
        return "目前尚無結算版本，歷史照常即時計算"
    if not state.calc_ok:
        return "目前沒有可用的結算版本（計算規則已更新），歷史照常即時計算"
    kept = f"保留第 {state.generation} 版（結算至 {state.settled_through.isoformat()}）"
    if handed:
        return f"{kept}；這 {handed} 筆變更回到待結算"
    return f"{kept}，最近這段照常即時計算"


def _abnormal_end(report: RunReport, exit_info: _Exit,
                  cancel_by: str | None) -> tuple[RunStatus, RunFailure]:
    """How to end a run its worker left ``preparing`` / ``running`` (plan §6.2 "Waiter")."""
    phase = _current_phase(report)
    if cancel_by is not None or exit_info.terminated:
        # only cancel() terminates a child (``ChildHandle.terminate_and_wait``)
        return "cancelled", RunFailure(phase=phase, reason_text="已取消（保留目前版本）",
                                       cancelled_by=cancel_by)
    suffix = f"（{exit_info.error_class}）" if exit_info.error_class else ""
    if exit_info.never_started:
        return "failed", RunFailure(phase=None, reason_text="結算程序無法啟動")
    if report.summary.status == "preparing":
        # it never claimed: nothing was taken, so this is a failure, not an interruption
        return "failed", RunFailure(phase=None,
                                    reason_text=f"結算程序在開始結算前結束{suffix}")
    return "interrupted", RunFailure(phase=phase, reason_text=f"結算程序異常結束{suffix}")


def _range_text(summary: RunSummary) -> str:
    if summary.range_from is None or not summary.days:
        return "帳本尚無資料，無需結算"
    return f"{summary.range_from.isoformat()} 起 {summary.days} 天"


#: The settle tables a self-check compares, in the owner's words (DEF-073: no table name in a
#: ``job_runs.detail``).
_SETTLE_TABLE_ZH: dict[str, str] = {
    "settled_day": "逐日結算",
    "settled_period": "月結與年結",
    "settled_period_holding": "期末持倉",
    "settled_period_cash": "期末現金",
}


def _selfcheck_where(report: RunReport) -> str:
    """「N 處，最早於 D（逐日結算）」 from the worker's ``selfcheck`` result, else the history."""
    check = report.selfcheck
    first = (check.first_day if check is not None and check.first_day is not None
             else report.history.first_changed_day if report.history is not None else None)
    count = f"{check.differences} 處，" if check is not None and check.differences else ""
    table = (f"（{_SETTLE_TABLE_ZH.get(check.first_table, '結算資料')}）"
             if check is not None and check.first_table else "")
    at = f"最早於 {first.isoformat()}{table}" if first is not None else ""
    return f"{count}{at}" if count or at else ""


def run_detail(report: RunReport) -> str:
    """The run's ``job_runs.detail`` — one zh sentence (DEF-073), e.g.
    「重新結算 2025-06-16 起 489 天，耗時 14 秒」."""
    s = report.summary
    took = _seconds_text(s.duration_s)
    span = _range_text(s)
    failure = report.failure
    if s.status == "published" and report.inputs.scope == "selfcheck":
        # keyed on the run's SCOPE: a resumed self-check (reason ``resume``) is one too
        if s.selfcheck_diff:
            where = _selfcheck_where(report)
            return f"每週自檢發現差異：{where + '，' if where else ''}與已發布版本不同，{took}"
        return f"每週自檢 {span}：與已發布版本一致，{took}"
    if s.status == "published":
        return f"重新結算 {span}，{took}"
    if s.status == "partial":
        rng = report.range
        published = rng.published_to.isoformat() if rng.published_to is not None else "—"
        handed = rng.handed_from.isoformat() if rng.handed_from is not None else "—"
        diff = "；每週自檢發現差異" if s.selfcheck_diff else ""
        return (f"重新結算 {span}，部分完成：已發布至 {published}，{handed} 起接續處理"
                f"{diff}，{took}")
    if s.status == "superseded":
        return f"重新結算 {span} 已併入下一輪：結算期間有更早的變更，{took}"
    if s.status == "cancelled":
        return f"重新結算已取消，保留目前版本，{took}"
    where = ""
    if failure is not None and failure.phase:
        where = f"（{PHASE_LABELS.get(failure.phase, failure.phase)}）"
    why = failure.reason_text if failure is not None else "原因不明"
    if s.status == "failed":
        return f"重新結算失敗{where}：{why}"
    if s.status == "interrupted":
        return f"重新結算中斷{where}：{why}"
    return f"重新結算{s.status_text}"


def _job_reason(status: str, report: RunReport, exit_info: _Exit | None) -> str | None:
    """``job_runs.reason``: an outcome code (spec §9.4), never the trigger."""
    if status == "cancelled":
        return "cancelled"
    if status == "interrupted":
        return "interrupted"
    if status == "failed":
        # The worker ends a failed run itself and exits 0 (review E #3): the class it records
        # on the failure (``RunFailure.error_class``, package D) comes first; a child that
        # died with a traceback is read from its stderr.
        cls = getattr(report.failure, "error_class", None) if report.failure else None
        if not cls and exit_info is not None:
            cls = exit_info.error_class
        return f"failed:{cls}" if cls else "failed"
    if report.summary.selfcheck_diff:
        return SELFCHECK_DIFF_REASON
    return None


@contextlib.contextmanager
def _savepoint(conn: sqlite3.Connection, name: str) -> Iterator[None]:
    """A SAVEPOINT around a block: released on success, rolled back to on error (and the
    error re-raised). Every service write inside commits nothing (``commit=False``), so the
    savepoint really scopes them."""
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        conn.execute(f"ROLLBACK TO {name}")
        conn.execute(f"RELEASE {name}")
        raise
    conn.execute(f"RELEASE {name}")


def _finalise(conn: sqlite3.Connection, run_id: int, *, exit_info: _Exit | None,
              cancel_by: str | None, now: datetime,
              failure_override: RunFailure | None = None) -> RunEnd:
    """End a run's report if its worker did not, then its ``job_runs`` row, without
    committing: the caller's session (or boot's savepoint) commits."""
    report = store.get_run(conn, run_id)
    if report is None:
        detail = "重新結算失敗：找不到這次結算的紀錄"
        jobs.finish_job_run(conn, run_id, status="error", detail=detail, reason="failed",
                            now=now, usage=_no_ai(), commit=False)
        return RunEnd(run_id=run_id, status="failed", job_status="error", detail=detail,
                      reason="", scope="dirty")
    stored = report.summary.status
    if stored in ACTIVE_RUN_STATUSES:
        if failure_override is not None:
            target: RunStatus = "interrupted"
            failure = failure_override
        else:
            target, failure = _abnormal_end(report, exit_info or _Exit(clean=False),
                                            cancel_by)
        summary = report.summary.model_copy(update={"cancelled_by": cancel_by}
                                            if target == "cancelled" and cancel_by else {})
        store.finalise_run(conn, run_id, target,
                           report.model_copy(update={"failure": failure, "summary": summary}))
    else:
        # The worker ended it; finalising again only fills an end time it left out.
        store.finalise_run(conn, run_id, stored, report)
    ended = store.get_run(conn, run_id)
    assert ended is not None
    status = ended.summary.status
    if status in ("cancelled", "failed", "interrupted", "superseded"):
        failure = ended.failure or RunFailure(reason_text="")
        if not failure.consequence and status != "superseded":
            failure = failure.model_copy(update={"consequence": _consequence(conn, run_id)})
            ended = ended.model_copy(update={"failure": failure})
            store.update_run(conn, run_id, report=ended)
    job_status = job_status_for(status, selfcheck_diff=ended.summary.selfcheck_diff)
    detail = run_detail(ended)
    jobs.finish_job_run(conn, run_id, status=job_status, detail=detail,
                        reason=_job_reason(status, ended, exit_info), now=now, usage=_no_ai(),
                        commit=False)
    dirty_after = store.store_state(conn, reporting=_reporting()).dirty_from
    return RunEnd(run_id=run_id, status=status, job_status=job_status, detail=detail,
                  reason=ended.summary.reason, scope=ended.inputs.scope,
                  resumed=ended.summary.resumed, dirty_after=dirty_after)


def _follow_up(ended: RunEnd) -> _Pending | None:
    """The run that follows *ended* (spec §9.1 "chained", §9.4 resume)."""
    if not flags.settle_enabled():
        return None
    if ended.status == "cancelled":
        # A cancel drops only the cancelled run's OWN follow-up (no chained run, no resume):
        # a nightly or self-check queued behind it already wrote 「排在其後」 and still starts
        # (review E #6).
        with _S.lock:
            return _S.queue.pop(0) if _S.queue else None
    if ended.status == "interrupted" and ended.reason != "resume":
        # At most one automatic resume in a row from the waiter: a child that keeps dying
        # (memory) must not respawn forever. Boot resumes again after a restart.
        return _Pending(scope=ended.scope, reason="resume", previous_run_id=ended.run_id,
                        mark_resumed=True)
    with _S.lock:
        if _S.queue:
            return _S.queue.pop(0)
    if ended.status in ("published", "partial", "superseded") and ended.dirty_after is not None:
        return _Pending(scope="dirty", reason="chained", previous_run_id=ended.run_id)
    return None


def _start_pending(pending: _Pending) -> None:
    try:
        start_automatic(scope=pending.scope, reason=pending.reason, why=pending.why,
                        previous_run_id=pending.previous_run_id,
                        mark_resumed=pending.mark_resumed)
    except Exception:  # noqa: BLE001 — a follow-up that cannot start leaves 待結算
        logger.warning("settlement follow-up %s could not start", pending.reason,
                       exc_info=True)


def _schedule_prewarm() -> None:
    if not flags.prewarm_enabled():
        return
    from portfolio_dash.api import alert_inputs  # local: it pulls in the dashboard build

    result_cache.schedule_prewarm(alert_inputs.prewarm_today, PREWARM_DELAY_S)


#: The waiter's attempts at a busy / locked database, and the first pause (doubled each time).
FINALISE_ATTEMPTS = 6
FINALISE_BACKOFF_S = 0.25


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _finalise_with_retry(run_id: int, *, exit_info: _Exit, cancel_by: str | None,
                         now: datetime) -> RunEnd | None:
    """:func:`_finalise` on a session of its own, retried while the database is busy (review E
    #4); ``None`` when it could not be done (logged)."""
    for attempt in range(FINALISE_ATTEMPTS):
        try:
            with session(actor=ACTOR_BOOT) as conn:
                return _finalise(conn, run_id, exit_info=exit_info, cancel_by=cancel_by,
                                 now=now)
        except sqlite3.OperationalError as exc:
            if not _is_busy(exc) or attempt == FINALISE_ATTEMPTS - 1:
                logger.exception("settlement run %s could not be finalised", run_id)
                return None
            time.sleep(FINALISE_BACKOFF_S * 2 ** attempt)
        except Exception:  # noqa: BLE001 — the waiter must never die silently with a live row
            logger.exception("settlement run %s could not be finalised", run_id)
            return None
    return None


def _after_exit(run_id: int, exit_info: _Exit) -> RunEnd | None:
    """The waiter (plan §6.2): finalise, push on a failed automatic run, ``note_write``,
    pre-warm, then the follow-up. Never raises."""
    with _S.lock:
        active = _S.active if _S.active is not None and _S.active.run_id == run_id else None
        cancel_by = active.cancel_by if active is not None else None
    ended: RunEnd | None = None
    now = _now()
    try:
        ended = _finalise_with_retry(run_id, exit_info=exit_info, cancel_by=cancel_by, now=now)
    finally:
        with _S.lock:
            if _S.active is not None and _S.active.run_id == run_id:
                _S.active = None
    flight.note_write()
    if ended is None:
        # The report stays active: the next start ends it 中斷 (``_clear_stale``) — and a run
        # queued behind it starts now, so it never waits on a run nobody finishes.
        with _S.lock:
            queued = _S.queue.pop(0) if _S.queue else None
        if queued is not None:
            _start_pending(queued)
        return None
    follow = _follow_up(ended)
    if _is_automatic(ended.reason) and (
            ended.status == "failed"
            or (ended.status == "interrupted" and (follow is None or follow.reason != "resume"))):
        try:
            notify_failure(ended.run_id, now=now)
        except Exception:  # noqa: BLE001 — a push is an observer
            logger.warning("settlement failure push for run %s failed", run_id, exc_info=True)
    if follow is None:
        _schedule_prewarm()
    else:
        _start_pending(follow)
    return ended


# =============================================================================================
# Cancel (spec §9.4)
# =============================================================================================


@dataclass(frozen=True)
class CancelResult:
    run_id: int
    status: RunStatus
    #: ``False`` when the run had already published before the cancel landed.
    cancelled: bool


def _terminate_pid(pid: int) -> None:
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)


def _wait_lock_free(path: Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while proc_lock.is_held(path):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def _stop_orphan(run_id: int) -> None:
    """Terminate a settle child that holds the lock but has no waiter in this process (its
    web process restarted), by the pid it recorded, and wait for the lock to free (§9.4)."""
    path = _lock_path()
    if not proc_lock.is_held(path):
        return
    progress = read_progress(run_id)
    if progress is not None and progress.pid:
        _terminate_pid(progress.pid)
    if not _wait_lock_free(path, ORPHAN_LOCK_WAIT_S):
        logger.warning("settlement lock still held after terminating run %s", run_id)


def cancel(*, by: str | None) -> CancelResult:
    """Cancel the active run (spec §9.4): terminate, WAIT for the child to exit, then end the
    run as cancelled — unless it already published, which then stands. Raises
    :class:`NotRunning` when nothing runs and :class:`CannotCancel` for an inline run."""
    with _S.lock:
        active = _S.active
        if active is None:
            handle = None
            run_id = 0
        else:
            if active.inline or active.handle is None:
                # start_run assigns the handle before it releases the lock, so a child run
                # always has one here; an inline run cannot be stopped mid-call (P5).
                raise CannotCancel
            active.cancel_by = by
            active.cancel_requested = True
            handle = active.handle
            run_id = active.run_id
    if handle is None:
        return _cancel_orphan(by=by)
    handle.terminate_and_wait(TERMINATE_TIMEOUT_S)
    with session() as conn:
        report = store.get_run(conn, run_id)
    status: RunStatus = report.summary.status if report is not None else "cancelled"
    return CancelResult(run_id=run_id, status=status, cancelled=status == "cancelled")


def _cancel_orphan(*, by: str | None) -> CancelResult:
    """A run active in the database with no waiter here (another process started it)."""
    with session() as conn:
        busy = _db_active_run(conn)
    if busy is None:
        raise NotRunning
    _stop_orphan(busy)
    with session(actor=ACTOR_BOOT) as conn:
        ended = _finalise(conn, busy, exit_info=_Exit(clean=False, terminated=True),
                          cancel_by=by, now=_now())
    flight.note_write()
    return CancelResult(run_id=busy, status=ended.status, cancelled=ended.status == "cancelled")


# =============================================================================================
# The failure push (plan P4, owner ruling Q-P1 = (a))
# =============================================================================================

_PUSH_TITLE = "portfolio-dash · 重新結算失敗"


def _push_text(report: RunReport) -> str:
    s = report.summary
    why = report.failure.reason_text if report.failure is not None else "原因不明"
    lead = "重新結算中斷" if s.status == "interrupted" else "重新結算失敗"
    return (f"{lead}（#{s.run_id}，{_trigger_label(s.reason)}）：{why}。"
            "歷史走勢與月度成績維持前一版本，帳戶即時數字不受影響。")


def _trigger_label(reason: str) -> str:
    if reason.startswith(JOB_REASON_PREFIX):
        return "排程工作後自動執行"
    return {
        "boot": "系統啟動時自動執行", "nightly": "每晚例行結算", "selfcheck": "每週自檢",
        "chained": "接續結算期間新增的變更", "resume": "續跑中斷的結算",
    }.get(reason, "自動執行")


PushResult = Literal["sent", "held", "suppressed", "unsubscribed", "no_channel", "missing"]


def _set_notify_pending(conn: sqlite3.Connection, run_id: int, value: bool) -> None:
    report = store.get_run(conn, run_id)
    if report is not None and report.notify_pending != value:
        store.update_run(conn, run_id, report=report.model_copy(
            update={"notify_pending": value}))


def _send(conn: sqlite3.Connection, report: RunReport, cfg: notify.NotifyConfig,
          channels: list[notify.NotifyChannel], sender: notify.Sender | None) -> None:
    link = notify.frontend_url(cfg.public_base_url, None)
    (sender or notify.dispatch)(channels, _PUSH_TITLE, _push_text(report), "warn", link)


def _gate(conn: sqlite3.Connection) -> tuple[PushResult | None, notify.NotifyConfig | None,
                                             list[notify.NotifyChannel]]:
    """``digest_service._push``'s gates, in its order: guest / demo → suppressed; no enabled
    channel → skip; unsubscribed → skip. ``None`` = may send (quiet hours are the caller's)."""
    if not auth_store.is_protected(conn):
        return "suppressed", None, []
    cfg = notify.load_config(conn)
    channels = notify.build_enabled_channels(cfg)
    if not channels:
        return "no_channel", cfg, []
    if not cfg.subscriptions.get(notify.SETTLEMENT_FAILED, True):
        return "unsubscribed", cfg, []
    return None, cfg, channels


def notify_failure(run_id: int, *, now: datetime | None = None,
                   sender: notify.Sender | None = None) -> PushResult:
    """Push a failed automatic run ONCE (P4). Inside quiet hours the push is held: the report
    records ``notify_pending`` and a one-shot job at the quiet window's end sends it
    (:func:`send_pending_notifications`); boot sends a still-pending one when it boots outside
    quiet hours."""
    now = now or _now()
    with session(actor=ACTOR_BOOT) as conn:
        report = store.get_run(conn, run_id)
        if report is None:
            return "missing"
        verdict, cfg, channels = _gate(conn)
        if verdict is not None:
            return verdict
        assert cfg is not None
        end = notify.quiet_hours_end(cfg.quiet_hours, now)
        if end is not None:
            _set_notify_pending(conn, run_id, True)
            held = True
        else:
            held = False
            _send(conn, report, cfg, channels, sender)
    if held:
        assert end is not None
        _defer_notify(end)
        return "held"
    return "sent"


def _published_after(conn: sqlite3.Connection, run_id: int) -> bool:
    row = conn.execute("SELECT 1 FROM settle_run WHERE run_id > ? AND status IN "
                       "('published', 'partial') LIMIT 1", (run_id,)).fetchone()
    return row is not None


def _pending_notify_ids(conn: sqlite3.Connection) -> list[int]:
    try:
        rows = conn.execute(
            "SELECT run_id FROM settle_run WHERE json_extract(report, '$.notify_pending') = 1 "
            "ORDER BY run_id").fetchall()
    except sqlite3.OperationalError:
        return []
    return [int(r[0]) for r in rows]


def send_pending_notifications(*, now: datetime | None = None,
                               sender: notify.Sender | None = None) -> int:
    """Send every push held through quiet hours, once each; returns how many were sent.

    The flag is cleared in the same transaction that decides to send, BEFORE the send, so two
    boots (or a boot and the one-shot job) never send one failure twice. Still inside quiet
    hours (the window moved) → held again until it ends; guest / demo, unsubscribed or no
    channel → dropped (never sent later)."""
    now = now or _now()
    sendable: list[RunReport] = []
    defer_at: datetime | None = None
    with session(actor=ACTOR_BOOT) as conn:
        ids = _pending_notify_ids(conn)
        if not ids:
            return 0
        verdict, cfg, channels = _gate(conn)
        if verdict is None:
            assert cfg is not None
            defer_at = notify.quiet_hours_end(cfg.quiet_hours, now)
        if defer_at is None:
            for run_id in ids:
                report = store.get_run(conn, run_id)
                _set_notify_pending(conn, run_id, False)
                # A later run published: 「歷史走勢…維持前一版本」 is no longer true, so the
                # held push is dropped (review E #7).
                if verdict is None and report is not None and not _published_after(conn,
                                                                                    run_id):
                    sendable.append(report)
            conn.commit()
    if defer_at is not None:
        _defer_notify(defer_at)
        return 0
    if sendable:
        assert cfg is not None
        with session(actor=ACTOR_BOOT) as conn:
            for report in sendable:
                _send(conn, report, cfg, channels, sender)
    return len(sendable)


def _send_pending_safely() -> None:
    try:
        send_pending_notifications()
    except Exception:  # noqa: BLE001 — a deferred push is an observer
        logger.warning("held settlement push could not be sent", exc_info=True)


def _defer_notify(at: datetime) -> None:
    """Run :func:`send_pending_notifications` once at *at* (+1 s, past the window's edge)."""
    when = at + timedelta(seconds=1)
    with _S.lock:
        scheduler = _S.scheduler
        if scheduler is not None:
            try:
                from apscheduler.triggers.date import DateTrigger

                # No misfire limit, coalesced: a throttled VM that wakes late still sends it
                # once (APScheduler's default grace is 1 s, review E #7).
                scheduler.add_job(_send_pending_safely, DateTrigger(run_date=when),
                                  id=NOTIFY_JOB_ID, replace_existing=True,
                                  misfire_grace_time=None, coalesce=True)
                return
            except Exception:  # noqa: BLE001 — fall back to a timer
                logger.warning("could not schedule the held settlement push", exc_info=True)
        if _S.timer is not None:
            _S.timer.cancel()
        delay = max(0.0, (when - _now()).total_seconds())
        timer = threading.Timer(delay, _send_pending_safely)
        timer.daemon = True
        timer.name = "settlement-held-push"
        _S.timer = timer
        timer.start()


# =============================================================================================
# The scheduler seams (plan P13)
# =============================================================================================

_JOB_LABEL: dict[str, str] = {"nightly": "每晚例行結算", "selfcheck": "每週自檢"}


def trigger_outcome(label: str, started: AutoStart) -> JobOutcome:
    """The trigger job's own ``job_runs`` verdict (DEF-067 / DEF-073): what it started. The
    run itself has its own row (``job_id = 'settlement'``)."""
    if started.queued_behind is not None:
        return JobOutcome("ok", f"重新結算 #{started.queued_behind} 正在進行，{label}排在其後")
    if started.ended is not None:
        ended = started.ended
        return JobOutcome(ended.job_status, f"{label} #{ended.run_id}：{ended.detail}")
    if started.run_id is None:
        return JobOutcome("skipped", f"重新結算已停用，{label}未執行")
    return JobOutcome("ok", f"已啟動{label} #{started.run_id}，於背景執行")


def scheduled_run(conn: sqlite3.Connection, scope: str, *, now: datetime) -> JobOutcome:
    """The settlement runner seam (``jobs.register_settlement_runner``): the nightly and the
    self-check runs, in the web process (P7). *conn* is the job's; the run opens its own."""
    del conn
    label = _JOB_LABEL.get(scope, "重新結算")
    if scope not in _JOB_LABEL:
        raise ValueError(f"not a scheduled settlement scope: {scope!r}")
    if not flags.settle_enabled():
        return JobOutcome("skipped", f"重新結算已停用，{label}未執行")
    run_scope = cast(RunScope, scope)
    return trigger_outcome(label, start_automatic(scope=run_scope, reason=scope, now=now))


@dataclass(frozen=True)
class TrailMark:
    """Where the change trail stood before a job (review E #9): the last log row, and every
    PENDING price / FX counter's count and earliest day."""

    seq: int
    last_log_id: int
    counters: dict[int, tuple[int, str]]


def trail_mark(conn: sqlite3.Connection) -> TrailMark | None:
    """Read the mark (``None`` without a store). Reads the store's ``settle_dirty``,
    ``settle_change_log`` and ``settle_change_agg`` by direct SQL (``portfolio/
    settlement_store.py`` owns them; the cross-layer read convention)."""
    try:
        seq = int(conn.execute("SELECT seq FROM settle_dirty WHERE id = 1").fetchone()[0])
        last = conn.execute("SELECT MAX(id) FROM settle_change_log").fetchone()[0]
        counters = {int(r[0]): (int(r[1]), str(r[2])) for r in conn.execute(
            "SELECT id, changes, earliest FROM settle_change_agg "
            "WHERE claimed_run_id IS NULL AND settled_run_id IS NULL")}
    except (sqlite3.OperationalError, TypeError):
        return None
    return TrailMark(seq=seq, last_log_id=int(last or 0), counters=counters)


def job_earliest_change(conn: sqlite3.Connection, job_id: str,
                        mark: TrailMark) -> date | None:
    """The earliest day among the changes THIS job recorded since *mark*: its own trail rows
    (actor ``job:<id>``) and the price / FX counters it created or moved earlier. A counter
    that only grew keeps an earliest day older changes set, so it is not the job's — and that
    older day was already pending, prompting the owner, before the job ran."""
    days: list[date] = []
    row = conn.execute(
        f"SELECT MIN({store._AFFECTED_SQL}) FROM settle_change_log WHERE id > ? AND actor = ?",
        (mark.last_log_id, f"job:{job_id}")).fetchone()
    if row is not None and row[0]:
        days.append(date.fromisoformat(str(row[0])))
    for counter_id, changes, earliest in conn.execute(
            "SELECT id, changes, earliest FROM settle_change_agg "
            "WHERE claimed_run_id IS NULL AND settled_run_id IS NULL"):
        before = mark.counters.get(int(counter_id))
        if before is None or (int(changes) > before[0] and str(earliest) < before[1]):
            days.append(date.fromisoformat(str(earliest)))
    return min(days) if days else None


def after_job_hook(job_id: str) -> Callable[[], None] | None:
    """The after-job seam (``jobs.register_after_job_hook``, spec §9.1): mark the change trail
    before the job; after it, start an automatic ``dirty`` run (reason ``job:<id>``) when the
    JOB'S OWN recorded changes reach before the live window (review E #9) — a change inside
    the window is already shown live and waits for the nightly run, and a pending change the
    owner made is the prompt's, not the job's.

    A quote job records nothing in the ordinary case: its quotes are dated after
    ``settled_through`` (the trail bound), so they are live and unrecorded
    (``tests/settlement/test_service_lifecycle.py`` pins it). What can reach the trail is a
    provider revising a past close (``history_daily``'s look-back, a benchmark)."""
    if not flags.settle_enabled():
        return None
    try:
        with session() as conn:
            mark = trail_mark(conn)
    except Exception:  # noqa: BLE001 — no store yet: nothing to compare
        return None
    if mark is None:
        return None

    def after() -> None:
        settle_after_job(job_id, mark=mark)

    return after


def settle_after_job(job_id: str, *, mark: TrailMark,
                     now: datetime | None = None) -> AutoStart | None:
    """:func:`after_job_hook`'s second half (public for tests)."""
    now = now or _now()
    with session() as conn:
        state = store.store_state(conn, reporting=_reporting())
        if state.seq <= mark.seq or not state.usable:
            return None
        earliest = job_earliest_change(conn, job_id, mark)
    if earliest is None or earliest >= store.window_start(_today(now)):
        return None
    return start_automatic(scope="dirty", reason=f"{JOB_REASON_PREFIX}{job_id}", now=now)


# =============================================================================================
# Boot (plan §6.2 on_boot; spec §9.1 boot row, §9.6)
# =============================================================================================


@dataclass(frozen=True)
class BootStart:
    scope: RunScope
    reason: str
    why: str | None = None
    previous_run_id: int | None = None
    mark_resumed: bool = False


@dataclass(frozen=True)
class BootPlan:
    """What :func:`on_boot` decided; :func:`start_after_boot` carries it out once the boot
    session has COMMITTED (the child reads its request from another connection)."""

    #: The run to start, or ``None`` (an identical boot, an empty ledger, ``PD_SETTLE=off``).
    start: BootStart | None = None
    #: The runs boot ended as 中斷（未續跑）.
    interrupted: tuple[int, ...] = ()


_BOOT_INTERRUPTED = "系統重新啟動時結算尚未完成"


def _end_orphans(conn: sqlite3.Connection, now: datetime) -> tuple[int, ...]:
    """Boot step 1 (always, even with ``PD_SETTLE=off``): end every ``preparing`` / ``running``
    report as 中斷（未續跑） with its ``job_runs`` row — terminating an orphan child that still
    holds the lock first and waiting for the lock to free — then every ``settlement`` row
    left unfinished by a web process that died after its run ended (or before its report
    existed)."""
    ended: list[int] = []
    for run_id in _active_run_ids(conn):
        _stop_orphan(run_id)
        _end_stale(conn, run_id, now, reason_text=_BOOT_INTERRUPTED)
        ended.append(run_id)
    rows = conn.execute(
        "SELECT j.id, r.run_id FROM job_runs j LEFT JOIN settle_run r ON r.run_id = j.id "
        "WHERE j.job_id = ? AND j.finished_at IS NULL ORDER BY j.id",
        (jobs.SETTLEMENT_JOB_ID,)).fetchall()
    for row in rows:
        run_id = int(row[0])
        if row[1] is None:
            jobs.finish_job_run(conn, run_id, status="error",
                                detail="重新結算未能開始：系統在建立紀錄前中斷",
                                reason="interrupted", now=now, usage=_no_ai(), commit=False)
            continue
        try:
            with _savepoint(conn, "settle_job_row"):
                _finalise(conn, run_id, exit_info=None, cancel_by=None, now=now)
        except Exception:  # noqa: BLE001 — a report that no longer reads: close the row only
            logger.exception("settlement run %s: its job row is closed without its report",
                             run_id)
            jobs.finish_job_run(conn, run_id, status="error",
                                detail="重新結算紀錄無法讀取，已結束此筆排程紀錄",
                                reason="interrupted", now=now, usage=_no_ai(), commit=False)
    store.cleanup_orphans(conn)
    return tuple(ended)


def _latest_ended(conn: sqlite3.Connection) -> RunSummary | None:
    """The latest ended run's summary (``None`` when its report no longer reads)."""
    try:
        items = store.list_runs(conn, limit=10).items
    except ValueError:  # a pydantic ValidationError: a report written by older code
        logger.warning("a recent settlement report no longer reads", exc_info=True)
        return None
    for summary in items:
        if summary.status not in ACTIVE_RUN_STATUSES:
            return summary
    return None


def _note_not_resumed(conn: sqlite3.Connection, run_ids: Sequence[int], why: str) -> None:
    """「結算中斷（未續跑）」 with the reason (spec §9.4): boot settles in full instead."""
    for run_id in run_ids:
        try:
            report = store.get_run(conn, run_id)
        except ValueError:  # it ended through its columns: nothing to annotate
            continue
        if report is None:
            continue
        failure = report.failure or RunFailure(reason_text=_BOOT_INTERRUPTED)
        failure = failure.model_copy(
            update={"consequence": f"未續跑：系統啟動時改為完整重新結算（{why}）"})
        store.update_run(conn, run_id, report=report.model_copy(update={"failure": failure}))


def on_boot(conn: sqlite3.Connection, *, trigger_check: TriggerCheck) -> BootPlan:
    """The boot hook, on the lifespan's ``system:boot`` session, before
    ``trigger_schema_version`` is recorded (every write here is DML; nothing moves
    ``schema_version``). Uses ONLY *conn*: a second connection would wait on the boot
    session's own write lock.

    1. Always: end every orphan run (:func:`_end_orphans`). With ``PD_SETTLE=off`` that is all.
    2. A calc-version or reporting-currency change, or a recreated trigger set → a full run;
       the interrupted ones stay 未續跑 with the reason.
    3. Otherwise resume the latest interrupted range (reason ``resume``).
    4. An empty store → a full run, unless the ledger has no rows (the first nightly run
       after rows arrive settles it in full).
    5. An identical boot starts nothing.

    Starting is :func:`start_after_boot`'s, once this session has committed.

    **Never raises** (review E #8): every write runs under one SAVEPOINT; on any error it is
    rolled back, logged, and an empty plan returned — a settlement fault must not stop the
    app from booting, with ``PD_SETTLE=off`` least of all.
    """
    try:
        with _savepoint(conn, "settle_boot"):
            return _on_boot(conn, trigger_check)
    except Exception:  # noqa: BLE001 — see above
        logger.exception("the settlement boot step failed; booting without it")
        return BootPlan()


def _on_boot(conn: sqlite3.Connection, trigger_check: TriggerCheck) -> BootPlan:
    now = _now()
    interrupted = _end_orphans(conn, now)
    if not flags.settle_enabled():
        return BootPlan(interrupted=interrupted)
    state = store.store_state(conn, reporting=_reporting())
    start: BootStart | None = None
    if state.generation == 0 or state.settled_through is None:
        if store.first_ledger_day(conn) is not None:
            start = BootStart(scope="full", reason="boot", why="首次結算")
    elif not state.currency_ok:
        start = BootStart(scope="full", reason="boot", why="報告幣別變更")
    elif not state.calc_ok:
        start = BootStart(scope="full", reason="boot", why="計算規則更新")
    elif trigger_check.recreated:
        labels = "、".join(dict.fromkeys(
            CHANGE_TABLE_ZH.get(t, t) for t in trigger_check.tables))
        start = BootStart(scope="full", reason="boot", why=f"觸發器重建：{labels or '全部'}")
    else:
        latest = _latest_ended(conn)
        if latest is not None and latest.status == "interrupted" and not latest.resumed:
            report = store.get_run(conn, latest.run_id)
            scope: RunScope = report.inputs.scope if report is not None else "dirty"
            start = BootStart(scope=scope, reason="resume", previous_run_id=latest.run_id,
                              mark_resumed=True)
    if start is not None and start.reason == "boot" and start.why:
        _note_not_resumed(conn, interrupted, start.why)
    return BootPlan(start=start, interrupted=interrupted)


def start_after_boot(plan: BootPlan) -> int | None:
    """Carry out *plan* after the boot session closed: send any push still held from quiet
    hours (outside them), then start the boot run (child: returns at once; inline: runs it).
    Returns the started run's id. Never raises — a boot must not fail on the settlement."""
    try:
        send_pending_notifications()
    except Exception:  # noqa: BLE001
        logger.warning("held settlement pushes could not be sent at boot", exc_info=True)
    start = plan.start
    if start is None or not flags.settle_enabled():
        return None
    if flags.settle_mode() == "inline" and not _sync_inline_boot():
        # An inline boot run would block the async lifespan for the whole settlement (review
        # E #11): outside tests it runs on a daemon thread, synchronously THERE (P5's "never
        # on a background thread" keeps the tests' SQL tracers honest; nothing traces here).
        threading.Thread(target=_boot_run, args=(start,), name="settlement-boot-run",
                         daemon=True).start()
        return None
    return _boot_run(start)


def _sync_inline_boot() -> bool:
    """Inline boot runs stay synchronous in tests: ``PD_TEST_MODE=1``, or inside pytest."""
    return os.environ.get("PD_TEST_MODE") == "1" or "PYTEST_CURRENT_TEST" in os.environ


def _boot_run(start: BootStart) -> int | None:
    try:
        result = start_automatic(scope=start.scope, reason=start.reason, why=start.why,
                                 previous_run_id=start.previous_run_id,
                                 mark_resumed=start.mark_resumed)
    except Exception:  # noqa: BLE001
        logger.exception("the boot settlement run could not start")
        return None
    return result.run_id


# =============================================================================================
# Status (spec §15) — reads only, never starts anything
# =============================================================================================


def _estimate_s(conn: sqlite3.Connection, days: int) -> float | None:
    """The prompt's estimate: the last published run's seconds per day × *days*."""
    if days <= 0:
        return None
    for summary in store.list_runs(conn, limit=20).items:
        if summary.status in ("published", "partial") and summary.days and summary.duration_s:
            return round(summary.duration_s / summary.days * days, 1)
    return None


def _synth_progress(conn: sqlite3.Connection, summary: RunSummary) -> RunProgress:
    """The panel's run block before the worker wrote its progress file (準備中)."""
    report = store.get_run(conn, summary.run_id)
    steps = report.steps if report is not None and report.steps else [
        RunPhase(key=k, label=label) for k, label in PHASES]
    return RunProgress(
        run_id=summary.run_id, status=summary.status, reason=summary.reason,
        started_by=summary.started_by, trigger_text=summary.trigger_text,
        started_at=summary.started_at, eta_initial_s=summary.eta_initial_s,
        range=report.range if report is not None else RunRange(),
        versions=report.versions if report is not None else RunVersions(),
        changes=report.changes if report is not None else RunChanges(),
        phases=steps)


def _selfcheck_differs(conn: sqlite3.Connection) -> bool:
    """The LATEST completed self-check (scope ``selfcheck``, a resumed one included) found a
    difference (review E #1): the chip keeps 自檢發現差異 through the nightly runs that follow,
    until a later self-check comes back clean. A self-check that failed or was cancelled
    checked nothing, so the one before it still stands."""
    try:
        row = conn.execute(
            "SELECT run_id FROM settle_run WHERE status IN ('published', 'partial') AND "
            "json_extract(report, '$.inputs.scope') = 'selfcheck' "
            "ORDER BY run_id DESC LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return False
    if row is None:
        return False
    report = store.get_run(conn, int(row[0]))
    return report is not None and report.summary.selfcheck_diff


def status(conn: sqlite3.Connection, *, viewer: str | None, now: datetime,
           reporting: Currency) -> SettlementStatus:
    """``GET /api/settlement/status`` (spec §15). Reads only: the store, the trail, the run
    reports, ``job_runs`` / ``schedule_config`` (``final_through``), the progress file and a
    non-blocking probe of the settle lock. Never writes, never starts or resumes anything."""
    if not flags.settle_enabled():
        return SettlementStatus(state="off")
    state = store.store_state(conn, reporting=reporting)
    today = _today(now)
    window = store.window_start(today)
    first_live = store.live_from(state, today) if state.usable else None
    stale = store.stale_range(state, today)
    try:
        active = store.active_or_unfinished_runs(conn)
    except ValueError:  # a report that no longer reads: the next start ends it
        logger.warning("an active settlement report no longer reads", exc_info=True)
        active = []
    current = active[-1] if active else None
    run_state: SettleState | None = None
    progress: RunProgress | None = None
    slow = False
    if current is not None:
        if current.status == "preparing":
            run_state = "preparing"
        elif _in_process_alive(current.run_id) or proc_lock.is_held(_lock_path()):
            run_state = "running"
        else:
            run_state = "interrupted"
        progress = read_progress(current.run_id) or _synth_progress(conn, current)
        progress = _render_progress(progress, viewer)
        if run_state == "running":
            ref = progress.heartbeat or current.started_at
            if ref.tzinfo is None:
                ref = ref.replace(tzinfo=_TPE)
            slow = (now - ref).total_seconds() > SLOW_AFTER_S
    page = store.pending_changes(conn, limit=0)
    pending = PendingChanges(tables=page.tables, records_total=page.total_count)
    has_pending = bool(page.total_count or page.counters)
    # changes committed while a run computes (「完成後接著處理」); before its claim they are
    # still the ones it will take
    queued = pending if run_state == "running" and has_pending else None
    last = _latest_ended(conn)
    try:
        change_runs = store.list_runs(conn, limit=1, only_changes=True).items
    except ValueError:
        change_runs = []
    last_change = change_runs[0] if change_runs else None
    name: SettleState
    if run_state is not None:
        name = run_state
    elif last is not None and last.status == "failed":
        name = "failed"
    elif last is not None and last.status == "interrupted" and not last.resumed:
        name = "interrupted"
    elif _selfcheck_differs(conn):
        name = "selfcheck_diff"
    elif state.dirty_day is not None or has_pending or (
            not state.usable and store.first_ledger_day(conn) is not None):
        name = "pending"
    else:
        name = "clean"
    ft = final_through(conn, now)
    needs_prompt = (current is None and state.usable and state.dirty_from is not None
                    and state.dirty_from < window)
    prompt: SettlePrompt | None = None
    if needs_prompt and state.dirty_from is not None:
        through = max(d for d in (ft.through, state.settled_through, state.held_through)
                      if d is not None)
        days = max(0, (through - state.dirty_from).days + 1)
        prompt = SettlePrompt.model_validate(
            {"from": state.dirty_from, "days": days, "est_s": _estimate_s(conn, days)})
    return SettlementStatus(
        state=name, generation=state.generation, settled_through=state.settled_through,
        calc_ok=state.calc_ok, dirty_from=state.dirty_from, live_from=first_live,
        stale_from=stale[0] if stale else None, stale_to=stale[1] if stale else None,
        needs_prompt=needs_prompt, prompt=prompt, pending=pending, queued=queued,
        final_through=ft.through, run=progress, slow=slow,
        last_run=_render_summary(last, viewer) if last is not None else None,
        last_change_run=_render_summary(last_change, viewer) if last_change is not None
        else None,
        seq=state.seq,
    )


# =============================================================================================
# Report reads for the API (spec §15)
# =============================================================================================


def runs_page(conn: sqlite3.Connection, *, viewer: str | None, limit: int, offset: int,
              only_changes: bool) -> dict[str, Any]:
    page = store.list_runs(conn, limit=limit, offset=offset, only_changes=only_changes)
    return {"rows": [_render_summary(s, viewer).model_dump(mode="json", by_alias=True)
                     for s in page.items], "total_count": page.total_count}


def run_report(conn: sqlite3.Connection, run_id: int, *,
               viewer: str | None) -> dict[str, Any] | None:
    """One run's full report, with the restatements it wrote (§9.6 Result — closes)."""
    report = store.get_run(conn, run_id)
    if report is None:
        return None
    body = _render_report(report, viewer).model_dump(mode="json", by_alias=True)
    body["restatements"] = [r.model_dump(mode="json", by_alias=True)
                            for r in store.restatements_of_run(conn, run_id)]
    return body


def _record_row(record: ChangeRecord, *, viewer: str | None,
                run_id: int | None) -> dict[str, Any]:
    """A trail record with its server-owned words (actor, operation, table, accounts as
    ``{account:<id>}`` tokens, attempts) — the panel only formats."""
    row = record.model_dump(mode="json", by_alias=True)
    row["actor_text"] = who_text(record.actor, viewer) if record.actor else actor_text(None)
    row["op_text"] = CHANGE_OP_ZH.get(record.op, record.op)
    row["table_label"] = CHANGE_TABLE_ZH.get(record.table, record.table)
    accounts = [a for a in (record.old_account_id, record.new_account_id) if a]
    if record.old_account_id and record.new_account_id and \
            record.old_account_id != record.new_account_id:
        row["account_text"] = (f"{account_ref(record.old_account_id)} → "
                               f"{account_ref(record.new_account_id)}")
    else:
        row["account_text"] = account_ref(accounts[0]) if accounts else None
    others = [i for i in record.attempted_run_ids if i != run_id]
    row["attempted_text"] = ("曾於 " + "、".join(f"#{i}" for i in others) + " 嘗試"
                             if others else None)
    row["handed_back"] = (run_id is not None and run_id in record.attempted_run_ids
                          and record.settled_run_id != run_id)
    return row


def changes_page(conn: sqlite3.Connection, *, state: str, viewer: str | None, limit: int,
                 offset: int) -> dict[str, Any] | None:
    """``state = pending`` or ``run:<id>``; ``None`` for a run with no report (pruned)."""
    run_id: int | None = None
    if state == "pending":
        page = store.pending_changes(conn, limit=limit, offset=offset)
    else:
        run_id = int(state.split(":", 1)[1])
        if store.get_run(conn, run_id) is None:
            return None
        page = store.run_changes(conn, run_id, limit=limit, offset=offset)
    return {
        "rows": [_record_row(r, viewer=viewer, run_id=run_id) for r in page.records],
        "total_count": page.total_count,
        "counters": [c.model_dump(mode="json", by_alias=True) for c in page.counters],
        "tables": [t.model_dump(mode="json", by_alias=True) for t in page.tables],
        "handed_back_text": "本次未生效，已退回待結算" if run_id is not None else None,
    }


def diff_rows(conn: sqlite3.Connection, run_id: int, *, field_name: str | None,
              kind: str | None, limit: int, offset: int) -> dict[str, Any] | None:
    if store.get_run(conn, run_id) is None:
        return None
    page = store.diff_page(conn, run_id, field=field_name, kind=kind, limit=limit,
                           offset=offset)
    return {"rows": [r.model_dump(mode="json", by_alias=True) for r in page.items],
            "total_count": page.total_count}


__all__ = [
    "AlreadyRunning",
    "AutoStart",
    "BootPlan",
    "BootStart",
    "CancelResult",
    "CannotCancel",
    "FinalThrough",
    "NotRunning",
    "RunEnd",
    "SettlementError",
    "SettlementOff",
    "Started",
    "after_job_hook",
    "bind_scheduler",
    "build_request",
    "cancel",
    "changes_page",
    "diff_rows",
    "final_through",
    "notify_failure",
    "on_boot",
    "progress_path",
    "read_progress",
    "render_trigger",
    "run_detail",
    "run_report",
    "runs_page",
    "scheduled_run",
    "send_pending_notifications",
    "settle_after_job",
    "shutdown",
    "start_after_boot",
    "start_automatic",
    "start_run",
    "status",
    "trigger_outcome",
    "trigger_text",
    "who_text",
]
