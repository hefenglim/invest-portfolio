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
from portfolio_dash.scheduler.jobs import QUOTE_JOB_IDS, migrate_job_runs_results

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
    # a refresh earlier the same day does not answer the fire; one after it does
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
