"""One scheduled job in a child process (PERF-D4; spec §16; plan §7.2).

Started by ``scheduler/jobs.py`` through ``shared/child_runner.py`` (heavy: one heavy child
at a time across the settlement and the jobs)::

    python -m portfolio_dash.api.job_worker <job_id> <run_id> --now <iso> \
        --trigger schedule|manual [--tz <zone>]

The parent has already inserted and committed the run's ``running`` ``job_runs`` row. This
process, in order:

1. loads the test stubs (``PD_TEST_STUBS``, refused unless ``PD_TEST_MODE=1``) and lowers its
   own priority (``nice 19``, ``SCHED_IDLE`` on Linux; a no-op on Windows);
2. keeps every nested dispatch in-process (``PD_JOB_MODE=inline``): a job that runs another
   (the alert scan's on_alert cards) must not ask the heavy gate its own parent holds for it;
3. takes ``<data>/job-<run_id>.lock`` with ``proc_lock.lock`` — never ``try_lock``, since a
   status probe takes the lock for an instant — and holds it for its whole life, so a boot of
   a new web process can tell a live worker's row from an abandoned one;
4. binds the runtime (``api/composition.bind_runtime``, the seams the app binds) and then
   CLEARS the after-job hook: the hook belongs to the web process, where the settlement
   service lives (plan P7), and the parent calls it once this process is gone;
5. appends its log records to ``app.log`` without ever rotating it (:func:`attach_append_log`)
   and writes its progress to ``<data>/job-<run_id>.progress.json`` (``set_progress``), which
   the parent merges into the 排程中心's status;
6. runs the job on its row (``scheduler.jobs.run_in_worker`` → the body of
   ``run_job_outcome``), so ``job_runs``, ``usage_tally`` and ``JobOutcome`` are the
   in-process run's.

Exit status: 0 when the row is final, 1 when it is not (the parent then finalises it
「工作子程序異常結束」), 2 when the run's lock is held by another process.
"""

from __future__ import annotations

import argparse
import logging
import os
from collections.abc import Sequence
from pathlib import Path

from portfolio_dash.scheduler import jobs
from portfolio_dash.shared import proc_lock
from portfolio_dash.shared.child_runner import apply_test_stubs_from_env, lower_own_priority
from portfolio_dash.shared.config import get_settings
from portfolio_dash.shared.db import install_actor, job_actor, session
from portfolio_dash.shared.logging_config import JsonLinesFormatter

log = logging.getLogger(__name__)

#: Exit codes (the parent reads only "zero or not"; the numbers are for the log).
EXIT_DONE = 0
EXIT_NOT_FINAL = 1
EXIT_LOCKED = 2

#: The handler's name — distinct from the web process's rotating handler
#: (``shared/logging_config.configure_logging``), which this process never attaches.
APPEND_HANDLER_NAME = "portfolio_dash.jsonlines.job_worker"


def attach_append_log(log_dir: Path | None = None) -> logging.Handler:
    """Append this process's records to the web process's ``<data>/logs/app.log`` — the same
    JSON lines the in-process run wrote there — through a plain ``FileHandler``: append
    only, never a rollover.

    Not ``configure_logging``: its ``RotatingFileHandler`` would make this a SECOND process
    rotating one file, and two rotators rename each other's file away (package H review,
    2026-10-20). Not stderr either: the runner keeps only the last 16 KiB of it and logs that
    only when the child fails, so a run that succeeded would lose its INFO lines, which the
    in-process run wrote to ``app.log``. Appending from two processes is safe for single JSON
    lines; when the web process rotates while this child runs, the child's lines land in the
    renamed file (POSIX) or the rename waits until the child has closed it (Windows), and the
    child lives for one job.
    """
    target = log_dir if log_dir is not None else get_settings().db_path.parent / "logs"
    target.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(target / "app.log", mode="a", encoding="utf-8")
    handler.name = APPEND_HANDLER_NAME
    handler.setFormatter(JsonLinesFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    return handler


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="job_worker")
    parser.add_argument("job_id")
    parser.add_argument("run_id", type=int)
    parser.add_argument("--now", required=True)
    parser.add_argument("--trigger", choices=("schedule", "manual"), required=True)
    parser.add_argument("--tz", default=None)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """The child's entry (see the module docstring for the order)."""
    args = _parse(argv)
    apply_test_stubs_from_env()
    lower_own_priority()
    os.environ["PD_JOB_MODE"] = "inline"
    attach_append_log()
    data = proc_lock.data_dir()
    try:
        held = proc_lock.lock(proc_lock.job_lock_path(args.run_id, data),
                              timeout=jobs.WORKER_LOCK_TIMEOUT_S)
    except TimeoutError:
        log.error("job worker: the lock of run %s is held by another process", args.run_id)
        return EXIT_LOCKED
    with held:
        # Imported here, after the stubs: the composition root reads each runner off its
        # module when it binds, so a stub that patched a module is what gets bound.
        from portfolio_dash.api import composition

        composition.bind_runtime()
        jobs.register_after_job_hook(None)
        jobs.enable_progress_sidecar(args.job_id, args.run_id,
                                     jobs.progress_sidecar_path(args.run_id, data))
        now = jobs.parse_worker_now(args.now, args.tz)
        trigger: jobs.TriggerSourceArg = "schedule" if args.trigger == "schedule" else "manual"
        with session() as conn:
            # The job's writes reach the settlement store's trail as ``job:<id>`` (spec §7),
            # exactly as the cron session and the manual worker stamp them.
            install_actor(conn, job_actor(args.job_id))
            final = jobs.run_in_worker(conn, args.job_id, args.run_id, now=now,
                                       trigger=trigger)
    return EXIT_DONE if final else EXIT_NOT_FINAL


if __name__ == "__main__":  # pragma: no cover - the child process entry
    raise SystemExit(main())
