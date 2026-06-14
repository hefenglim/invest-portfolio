"""Scheduler job registry, DB schedule config, and run log.

`scheduler/` triggers `pricing` (and later `llm_insight`) only — it holds no
business logic. This module is import-safe without APScheduler so it is fully
unit-testable; the APScheduler wiring lives in ``runtime.py``.
"""

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from portfolio_dash.pricing import datasources_store, ingest
from portfolio_dash.pricing.defaults import default_registry
from portfolio_dash.pricing.finmind_datasets import FinMindQuotaError, FinMindTierError
from portfolio_dash.pricing.refresh import refresh_dividends, refresh_history, refresh_quotes
from portfolio_dash.pricing.refs import FxPair, InstrumentRef
from portfolio_dash.pricing.results import RefreshSummary
from portfolio_dash.shared import config_store
from portfolio_dash.shared.db import session
from portfolio_dash.shared.enums import Currency, Market

logger = logging.getLogger(__name__)

# 3 consecutive failed runs of an ingest job escalate its source health to "error".
_FAIL_STREAK_THRESHOLD = 3

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


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, decl: str
) -> None:
    """Add ``column`` to ``table`` if absent (additive, idempotent migration).

    A LOCAL copy of the ``data_ingestion`` PRAGMA pattern, intentionally NOT imported:
    ``scheduler/`` must not gain a dependency on ``data_ingestion`` (see
    ``architecture.md``). ``PRAGMA table_info`` row index 1 is the column name,
    which is row_factory-agnostic.
    """
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def create_scheduler_tables(conn: sqlite3.Connection) -> None:
    """Create the scheduler tables idempotently and apply additive §15.0 migrations.

    The §15.0 columns (SR 2026-06-13) are added for legacy DBs that predate them so
    specs 04/07 (insight scheduling, run cost/skip reasons) can rely on their presence.
    """
    conn.executescript(_DDL)
    _add_column_if_missing(conn, "schedule_config", "kind", "TEXT NOT NULL DEFAULT 'system'")
    _add_column_if_missing(conn, "schedule_config", "payload", "TEXT")
    _add_column_if_missing(conn, "job_runs", "payload", "TEXT")
    _add_column_if_missing(conn, "job_runs", "reason", "TEXT")
    _add_column_if_missing(conn, "job_runs", "cost_usd", "TEXT")
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
    summary = refresh_quotes(conn, default_registry(conn), instruments, fx_pairs, now=now)
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
    summary = refresh_history(conn, default_registry(conn), instruments, start, now=now)
    return _summarize(summary)


def dividends_daily(conn: sqlite3.Connection, *, now: datetime) -> str:
    """Sweep dividend/ex-div events for all instruments."""
    instruments, _ = build_worklist(conn, None)
    summary = refresh_dividends(conn, default_registry(conn), instruments, now=now)
    return _summarize(summary)


# --- External-snapshot ingest jobs (spec 20.4) --------------------------------
# Map each ingest job to the data source whose health it escalates on a fail streak.
_INGEST_JOB_SOURCE: dict[str, str] = {
    "finmind_chips_daily": "finmind",
    "finmind_valuation_daily": "finmind",
    "finmind_fundamentals_monthly": "finmind",
    "sentiment_daily": "yfinance",
    "index_quotes_daily": "yfinance",
}


def _prior_consecutive_failures(conn: sqlite3.Connection, job_id: str) -> int:
    """Count the run of trailing ``error`` runs among the job's COMPLETED runs.

    Excludes the current in-progress run (``finished_at IS NULL``), so the caller adds
    1 for the about-to-fail current run when deciding whether the streak reached 3.
    """
    rows = conn.execute(
        "SELECT status FROM job_runs WHERE job_id = ? AND finished_at IS NOT NULL "
        "ORDER BY id DESC",
        (job_id,),
    ).fetchall()
    streak = 0
    for row in rows:
        if row["status"] == "error":
            streak += 1
        else:
            break
    return streak


def _run_ingest(
    conn: sqlite3.Connection, job_id: str, fn: Callable[[], int], *, now: datetime
) -> str:
    """Run one ingest, escalating source health to ``error`` on failure.

    On success returns a short summary (its ``job_runs`` row will log ``ok``, resetting
    the streak). A FinMind tier/quota error (spec 20.15.4) is a clear, actionable
    failure: it marks health ``error`` with the reason IMMEDIATELY (no 3-streak needed),
    writes no snapshot, then re-raises so ``run_job`` records the error row. Any other
    failure escalates health only when THIS run makes the trailing error streak reach the
    threshold (spec 20.12). Either way the exception re-raises for the ``job_runs`` log.
    """
    try:
        written = fn()
        return f"{written} snapshot(s) written"
    except (FinMindTierError, FinMindQuotaError) as exc:
        source_id = _INGEST_JOB_SOURCE.get(job_id, job_id)
        logger.warning(
            "ingest job %s hit a FinMind tier/quota limit; marking %s health=error: %s",
            job_id, source_id, exc,
        )
        datasources_store.upsert_health(
            conn, source_id, status="error", last_test=now.isoformat(),
            latency_ms=None, detail=f"{job_id}: {exc}",
        )
        raise
    except Exception as exc:  # noqa: BLE001 - escalate health, then re-raise to log
        streak = _prior_consecutive_failures(conn, job_id) + 1
        if streak >= _FAIL_STREAK_THRESHOLD:
            source_id = _INGEST_JOB_SOURCE.get(job_id, job_id)
            logger.warning(
                "ingest job %s failed %d times consecutively; marking %s health=error: %s",
                job_id, streak, source_id, exc,
            )
            datasources_store.upsert_health(
                conn, source_id, status="error", last_test=now.isoformat(),
                latency_ms=None, detail=f"{job_id}: {exc}",
            )
        raise


def finmind_chips_daily(conn: sqlite3.Connection, *, now: datetime) -> str:
    """Post-close: institutional + margin chips for the TW universe (FinMind)."""
    return _run_ingest(
        conn, "finmind_chips_daily", lambda: ingest.ingest_chips(conn, now=now), now=now
    )


def finmind_valuation_daily(conn: sqlite3.Connection, *, now: datetime) -> str:
    """Daily: PER/PBR/yield valuation for the TW universe (FinMind)."""
    return _run_ingest(
        conn, "finmind_valuation_daily", lambda: ingest.ingest_valuation(conn, now=now),
        now=now,
    )


def finmind_fundamentals_monthly(conn: sqlite3.Connection, *, now: datetime) -> str:
    """Monthly: revenue + financial statements for the TW universe (FinMind)."""
    return _run_ingest(
        conn, "finmind_fundamentals_monthly",
        lambda: ingest.ingest_fundamentals(conn, now=now), now=now,
    )


def sentiment_daily(conn: sqlite3.Connection, *, now: datetime) -> str:
    """Daily: VIX (yfinance ^VIX) + CNN Fear & Greed snapshots."""
    return _run_ingest(
        conn, "sentiment_daily", lambda: ingest.ingest_sentiment(conn, now=now), now=now
    )


def index_quotes_daily(conn: sqlite3.Connection, *, now: datetime) -> str:
    """Trading-day: TAIEX/SPX/KLCI index closes (yfinance)."""
    return _run_ingest(
        conn, "index_quotes_daily", lambda: ingest.ingest_index(conn, now=now), now=now
    )


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
    # External-snapshot ingest (spec 20.4).
    JobSpec(
        "finmind_chips_daily", finmind_chips_daily, "30 14 * * mon-fri", "Asia/Taipei", True,
        "TW institutional + margin chips (post-close)",
    ),
    JobSpec(
        "finmind_valuation_daily", finmind_valuation_daily, "40 14 * * mon-fri",
        "Asia/Taipei", True, "TW PER/PBR/yield valuation",
    ),
    JobSpec(
        "finmind_fundamentals_monthly", finmind_fundamentals_monthly, "0 9 12 * *",
        "Asia/Taipei", True, "TW monthly revenue + financials",
    ),
    JobSpec(
        "sentiment_daily", sentiment_daily, "0 8 * * *", "Asia/Taipei", True,
        "VIX + CNN Fear & Greed",
    ),
    JobSpec(
        "index_quotes_daily", index_quotes_daily, "50 14 * * mon-fri", "Asia/Taipei", True,
        "TAIEX/SPX/KLCI index closes",
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


def run_job(conn: sqlite3.Connection, job_id: str, *, now: datetime) -> int:
    """Execute one job, logging start/finish to ``job_runs``; return its run id.

    A job exception is caught and logged as ``status="error"`` (never re-raised), so
    one failing job cannot crash the scheduler or other jobs. The ``job_runs`` row is
    inserted before the job func runs, so the returned id is always valid regardless of
    job success/failure (consumed by the manual-refresh action to report ``run_ids``).
    """
    spec = _jobs_by_id()[job_id]
    cur = conn.execute(
        "INSERT INTO job_runs (job_id, started_at) VALUES (?, ?)",
        (job_id, now.isoformat()),
    )
    run_id = int(cur.lastrowid or 0)
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
    return run_id


def start_job_run(conn: sqlite3.Connection, job_id: str, *, now: datetime) -> int:
    """Insert a 'running' ``job_runs`` row (finished_at NULL) and return its id.

    Used by ``POST /api/scheduler/jobs/{id}/run`` to obtain the run id synchronously
    (on the request conn) before the background thread finalizes the row.
    """
    cur = conn.execute(
        "INSERT INTO job_runs (job_id, started_at) VALUES (?, ?)", (job_id, now.isoformat())
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def finish_job_run(conn: sqlite3.Connection, run_id: int, *, status: str, detail: str) -> None:
    """Finalize a running ``job_runs`` row with its terminal status + detail."""
    conn.execute(
        "UPDATE job_runs SET finished_at = ?, status = ?, detail = ? WHERE id = ?",
        (datetime.now(UTC).isoformat(), status, detail, run_id),
    )
    conn.commit()


def latest_run_unfinished(conn: sqlite3.Connection, job_id: str) -> bool:
    """True if the job's most recent run row is still running (``finished_at IS NULL``)."""
    row = conn.execute(
        "SELECT finished_at FROM job_runs WHERE job_id = ? ORDER BY id DESC LIMIT 1", (job_id,)
    ).fetchone()
    return row is not None and row["finished_at"] is None


def run_job_func(job_id: str, *, now: datetime) -> None:
    """Execute a job in a fresh session, finalizing its latest running row.

    For the async ``/run`` endpoint: the request handler already inserted the running
    row via ``start_job_run``; this opens its OWN connection (the request conn is closed
    by then) and finalizes it. This is a fire-and-forget daemon-thread target, so the
    WHOLE body is exception-safe — any failure (job func, or even the surrounding DB
    access) is swallowed so it never crashes the worker thread.
    """
    try:
        with session() as conn:
            rid = conn.execute(
                "SELECT id FROM job_runs WHERE job_id=? AND finished_at IS NULL "
                "ORDER BY id DESC LIMIT 1",
                (job_id,),
            ).fetchone()
            if rid is None:
                return
            try:
                detail = _jobs_by_id()[job_id].func(conn, now=now)
                status = "ok"
            except Exception as exc:  # noqa: BLE001 — swallow + log; never crash the thread
                detail, status = str(exc), "error"
            finish_job_run(conn, int(rid["id"]), status=status, detail=detail)
    except Exception:  # noqa: BLE001 — background worker must never raise out of the thread
        return


def log_export_run(
    conn: sqlite3.Connection, export_type: str, *, now: datetime, detail: str
) -> int:
    """Write a `job_runs` audit row for a completed export (spec 02 §3).

    Exports are not registered jobs, so the row uses a namespaced ``job_id``
    (``export:<type>``) rather than a ``kind`` column — spec 15.0 places ``kind`` on
    ``schedule_config``, not ``job_runs``. ``started_at`` == ``finished_at`` (synchronous).
    """
    ts = now.isoformat()
    cur = conn.execute(
        "INSERT INTO job_runs (job_id, started_at, finished_at, status, detail) "
        "VALUES (?, ?, ?, 'ok', ?)",
        (f"export:{export_type}", ts, ts, detail),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def trigger_job(job_id: str) -> None:
    """Manual ad-hoc run of a job (used by the scheduler and a future manual-trigger route)."""
    with session() as conn:
        run_job(conn, job_id, now=datetime.now(UTC))
