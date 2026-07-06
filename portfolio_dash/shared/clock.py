"""Single business-day-anchor clock (decision Q6, 2026-07-07).

Asia/Taipei (``Settings.tz_display``) is the ONLY day anchor for business logic:
insight cache fingerprints, backup filenames, news day-walks, and ``job_runs``
timestamps. The scheduler's old ``datetime.now(UTC)`` was a bug — between 00:00 and
07:59 Taipei the two clocks disagreed on "today", so a cron run and a manual run of
the SAME trading day produced different day-anchored fingerprints (duplicate cards,
double cost).

``shared/`` depends on nothing internal except itself, so every layer (scheduler,
api, ops) may import this helper; the api's ``deps.get_now`` delegates here.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from portfolio_dash.shared.config import get_settings


def app_now() -> datetime:
    """The current time in the application (business day-anchor) timezone."""
    return datetime.now(ZoneInfo(get_settings().tz_display))
