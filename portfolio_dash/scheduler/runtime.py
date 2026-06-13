"""APScheduler wiring: build cron-triggered jobs from the DB schedule config.

Triggers only. Each scheduled action calls ``trigger_job`` (which opens its own
DB connection), so jobs are independent and a single failure is logged, not fatal.
"""

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import BaseScheduler
from apscheduler.triggers.cron import CronTrigger

from portfolio_dash.scheduler.jobs import ensure_scheduler_seeded, trigger_job
from portfolio_dash.shared.db import session


def build_scheduler() -> BackgroundScheduler:
    """Build a scheduler with a cron trigger per enabled ``schedule_config`` row."""
    scheduler = BackgroundScheduler()
    with session() as conn:
        ensure_scheduler_seeded(conn)
        rows = conn.execute(
            "SELECT job_id, enabled, cron, timezone FROM schedule_config"
        ).fetchall()
    for row in rows:
        if not row["enabled"]:
            continue
        trigger = CronTrigger.from_crontab(row["cron"], timezone=row["timezone"])
        scheduler.add_job(
            trigger_job,
            trigger,
            args=[row["job_id"]],
            id=row["job_id"],
            replace_existing=True,
        )
    return scheduler


def start() -> BackgroundScheduler:
    """Build and start the background scheduler."""
    scheduler = build_scheduler()
    scheduler.start()
    return scheduler


def shutdown(scheduler: BackgroundScheduler) -> None:
    """Stop the scheduler."""
    scheduler.shutdown(wait=False)


def reschedule_job(
    scheduler: BaseScheduler | None, job_id: str, *, cron: str, tz: str, enabled: bool
) -> None:
    """Apply a schedule change to the live scheduler immediately.

    A no-op when ``scheduler`` is None (e.g. ``PD_DISABLE_SCHEDULER=1`` in tests / when
    the scheduler is not running). Disabled jobs are removed; enabled jobs are (re)added
    with ``replace_existing`` so an existing trigger is updated in place.
    """
    if scheduler is None:
        return
    if not enabled:
        if scheduler.get_job(job_id) is not None:
            scheduler.remove_job(job_id)
        return
    trigger = CronTrigger.from_crontab(cron, timezone=tz)
    scheduler.add_job(trigger_job, trigger, args=[job_id], id=job_id, replace_existing=True)
