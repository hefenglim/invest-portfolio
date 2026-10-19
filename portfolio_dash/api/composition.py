"""The runtime bindings of the composition root (PERF spec §16, plan §7.2 / P13).

``scheduler/`` reaches every computation that lives above it — the insight runner, the
held-symbol readers, the alert compute, the digest, the settlement — through ``register_*``
seams (``architecture.md``: the binder is the layer above both). :func:`bind_runtime` binds
all of them, in ONE place, for the two processes that run jobs:

- the web process — the app's lifespan calls it once the boot session has committed;
- a job worker child (``api/job_worker.py``) — it calls it before running its job, so a job
  runs against the same runners in a child as in the web process (spec §16: "binds the same
  injection seams as the app").

Each runner is read off its MODULE at call time (``insight_service.run_for_id``, never a
name imported into this module), so a test-stub module loaded before the binding
(``PD_TEST_STUBS``, ``shared/child_runner.py``) can replace one by patching its module.

Not here, because they are not seams a job reaches (they stay in the lifespan):
``settlement_service.bind_scheduler`` (the live scheduler, web process only) and
``settlement_service.start_after_boot`` (the boot run).

``register_snapshot_runner`` is still bound: ``snapshot_monthly`` is retired (plan P11) but
stays in ``JOBS`` until W4 removes it, and its seam with it.
"""

from __future__ import annotations

from portfolio_dash.api import (
    alert_inputs,
    digest_service,
    dividend_inbox,
    fundamentals_service,
    insight_service,
    news_service,
    settlement_service,
    signals_service,
    snapshots,
)
from portfolio_dash.api.routers import actions
from portfolio_dash.scheduler import jobs

#: The job worker's module (``python -m <module> <job_id> <run_id> ...``). Named here, not in
#: ``scheduler/``, which never names an ``api`` module.
JOB_WORKER_MODULE = "portfolio_dash.api.job_worker"


def bind_runtime() -> None:
    """Bind every ``scheduler.jobs`` seam to its api-side implementation (idempotent).

    ``tests/scheduler/test_jobs_in_child.py`` holds that every ``register_*`` function of
    ``scheduler/jobs.py`` is called here, and that each seam is bound afterwards — a new
    seam that is not bound fails there, not as a silently idle job in production."""
    # Wire the kind=insight scheduler dispatch + manual-run daemon to the api service seam
    # (scheduler triggers only; it never imports api — spec 04.2 / architecture.md).
    jobs.register_insight_runner(insight_service.run_for_id)
    # DEF-041: the on_alert 持倉提點 card is dispatched only for HELD symbols; "held" is the
    # computed book's answer, which scheduler/ cannot replay — bound here, next to the runner.
    jobs.register_alert_held_fn(insight_service.held_symbols_for_alerts)
    # The Loop-2/3 evolution runners (price-/master-bearing reads live in the api seam).
    jobs.register_evaluation_runner(insight_service.evaluate_due)
    jobs.register_calibration_runner(insight_service.generate_calibrations_for_all)
    # 待確認匯入 daily scan (R5): full scan (events + pending count) via the api seam.
    jobs.register_dividend_scan_runner(dividend_inbox.scan_job)
    # 月度快照 (R6 item 8): retired by plan P11; the seam goes with the job at W4.
    jobs.register_snapshot_runner(snapshots.snapshot_job)
    # News pipeline (batch ④): nightly fetch + AI-organize into the separate news DB.
    jobs.register_news_runner(news_service.run_news_daily)
    # Digests (P3 batch 3): daily close summary + weekly action list (assemble + store + push).
    jobs.register_digest_runner(digest_service.run_digest)
    # Rule-signal scan (P2 batch 2): held-symbol signal evaluation + transition events.
    jobs.register_signal_scan_runner(signals_service.scan_signals)
    # Alert-compute (P3 batch 2): the FULL rule set (market-risk rules need pricing/consensus
    # reads that live in the api seam) — the scheduler's alert_scan runs through this.
    jobs.register_alert_compute_runner(alert_inputs.scan_alert_compute)
    # Fundamentals AV leg (W3, AI-D16): the Saturday job covers HELD symbols only; the
    # held set is computed here (scheduler/ + pricing/ cannot replay the book).
    jobs.register_fundamentals_runner(fundamentals_service.run_fundamentals_av)
    # Quote-job partial threshold (M10-02): "did a HELD instrument fail?" — the held set is
    # computed here for the same reason as the line above. Unregistered, the verdict counts
    # every lost instrument (over-reports, never hides).
    jobs.register_held_symbols_fn(actions.held_symbols)
    # The settlement (PERF spec 2026-10-18 §9.1, §15): the nightly / weekly jobs run through
    # the service, and every other job's end may chain a run (the after-job hook). A job
    # worker child clears the hook again: the hook runs in the web process (plan P7).
    jobs.register_settlement_runner(settlement_service.scheduled_run)
    jobs.register_after_job_hook(settlement_service.after_job_hook)
    # Job workers (PERF-D4, spec §16): with PD_JOB_MODE=child a scheduled job runs in
    # `python -m portfolio_dash.api.job_worker`, which calls this function again.
    jobs.register_job_worker(JOB_WORKER_MODULE)
