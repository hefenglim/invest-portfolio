"""``final_through`` (spec §9.1): the last day a run may settle, from the schedule and the quote
jobs' verdicts — never from the store.

A day D qualifies when every market quote job DUE on D (its cron, enabled, the scheduler on,
judged by the date in the job's own timezone, from its first ``job_runs`` row on) ended
``ok``; a day older than 4 days qualifies regardless and lists its run's ``held_failed``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.api import settlement_service as service
from portfolio_dash.pricing import store as price_store
from portfolio_dash.scheduler.jobs import (
    QUOTE_JOB_IDS,
    migrate_job_runs_results,
    update_schedule,
)

TPE = ZoneInfo("Asia/Taipei")
NY = ZoneInfo("America/New_York")
KL = ZoneInfo("Asia/Kuala_Lumpur")
_CRON = {"quotes_tw": ("0 14 * * mon-fri", TPE), "quotes_us": ("30 16 * * mon-fri", NY),
         "quotes_my": ("30 17 * * mon-fri", KL)}


@pytest.fixture
def conn(golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
         ) -> Iterator[sqlite3.Connection]:
    monkeypatch.delenv("PD_DISABLE_SCHEDULER", raising=False)
    migrate_job_runs_results(golden_db)
    for job_id in QUOTE_JOB_IDS:
        cron, tz = _CRON[job_id]
        golden_db.execute("INSERT OR REPLACE INTO schedule_config (job_id, enabled, cron, "
                          "timezone) VALUES (?, 1, ?, ?)", (job_id, cron, str(tz)))
    golden_db.commit()
    yield golden_db


def _run(conn: sqlite3.Connection, job_id: str, at: datetime, status: str = "ok",
         held_failed: list[str] | None = None) -> None:
    results = None if held_failed is None else json.dumps({"held_failed": held_failed})
    conn.execute("INSERT INTO job_runs (job_id, started_at, finished_at, status, detail, "
                 "results) VALUES (?, ?, ?, ?, 'd', ?)",
                 (job_id, at.isoformat(), (at + timedelta(minutes=1)).isoformat(), status,
                  results))
    conn.commit()


def _weekdays(first: date, last: date) -> list[date]:
    return [first + timedelta(days=i) for i in range((last - first).days + 1)
            if (first + timedelta(days=i)).weekday() < 5]


def _ok_every_fire(conn: sqlite3.Connection, first: date, last: date, *,
                   jobs: tuple[str, ...] = ("quotes_tw", "quotes_us"),
                   except_: dict[tuple[str, date], tuple[str, list[str]]] | None = None) -> None:
    for day in _weekdays(first, last):
        for job_id in jobs:
            cron, tz = _CRON[job_id]
            hour, minute = (int(cron.split()[1]), int(cron.split()[0]))
            at = datetime(day.year, day.month, day.day, hour, minute, 2, tzinfo=tz)
            status, held = (except_ or {}).get((job_id, day), ("ok", []))
            _run(conn, job_id, at, status, held)


def _disable(conn: sqlite3.Connection, job_id: str) -> None:
    conn.execute("UPDATE schedule_config SET enabled = 0 WHERE job_id = ?", (job_id,))
    conn.commit()


def test_the_stale_age_is_the_live_one() -> None:
    assert service.STALE_AGE_DAYS == price_store._DEFAULT_MAX_AGE


def test_a_weekend_never_blocks(conn: sqlite3.Connection) -> None:
    _disable(conn, "quotes_my")
    _ok_every_fire(conn, date(2026, 8, 3), date(2026, 8, 7))
    monday = datetime(2026, 8, 10, 6, 40, tzinfo=TPE)
    assert service.final_through(conn, monday) == service.FinalThrough(through=date(2026, 8, 9))


def test_nothing_is_due_before_go_live_or_with_the_scheduler_off(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 12, 6, 40, tzinfo=TPE)
    assert service.final_through(conn, now).through == date(2026, 8, 11)  # no row: pre-go-live
    _run(conn, "quotes_tw", datetime(2026, 8, 11, 14, 0, 2, tzinfo=TPE), "partial", ["2330"])
    assert service.final_through(conn, now).through == date(2026, 8, 10)
    monkeypatch.setenv("PD_DISABLE_SCHEDULER", "1")
    assert service.final_through(conn, now).through == date(2026, 8, 11)


def test_a_disabled_market_job_is_never_due(conn: sqlite3.Connection) -> None:
    _ok_every_fire(conn, date(2026, 8, 3), date(2026, 8, 11), jobs=("quotes_tw", "quotes_us"))
    _run(conn, "quotes_my", datetime(2026, 8, 3, 17, 30, 2, tzinfo=KL))  # went live, then…
    now = datetime(2026, 8, 12, 6, 40, tzinfo=TPE)
    assert service.final_through(conn, now).blocked_jobs == ("quotes_my",)
    _disable(conn, "quotes_my")  # …the owner switched it off
    assert service.final_through(conn, now).through == date(2026, 8, 11)


def test_a_partial_friday_month_end_blocks_until_it_is_older_than_four_days(
    conn: sqlite3.Connection,
) -> None:
    _disable(conn, "quotes_my")
    friday = date(2026, 7, 31)
    _ok_every_fire(conn, date(2026, 7, 27), date(2026, 8, 7),
                   except_={("quotes_tw", friday): ("partial", ["2330"])})
    monday = datetime(2026, 8, 3, 6, 40, tzinfo=TPE)
    blocked = service.final_through(conn, monday)
    assert blocked.through == date(2026, 7, 30) and blocked.blocked_on == friday
    assert blocked.blocked_jobs == ("quotes_tw",)
    day_four = service.final_through(conn, datetime(2026, 8, 4, 6, 40, tzinfo=TPE))
    assert day_four.through == date(2026, 7, 30)  # 4 days old: still blocked
    day_five = service.final_through(conn, datetime(2026, 8, 5, 6, 40, tzinfo=TPE),
                                     since=date(2026, 7, 1))
    assert day_five.through == date(2026, 8, 4) and day_five.blocked_on is None
    assert day_five.unconfirmed_closes == {friday: ["2330"]}


def test_a_permanently_unquoted_held_symbol_never_blocks_more_than_four_days(
    conn: sqlite3.Connection,
) -> None:
    _disable(conn, "quotes_my")
    days = _weekdays(date(2026, 8, 3), date(2026, 8, 14))
    _ok_every_fire(conn, days[0], days[-1],
                   except_={("quotes_tw", d): ("partial", ["9999"]) for d in days})
    now = datetime(2026, 8, 15, 6, 40, tzinfo=TPE)  # Saturday
    result = service.final_through(conn, now, since=date(2026, 8, 1))
    # 08-11 .. 08-14 are within 4 days and partial; 08-10 and older settle with the symbol
    assert result.blocked_on == date(2026, 8, 11) and result.through == date(2026, 8, 10)
    assert result.unconfirmed_closes == {d: ["9999"] for d in days if d <= date(2026, 8, 10)}


def test_the_us_close_is_judged_by_its_new_york_date(conn: sqlite3.Connection) -> None:
    _disable(conn, "quotes_my")
    _ok_every_fire(conn, date(2026, 8, 3), date(2026, 8, 7))
    _run(conn, "quotes_tw", datetime(2026, 8, 10, 14, 0, 2, tzinfo=TPE))
    # Tuesday 03:00 Taipei = Monday 15:00 New York: Monday's US fire has not happened yet
    early = service.final_through(conn, datetime(2026, 8, 11, 3, 0, tzinfo=TPE))
    assert early.blocked_on == date(2026, 8, 10) and early.blocked_jobs == ("quotes_us",)
    _run(conn, "quotes_us", datetime(2026, 8, 10, 16, 30, 2, tzinfo=NY))
    later = service.final_through(conn, datetime(2026, 8, 11, 6, 40, tzinfo=TPE))
    assert later.through == date(2026, 8, 10)


def test_a_same_day_rerun_after_a_failed_fire_counts(conn: sqlite3.Connection) -> None:
    _disable(conn, "quotes_my")
    _ok_every_fire(conn, date(2026, 8, 3), date(2026, 8, 6))
    _run(conn, "quotes_us", datetime(2026, 8, 7, 16, 30, 2, tzinfo=NY))
    _run(conn, "quotes_tw", datetime(2026, 8, 7, 14, 0, 2, tzinfo=TPE), "partial", ["2330"])
    monday = datetime(2026, 8, 10, 6, 40, tzinfo=TPE)
    assert service.final_through(conn, monday).through == date(2026, 8, 6)
    # a refresh earlier the same day (an ok intraday read, written AFTER the fire's row) does
    # not answer the fire …
    _run(conn, "quotes_tw", datetime(2026, 8, 7, 10, 0, tzinfo=TPE))
    assert service.final_through(conn, monday).through == date(2026, 8, 6)
    # … one after it does
    _run(conn, "quotes_tw", datetime(2026, 8, 7, 15, 30, tzinfo=TPE))
    assert service.final_through(conn, monday).through == date(2026, 8, 9)


def test_a_fire_whose_job_wrote_no_row_settles_after_four_days_with_an_empty_list(
    conn: sqlite3.Connection,
) -> None:
    _disable(conn, "quotes_my")
    _ok_every_fire(conn, date(2026, 8, 3), date(2026, 8, 14), jobs=("quotes_us",))
    _ok_every_fire(conn, date(2026, 8, 3), date(2026, 8, 14), jobs=("quotes_tw",))
    conn.execute("DELETE FROM job_runs WHERE job_id = 'quotes_tw' AND started_at LIKE "
                 "'2026-08-05%'")
    conn.commit()
    result = service.final_through(conn, datetime(2026, 8, 15, 6, 40, tzinfo=TPE),
                                   since=date(2026, 8, 1))
    assert result.through == date(2026, 8, 14)
    assert result.unconfirmed_closes == {date(2026, 8, 5): []}


def _seed_14h(conn: sqlite3.Connection) -> None:
    """quotes_tw alone, at 14:00 every weekday from July: ok, but the month end is partial."""
    _disable(conn, "quotes_us")
    _disable(conn, "quotes_my")
    _ok_every_fire(conn, date(2026, 7, 1), date(2026, 8, 14), jobs=("quotes_tw",),
                   except_={("quotes_tw", date(2026, 7, 31)): ("partial", ["2330"])})


def test_a_schedule_change_never_rewrites_history(conn: sqlite3.Connection) -> None:
    """Review E #2 (the cron-change probe): moving quotes_tw from 14:00 to 14:30 must neither
    block four days nor mark every older weekday unconfirmed, nor lose 07-31's real list."""
    _seed_14h(conn)
    now = datetime(2026, 8, 15, 6, 40, tzinfo=TPE)
    before = service.final_through(conn, now, since=service.SINCE_GO_LIVE)
    assert before.through == date(2026, 8, 14) and before.blocked_on is None
    assert before.unconfirmed_closes == {date(2026, 7, 31): ["2330"]}
    changed = update_schedule(conn, "quotes_tw", cron="30 14 * * mon-fri", tz="Asia/Taipei",
                              enabled=True, now=datetime(2026, 8, 14, 20, 0, tzinfo=TPE))
    assert changed
    assert service.final_through(conn, now, since=service.SINCE_GO_LIVE) == before
    # after the change the new cron judges: a fire at 14:30 that wrote no row blocks its day
    _run(conn, "quotes_tw", datetime(2026, 8, 17, 14, 0, 2, tzinfo=TPE))  # the OLD time
    later = service.final_through(conn, datetime(2026, 8, 18, 6, 40, tzinfo=TPE))
    assert later.blocked_on == date(2026, 8, 17)
    _run(conn, "quotes_tw", datetime(2026, 8, 17, 14, 30, 2, tzinfo=TPE))
    assert service.final_through(conn, datetime(2026, 8, 18, 6, 40, tzinfo=TPE)).through == \
        date(2026, 8, 17)


def test_an_unchanged_save_stamps_nothing(conn: sqlite3.Connection) -> None:
    _seed_14h(conn)
    assert not update_schedule(conn, "quotes_tw", cron="0 14 * * mon-fri", tz="Asia/Taipei",
                               enabled=True, now=datetime(2026, 8, 14, 20, 0, tzinfo=TPE))
    stamp = conn.execute("SELECT updated_at FROM schedule_config WHERE job_id = 'quotes_tw'"
                         ).fetchone()[0]
    assert stamp is None


def test_a_job_switched_on_again_does_not_block_its_quiet_weeks(
    conn: sqlite3.Connection,
) -> None:
    """Disabled in late July, enabled again on 08-14: the weeks it was off were never due."""
    _disable(conn, "quotes_us")
    _disable(conn, "quotes_my")
    _ok_every_fire(conn, date(2026, 7, 1), date(2026, 7, 24), jobs=("quotes_tw",))
    _ok_every_fire(conn, date(2026, 8, 14), date(2026, 8, 14), jobs=("quotes_tw",))
    update_schedule(conn, "quotes_tw", cron="0 14 * * mon-fri", tz="Asia/Taipei",
                    enabled=False, now=datetime(2026, 7, 25, 9, 0, tzinfo=TPE))
    update_schedule(conn, "quotes_tw", cron="0 14 * * mon-fri", tz="Asia/Taipei",
                    enabled=True, now=datetime(2026, 8, 14, 9, 0, tzinfo=TPE))
    result = service.final_through(conn, datetime(2026, 8, 15, 6, 40, tzinfo=TPE),
                                   since=service.SINCE_GO_LIVE)
    assert result.through == date(2026, 8, 14) and result.unconfirmed_closes == {}


def test_a_run_request_carries_every_mark_since_go_live(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review E #10: the request's unconfirmed closes cover every quote run since go-live,
    never only the days the run could start from when it was requested."""
    _seed_14h(conn)
    conn.execute("UPDATE settle_meta SET generation = 1, settled_through = '2026-08-10', "
                 "held_through = '2026-08-10', calc_version = 'x', reporting_ccy = 'TWD' "
                 "WHERE id = 1")
    conn.commit()
    request = service.build_request(conn, scope="dirty", reason="prompt", started_by=None,
                                    now=datetime(2026, 8, 15, 6, 40, tzinfo=TPE))
    assert request.unconfirmed_closes == {date(2026, 7, 31): ["2330"]}
    assert request.through == date(2026, 8, 14)


def test_a_hand_edited_schedule_is_judged_by_what_ran(conn: sqlite3.Connection) -> None:
    """The reviewer's cron-change probe edits schedule_config by hand: the schedule no longer
    matches its stamp, so history is judged by what ran (never by the new cron), and the next
    boot stamps the edit."""
    _disable(conn, "quotes_us")
    _disable(conn, "quotes_my")
    migrate_job_runs_results(conn)  # the boot: stamps today's schedules
    _seed_14h(conn)
    now = datetime(2026, 8, 15, 6, 40, tzinfo=TPE)
    before = service.final_through(conn, now, since=service.SINCE_GO_LIVE)
    conn.execute("UPDATE schedule_config SET cron = '30 14 * * mon-fri' "
                 "WHERE job_id = 'quotes_tw'")
    conn.commit()
    after = service.final_through(conn, now, since=service.SINCE_GO_LIVE)
    assert after == before
    assert after.unconfirmed_closes == {date(2026, 7, 31): ["2330"]}
    migrate_job_runs_results(conn)  # the next boot stamps the edit
    row = conn.execute("SELECT updated_at, stamped_schedule FROM schedule_config "
                       "WHERE job_id = 'quotes_tw'").fetchone()
    assert row[0] is not None and row[1] == "30 14 * * mon-fri|Asia/Taipei|1"
