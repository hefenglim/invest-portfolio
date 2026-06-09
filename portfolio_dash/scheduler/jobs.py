"""Scheduler job registry, DB schedule config, and run log.

`scheduler/` triggers `pricing` (and later `llm_insight`) only — it holds no
business logic. This module is import-safe without APScheduler so it is fully
unit-testable; the APScheduler wiring lives in ``runtime.py``.
"""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from portfolio_dash.pricing.defaults import default_registry
from portfolio_dash.pricing.refresh import refresh_dividends, refresh_history, refresh_quotes
from portfolio_dash.pricing.refs import FxPair, InstrumentRef
from portfolio_dash.pricing.results import RefreshSummary
from portfolio_dash.shared import config_store
from portfolio_dash.shared.db import session
from portfolio_dash.shared.enums import Currency, Market

# A job does its own trigger+wiring and returns a short run summary for job_runs.detail.
JobFunc = Callable[..., str]


@dataclass(frozen=True)
class JobSpec:
    id: str
    func: JobFunc
    default_cron: str
    default_timezone: str
    default_enabled: bool
    description: str


_DDL = """
CREATE TABLE IF NOT EXISTS schedule_config (
    job_id TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    cron TEXT NOT NULL,
    timezone TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT,
    detail TEXT
);
"""


def create_scheduler_tables(conn: sqlite3.Connection) -> None:
    """Create the scheduler tables idempotently."""
    conn.executescript(_DDL)
    conn.commit()


def ensure_job_rows(conn: sqlite3.Connection) -> None:
    """Insert a default ``schedule_config`` row for any registered job that lacks one.

    Idempotent (``INSERT OR IGNORE``): seeds all jobs on first run and adds rows for
    newly-registered jobs on later runs, while leaving existing (possibly user-edited)
    rows untouched.
    """
    for job in JOBS:
        conn.execute(
            "INSERT OR IGNORE INTO schedule_config (job_id, enabled, cron, timezone) "
            "VALUES (?, ?, ?, ?)",
            (job.id, 1 if job.default_enabled else 0, job.default_cron, job.default_timezone),
        )
    conn.commit()


def ensure_scheduler_seeded(conn: sqlite3.Connection) -> None:
    """Create scheduler tables (once) and ensure a default row per registered job (always)."""
    config_store.ensure_seeded(
        conn, "scheduler", create=create_scheduler_tables, seed=ensure_job_rows
    )
    ensure_job_rows(conn)  # also run unconditionally so newly-registered jobs get their row


# --- Job registry -------------------------------------------------------------
# Default cron times fall after each exchange's close; users override per job later.

_HISTORY_LOOKBACK_DAYS = 7


def _summarize(summary: RefreshSummary) -> str:
    return f"{len(summary.ok)} ok, {len(summary.failed)} failed"


def refresh_quotes_for(conn: sqlite3.Connection, market: Market, *, now: datetime) -> str:
    """Refresh latest quotes + FX for one market's instruments."""
    instruments, fx_pairs = build_worklist(conn, market)
    summary = refresh_quotes(conn, default_registry(), instruments, fx_pairs, now=now)
    return _summarize(summary)


def quotes_tw(conn: sqlite3.Connection, *, now: datetime) -> str:
    return refresh_quotes_for(conn, Market.TW, now=now)


def quotes_us(conn: sqlite3.Connection, *, now: datetime) -> str:
    return refresh_quotes_for(conn, Market.US, now=now)


def quotes_my(conn: sqlite3.Connection, *, now: datetime) -> str:
    return refresh_quotes_for(conn, Market.MY, now=now)


def history_daily(conn: sqlite3.Connection, *, now: datetime) -> str:
    """Backfill a recent history window for all instruments (deep backfill is manual)."""
    instruments, _ = build_worklist(conn, None)
    start = (now - timedelta(days=_HISTORY_LOOKBACK_DAYS)).date()
    summary = refresh_history(conn, default_registry(), instruments, start, now=now)
    return _summarize(summary)


def dividends_daily(conn: sqlite3.Connection, *, now: datetime) -> str:
    """Sweep dividend/ex-div events for all instruments."""
    instruments, _ = build_worklist(conn, None)
    summary = refresh_dividends(conn, default_registry(), instruments, now=now)
    return _summarize(summary)


JOBS: list[JobSpec] = [
    JobSpec(
        "quotes_tw", quotes_tw, "0 14 * * mon-fri", "Asia/Taipei", True,
        "TW quotes + FX (post-close)",
    ),
    JobSpec(
        "quotes_us", quotes_us, "30 16 * * mon-fri", "America/New_York", True,
        "US quotes + FX (post-close)",
    ),
    JobSpec(
        "quotes_my", quotes_my, "30 17 * * mon-fri", "Asia/Kuala_Lumpur", True,
        "MY quotes + FX (post-close)",
    ),
    JobSpec(
        "history_daily", history_daily, "0 2 * * *", "Asia/Taipei", True,
        "Daily history backfill (recent window)",
    ),
    JobSpec(
        "dividends_daily", dividends_daily, "0 3 * * *", "Asia/Taipei", True,
        "Daily dividend/ex-div sweep",
    ),
]

_DEFAULT_BOARD: dict[Market, str] = {Market.US: "", Market.MY: ".KL", Market.TW: "TWSE"}

# Reporting-currency FX pairs needed for the combined view (reporting ccy = TWD).
_FX_PAIRS: list[FxPair] = [
    FxPair(base=Currency.USD, quote=Currency.TWD),
    FxPair(base=Currency.USD, quote=Currency.MYR),
    FxPair(base=Currency.MYR, quote=Currency.TWD),
]


def build_worklist(
    conn: sqlite3.Connection, market: Market | None
) -> tuple[list[InstrumentRef], list[FxPair]]:
    """Build the pricing work-list from the ``instruments`` table.

    Board comes from the stored ``instruments.board`` column when set, else the
    deterministic market default (US ``""`` / MY ``".KL"`` / TW ``"TWSE"``). FX pairs
    are the fixed reporting-currency set.
    """
    sql = "SELECT symbol, market, board FROM instruments"
    params: tuple[str, ...] = ()
    if market is not None:
        sql += " WHERE market = ?"
        params = (market.value,)
    refs: list[InstrumentRef] = []
    for row in conn.execute(sql, params):
        mkt = Market(row["market"])
        board = row["board"] or _DEFAULT_BOARD[mkt]
        refs.append(InstrumentRef(symbol=row["symbol"], market=mkt, board=board))
    return refs, _FX_PAIRS


def _jobs_by_id() -> dict[str, JobSpec]:
    return {j.id: j for j in JOBS}


def run_job(conn: sqlite3.Connection, job_id: str, *, now: datetime) -> None:
    """Execute one job, logging start/finish to ``job_runs``.

    A job exception is caught and logged as ``status="error"`` (never re-raised), so
    one failing job cannot crash the scheduler or other jobs.
    """
    spec = _jobs_by_id()[job_id]
    cur = conn.execute(
        "INSERT INTO job_runs (job_id, started_at) VALUES (?, ?)",
        (job_id, now.isoformat()),
    )
    run_id = cur.lastrowid
    conn.commit()
    try:
        detail = spec.func(conn, now=now)
        status = "ok"
    except Exception as exc:  # noqa: BLE001 — swallow + log; never crash the scheduler
        detail, status = str(exc), "error"
    conn.execute(
        "UPDATE job_runs SET finished_at = ?, status = ?, detail = ? WHERE id = ?",
        (datetime.now(UTC).isoformat(), status, detail, run_id),
    )
    conn.commit()


def trigger_job(job_id: str) -> None:
    """Manual ad-hoc run of a job (used by the scheduler and a future manual-trigger route)."""
    with session() as conn:
        run_job(conn, job_id, now=datetime.now(UTC))
