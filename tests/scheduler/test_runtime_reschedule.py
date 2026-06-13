from apscheduler.schedulers.background import BackgroundScheduler

from portfolio_dash.scheduler.runtime import reschedule_job


def test_reschedule_adds_and_updates_job() -> None:
    sch = BackgroundScheduler()
    reschedule_job(sch, "quotes_tw", cron="0 14 * * mon-fri", tz="Asia/Taipei", enabled=True)
    assert sch.get_job("quotes_tw") is not None
    reschedule_job(
        sch, "quotes_tw", cron="30 17 * * mon-fri", tz="Asia/Kuala_Lumpur", enabled=True
    )
    assert sch.get_job("quotes_tw") is not None  # replaced, still present


def test_reschedule_disabled_removes_job() -> None:
    sch = BackgroundScheduler()
    reschedule_job(sch, "quotes_tw", cron="0 14 * * mon-fri", tz="Asia/Taipei", enabled=True)
    reschedule_job(sch, "quotes_tw", cron="0 14 * * mon-fri", tz="Asia/Taipei", enabled=False)
    assert sch.get_job("quotes_tw") is None


def test_reschedule_none_scheduler_is_noop() -> None:
    # no raise when the scheduler is None (PD_DISABLE_SCHEDULER=1 / not running)
    reschedule_job(None, "quotes_tw", cron="0 14 * * mon-fri", tz="Asia/Taipei", enabled=True)
