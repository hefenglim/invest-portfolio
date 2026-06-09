"""Scheduler job registry, DB schedule config, and run log.

`scheduler/` triggers `pricing` (and later `llm_insight`) only — it holds no
business logic. This module is import-safe without APScheduler so it is fully
unit-testable; the APScheduler wiring lives in ``runtime.py``.
"""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from portfolio_dash.pricing.refs import FxPair, InstrumentRef
from portfolio_dash.shared import config_store
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
# Placeholder funcs are replaced with the real pricing-refresh functions in a later task.
def _todo(conn: sqlite3.Connection, *, now: datetime) -> str:  # pragma: no cover
    raise NotImplementedError


JOBS: list[JobSpec] = [
    JobSpec(
        "quotes_tw", _todo, "0 14 * * mon-fri", "Asia/Taipei", True,
        "TW quotes + FX (post-close)",
    ),
    JobSpec(
        "quotes_us", _todo, "30 16 * * mon-fri", "America/New_York", True,
        "US quotes + FX (post-close)",
    ),
    JobSpec(
        "quotes_my", _todo, "30 17 * * mon-fri", "Asia/Kuala_Lumpur", True,
        "MY quotes + FX (post-close)",
    ),
    JobSpec(
        "history_daily", _todo, "0 2 * * *", "Asia/Taipei", True,
        "Daily history backfill (recent window)",
    ),
    JobSpec(
        "dividends_daily", _todo, "0 3 * * *", "Asia/Taipei", True,
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
