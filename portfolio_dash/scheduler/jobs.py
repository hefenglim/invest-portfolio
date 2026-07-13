"""Scheduler job registry, DB schedule config, and run log.

`scheduler/` triggers `pricing` (and later `llm_insight`) only — it holds no
business logic. This module is import-safe without APScheduler so it is fully
unit-testable; the APScheduler wiring lives in ``runtime.py``.
"""

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from portfolio_dash.llm_insight import alerts_bridge
from portfolio_dash.ops import backup as backup_ops
from portfolio_dash.ops import notify_dispatch
from portfolio_dash.pricing import datasources_store, ingest
from portfolio_dash.pricing.defaults import default_registry
from portfolio_dash.pricing.finmind_datasets import FinMindQuotaError, FinMindTierError
from portfolio_dash.pricing.refresh import (
    refresh_dividends,
    refresh_fx_history,
    refresh_history,
    refresh_quotes,
)
from portfolio_dash.pricing.refs import FxPair, InstrumentRef
from portfolio_dash.pricing.results import RefreshSummary
from portfolio_dash.shared import config_store
from portfolio_dash.shared.clock import app_now
from portfolio_dash.shared.config import get_settings
from portfolio_dash.shared.db import session
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.strategy.alerts import Alert, compute_alerts

logger = logging.getLogger(__name__)

# 3 consecutive failed runs of an ingest job escalate its source health to "error".
_FAIL_STREAK_THRESHOLD = 3

# --- Insight runner registration (spec 04.2) ----------------------------------
# The scheduler dispatches ``kind=insight`` schedule rows to a runner the app registers
# at startup, so ``scheduler/`` never imports ``api`` (architecture.md: scheduler triggers
# only). The runner reads pricing/portfolio (it lives in ``api/insight_service.py``).
InsightRunner = Callable[..., object]
_INSIGHT_RUNNER: InsightRunner | None = None


def register_insight_runner(fn: InsightRunner | None) -> None:
    """Register (or clear with None) the kind=insight dispatch runner (app wiring seam)."""
    global _INSIGHT_RUNNER
    _INSIGHT_RUNNER = fn


def get_insight_runner() -> InsightRunner | None:
    """The currently-registered insight runner, or None (not wired / scheduler-only)."""
    return _INSIGHT_RUNNER


# --- News runner registration (batch ④) ---------------------------------------
# Same seam as the insight runner: the app registers the news-pipeline runner at startup
# so ``scheduler/`` never imports ``api``/``news``. The runner (api/news_service.py) reads
# holdings + fetches + organizes into the separate news DB.
NewsRunner = Callable[..., object]
_NEWS_RUNNER: NewsRunner | None = None


def register_news_runner(fn: NewsRunner | None) -> None:
    """Register (or clear with None) the news_daily pipeline runner (app wiring seam)."""
    global _NEWS_RUNNER
    _NEWS_RUNNER = fn


# The Loop-2/3/4 runners (price-bearing evaluate + master-bearing calibrate) live in
# ``api/insight_service.py`` and are registered at startup, so ``scheduler/`` never imports
# ``api`` (architecture.md). The static evaluate/calibrate JOBS dispatch through these.
EvolutionRunner = Callable[..., object]
_EVALUATION_RUNNER: EvolutionRunner | None = None
_CALIBRATION_RUNNER: EvolutionRunner | None = None


def register_evaluation_runner(fn: EvolutionRunner | None) -> None:
    """Register (or clear with None) the Loop-2 evaluate runner (app wiring seam)."""
    global _EVALUATION_RUNNER
    _EVALUATION_RUNNER = fn


def get_evaluation_runner() -> EvolutionRunner | None:
    """The currently-registered evaluate runner, or None (scheduler-only / not wired)."""
    return _EVALUATION_RUNNER


def register_calibration_runner(fn: EvolutionRunner | None) -> None:
    """Register (or clear with None) the Loop-3 calibration runner (app wiring seam)."""
    global _CALIBRATION_RUNNER
    _CALIBRATION_RUNNER = fn


def get_calibration_runner() -> EvolutionRunner | None:
    """The currently-registered calibration runner, or None (scheduler-only / not wired)."""
    return _CALIBRATION_RUNNER

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

# job_runs.is_shadow: a Loop-4 shadow batch (spec 4.6) writes its own job_runs row under
# the SAME insight:{id} job_id as the active run; this flag distinguishes it so the
# user-facing /runs lists exclude it and spec-07 cost attribution stays per-run-kind.


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
    _add_column_if_missing(conn, "job_runs", "is_shadow", "INTEGER NOT NULL DEFAULT 0")
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


# --- Insight-type schedule binding (spec 4.2) ---------------------------------
# An insight_type schedule is a DYNAMIC, payload-dispatched ``schedule_config`` row —
# NOT one of the static ``JOBS``. 04a only persists the binding (kind=insight,
# payload=insight_type_id) and returns a deterministic job_id; the runtime dispatch of
# kind=insight is 04b. The API router (api → scheduler is allowed) calls these from the
# composer endpoints; ``llm_insight`` itself never imports ``scheduler``.


def insight_job_id(insight_type_id: int) -> str:
    """The deterministic schedule job_id for an insight_type binding."""
    return f"insight:{insight_type_id}"


def bind_insight_schedule(
    conn: sqlite3.Connection,
    insight_type_id: int,
    *,
    cron: str,
    tz: str = "Asia/Taipei",
) -> str:
    """Create/update the kind=insight ``schedule_config`` row for an insight_type.

    Upserts on the deterministic ``job_id`` so a re-bind updates the cron/timezone in
    place (no duplicate row). Returns the job_id. Ensures the scheduler tables exist
    first (idempotent). NO APScheduler wiring here — pure row write.
    """
    create_scheduler_tables(conn)
    job_id = insight_job_id(insight_type_id)
    conn.execute(
        "INSERT INTO schedule_config (job_id, enabled, cron, timezone, kind, payload) "
        "VALUES (?, 1, ?, ?, 'insight', ?) "
        "ON CONFLICT(job_id) DO UPDATE SET enabled = 1, cron = excluded.cron, "
        "timezone = excluded.timezone, kind = 'insight', payload = excluded.payload",
        (job_id, cron, tz, str(insight_type_id)),
    )
    conn.commit()
    return job_id


def unbind_insight_schedule(conn: sqlite3.Connection, insight_type_id: int) -> None:
    """Remove an insight_type's ``schedule_config`` binding row (no-op if absent)."""
    create_scheduler_tables(conn)
    conn.execute(
        "DELETE FROM schedule_config WHERE job_id = ?", (insight_job_id(insight_type_id),)
    )
    conn.commit()


# --- Job registry -------------------------------------------------------------
# Default cron times fall after each exchange's close; users override per job later.

_HISTORY_LOOKBACK_DAYS = 7


def _summarize(summary: RefreshSummary) -> str:
    """Human-readable run detail: counts + WHICH source answered WHAT (item 8).

    The old "N ok, M failed" told the user nothing about data sources or targets;
    now: ``3 ok, 1 failed [twse: 2330, 2603; yfinance: AAPL] failed: 8299``.
    Long lists truncate so job_runs.detail stays a one-liner.
    """
    parts = [f"{len(summary.ok)} ok, {len(summary.failed)} failed"]
    if summary.ok:
        by_src: dict[str, list[str]] = {}
        for key, src in summary.ok.items():
            by_src.setdefault(src, []).append(key)
        srcs = "; ".join(
            f"{src}: {', '.join(sorted(keys)[:8])}" + ("…" if len(keys) > 8 else "")
            for src, keys in sorted(by_src.items())
        )
        parts.append(f"[{srcs}]")
    if summary.failed:
        failed = sorted(summary.failed)
        parts.append("failed: " + ", ".join(failed[:8]) + ("…" if len(failed) > 8 else ""))
    return " ".join(parts)


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


# --- 待確認匯入 daily scan (R5 item 2, 2026-07-03) ------------------------------
# The full scan (event refresh + PENDING COUNT) lives in the api layer
# (api/dividend_inbox.scan_job) and is registered here at app startup — the same
# runner seam the insight jobs use, so scheduler/ never imports api/. A
# scheduler-only process without the runner falls back to the event refresh
# (the inbox computes on read, so items still appear).
DividendScanRunner = Callable[..., str]
_DIVIDEND_SCAN_RUNNER: DividendScanRunner | None = None


def register_dividend_scan_runner(fn: DividendScanRunner | None) -> None:
    """Register (or clear with None) the dividend-inbox scan runner (app wiring seam)."""
    global _DIVIDEND_SCAN_RUNNER
    _DIVIDEND_SCAN_RUNNER = fn


# 月度快照 runner seam (R6 item 8) — the writer needs build_dashboard (portfolio
# via the api service), registered at app startup like the other runners.
SnapshotRunner = Callable[..., str]
_SNAPSHOT_RUNNER: SnapshotRunner | None = None


def register_snapshot_runner(fn: SnapshotRunner | None) -> None:
    """Register (or clear with None) the monthly-snapshot runner (app wiring seam)."""
    global _SNAPSHOT_RUNNER
    _SNAPSHOT_RUNNER = fn


def snapshot_monthly(conn: sqlite3.Connection, *, now: datetime) -> str:
    """Daily: upsert the current month's KPI snapshot (month-rollover = final)."""
    runner = _SNAPSHOT_RUNNER
    if runner is None:
        return "no snapshot runner registered"
    return str(runner(conn, now=now))


# signal_scan runner seam (P2 batch 2): the scan reads pricing/portfolio + the rule engine
# and writes signal_states/alert_events; that orchestration lives in the api seam
# (api/signals_service.scan_signals), registered at app startup — so scheduler/ never
# imports api (architecture.md). A scheduler-only process without the runner is a safe
# no-op (state resumes seeding once the app wires it on the next scan).
SignalScanRunner = Callable[..., str]
_SIGNAL_SCAN_RUNNER: SignalScanRunner | None = None


def register_signal_scan_runner(fn: SignalScanRunner | None) -> None:
    """Register (or clear with None) the signal-scan runner (app wiring seam)."""
    global _SIGNAL_SCAN_RUNNER
    _SIGNAL_SCAN_RUNNER = fn


def signal_scan(conn: sqlite3.Connection, *, now: datetime) -> str:
    """Post-close: evaluate held-symbol rule signals → detect transitions → events.

    A separate static job (jobs here are one-purpose; the blueprint allows this or an
    alert_scan pre-step — the runner-seam job is the lowest-coupling option and is
    independently triggerable via ``POST /api/scheduler/jobs/signal_scan/run``). No runner
    wired → safe no-op summary."""
    runner = _SIGNAL_SCAN_RUNNER
    if runner is None:
        return "no signal scan runner registered"
    return str(runner(conn, now=now))


def dividend_inbox_scan(conn: sqlite3.Connection, *, now: datetime) -> str:
    """Daily: refresh dividend events for acquired symbols + report pending count."""
    runner = _DIVIDEND_SCAN_RUNNER
    if runner is not None:
        return str(runner(conn, now=now))
    acq = earliest_acquisitions(conn)
    instruments, _ = build_worklist(conn, None)
    refs = [r for r in instruments if r.symbol in acq]
    if not refs:
        return "no acquired symbols"
    summary = refresh_dividends(conn, default_registry(conn), refs, now=now)
    return _summarize(summary)


# --- External-snapshot ingest jobs (spec 20.4) --------------------------------
# Map each ingest job to the data source whose health it escalates on a fail streak.
_INGEST_JOB_SOURCE: dict[str, str] = {
    "finmind_chips_daily": "finmind",
    "finmind_valuation_daily": "finmind",
    "finmind_fundamentals_monthly": "finmind",
    "sentiment_daily": "yfinance",
    "index_quotes_daily": "yfinance",
    "consensus_daily": "yfinance",
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


def consensus_daily(conn: sqlite3.Connection, *, now: datetime) -> str:
    """Daily: analyst target-price + rating-distribution snapshots for all instruments.

    Slot: 09:10 Asia/Taipei — analyst consensus is a slow-moving, timezone-agnostic
    signal (not a market close), so it runs once in the morning, staggered just after
    the 08:00 sentiment job and before the intraday quote crons, on all days (yfinance
    serves whatever the latest consensus is regardless of any single market's session).
    """
    return _run_ingest(
        conn, "consensus_daily", lambda: ingest.ingest_consensus(conn, now=now), now=now
    )


# --- alert-scan + on_alert dispatch (spec 04.9 R7 / 4.10) ---------------------
# The job COMPUTES spec-03 alerts (reading the dashboard via strategy.alerts — a scheduler
# trigger of an existing computation, NEVER on page load), records ``alert_events``, and
# dispatches subscribing on_alert combos via the registered insight runner. This is the
# ONLY place an LLM insight is event-triggered; the dispatch is 24h-debounced per
# (task, rule, symbol) in ``llm_insight.alerts_bridge``.


# alert-compute runner seam (P3 batch 2): the FULL rule set needs per-symbol market metrics
# read from pricing + consensus snapshots, which lives in the api seam (api/alert_inputs.py).
# The app registers it at startup so scheduler/ never imports api (architecture.md), exactly
# like the signal_scan / snapshot / insight runners.
AlertComputeRunner = Callable[..., list[Alert]]
_ALERT_COMPUTE_RUNNER: AlertComputeRunner | None = None


def register_alert_compute_runner(fn: AlertComputeRunner | None) -> None:
    """Register (or clear with None) the full-alert-compute runner (app wiring seam)."""
    global _ALERT_COMPUTE_RUNNER
    _ALERT_COMPUTE_RUNNER = fn


def _compute_alerts_for_scan(conn: sqlite3.Connection, *, now: datetime) -> list[Alert]:
    """Compute the current spec-03 alerts for the scan (reporting ccy = TWD).

    A thin seam (overridable in tests) so the scan job stays a trigger: it does not
    reimplement the rule engine. When the app has registered the alert-compute runner
    (``api.alert_inputs.scan_alert_compute``) it runs the FULL P3 rule set (incl. the
    market-risk rules whose inputs are read from pricing — scheduler/ never imports api).
    A scheduler-only process without the runner degrades to the base ``strategy.alerts``
    engine (the 8 pre-P3 rules; the market-risk rules simply do not fire), which mirrors how
    the scan already omits ``calib_gap``.
    """
    runner = _ALERT_COMPUTE_RUNNER
    if runner is not None:
        return list(runner(conn, now=now))
    return compute_alerts(conn, now=now, reporting=Currency.TWD)


def _alert_symbol(alert: Alert) -> str | None:
    """The symbol an alert pertains to: the suffix of ``rule:symbol`` ids, else None.

    Per-target alerts use ``f"{rule}:{symbol}"`` ids (e.g. ``fx_drift:schwab``); a global
    alert's id equals its rule (e.g. ``quota_low``) and has no symbol.
    """
    prefix = f"{alert.rule}:"
    if alert.id.startswith(prefix):
        return alert.id[len(prefix):]
    return None


def alert_scan(conn: sqlite3.Connection, *, now: datetime) -> str:
    """Compute alerts → record events → dispatch subscribing on_alert combos (R7).

    The registered insight runner produces one short-horizon card per subscribing combo
    per (rule, symbol), 24h-debounced. Returns a short summary for the ``job_runs`` detail.
    """
    alerts_bridge.ensure_tables(conn)
    alerts = _compute_alerts_for_scan(conn, now=now)
    rules_seen: list[str] = []
    for alert in alerts:
        alerts_bridge.record_event(
            conn, rule_id=alert.rule, symbol=_alert_symbol(alert), now=now
        )
        if alert.rule not in rules_seen:
            rules_seen.append(alert.rule)
    runner = _INSIGHT_RUNNER
    dispatched = 0
    if runner is not None:
        dispatched = alerts_bridge.dispatch_alert_events(conn, runner, now=now)
    else:
        # No runner wired (scheduler-only process): still consume events so they do not
        # pile up; cards are produced once the app wires the runner on the next scan.
        for event in alerts_bridge.unconsumed_events(conn):
            alerts_bridge.mark_consumed(conn, event.id)
    # WP 3B: push unnotified events (this scan's + signal_scan's 14:55 events) to the
    # enabled channels. Uses the SEPARATE notified_at marker (independent of `consumed`
    # above). Wrapped so a push-path failure can never fail the alert scan itself.
    try:
        notify_detail = notify_dispatch.dispatch_notifications(conn, now=now)
    except Exception as exc:  # noqa: BLE001 - the push path must never break the scan
        logger.warning("notify dispatch failed in alert_scan: %s", exc)
        notify_detail = "notify: error"
    return (
        f"{len(alerts)} alert(s) [{', '.join(rules_seen)}], {dispatched} dispatched; "
        f"{notify_detail}"
    )


# --- Loop-2 evaluate + Loop-3 calibrate jobs (spec 04.4 / 4.5) ----------------
# Both dispatch to a runner registered by the app (price-/master-bearing reads live in
# ``api/insight_service.py``); a scheduler-only process with no runner is a safe no-op.


def evaluate_insights(conn: sqlite3.Connection, *, now: datetime) -> str:
    """Loop-2 daily: score every due insight via the registered evaluate runner (spec 4.4).

    The runner (``insight_service.evaluate_due``) reads price-at-create vs price-at-due,
    feeds the actual into the pure quant scorer, runs master narrative scoring (skipped when
    master unset), and writes ``insight_evaluations`` rows. Missing actual → pending_data
    (anti-poison). No runner wired → safe no-op summary (cards/evaluation resume once the
    app wires it).
    """
    runner = _EVALUATION_RUNNER
    if runner is None:
        return "no evaluate runner registered"
    runner(conn, now=now)
    return "evaluate pass complete"


def generate_calibrations(conn: sqlite3.Connection, *, now: datetime) -> str:
    """Loop-3 weekly: generate calibration versions via the registered calibration runner.

    The runner (``insight_service.generate_calibrations_for_all``) applies the §4.5 triggers
    + the min_samples gate + the §4.8 validator. Master unset → the runner pauses (no crash);
    no runner wired → safe no-op summary.
    """
    runner = _CALIBRATION_RUNNER
    if runner is None:
        return "no calibration runner registered"
    runner(conn, now=now)
    return "calibration pass complete"


def news_daily(conn: sqlite3.Connection, *, now: datetime) -> str:
    """Batch ④ nightly: run the news pipeline (discover→fetch→organize→store) via the
    registered runner (``news_service.run_news_daily``). No runner wired → safe no-op."""
    runner = _NEWS_RUNNER
    if runner is None:
        return "no news runner registered"
    result = runner(conn, now=now)
    if isinstance(result, dict):
        return (f"news: organized {result.get('organized', 0)}, "
                f"headline {result.get('headline_only', 0)}, "
                f"skipped {result.get('skipped_existing', 0)}"
                + (" (budget stop)" if result.get("stopped_budget") else ""))
    return "news pass complete"


# --- Ops 保全: daily SQLite backup + integrity check (spec 19.3) --------------
# Downward call (scheduler → ops, fine per architecture.md). The job runs the integrity
# pragma FIRST; a failed check RAISES so run_job records an error run (the v1 "warn" is the
# structured logger.warning + that error row — NOT a new spec-03 alert rule). A healthy DB
# is backed up + rotated. After an error streak that reached the threshold on PRIOR runs, a
# best-effort 3-consecutive-fail warning is logged (non-fatal).


def backup_daily(conn: sqlite3.Connection, *, now: datetime) -> str:
    """Daily: integrity-check the SQLite DB then write a rotated gzipped backup.

    On a FAILED ``PRAGMA integrity_check`` the job logs a structured warning and RAISES
    ``RuntimeError`` so ``run_job`` records an ``error`` run (the v1 保全 "warn" signal).
    On success it writes the daily backup via ``ops.backup.backup_database`` and, when the
    trailing consecutive-failure streak had already reached the threshold on prior runs,
    logs a best-effort 3-consecutive-fail warning. Returns a short ``job_runs.detail``.
    """
    ok, detail = backup_ops.check_integrity()
    if not ok:
        logger.warning("backup_daily integrity_check failed: %s", detail)
        raise RuntimeError(f"integrity_check failed: {detail}")
    if _prior_consecutive_failures(conn, "backup_daily") >= _FAIL_STREAK_THRESHOLD:
        logger.warning(
            "backup_daily recovered after %d+ consecutive failed run(s); backup resuming",
            _FAIL_STREAK_THRESHOLD,
        )
    path = backup_ops.backup_database(now=now)
    return f"backup ok -> {path.name}"


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
    # 待確認匯入 feeder (R5): post-close, after the quote refreshes settle.
    JobSpec(
        "dividend_inbox_scan", dividend_inbox_scan, "30 15 * * mon-fri", "Asia/Taipei",
        True, "Dividend detection sweep + pending count (feeds 待確認匯入)",
    ),
    # 月度快照 (R6 item 8): nightly upsert of the current month's KPI row — the
    # value standing at month rollover IS the month-end record.
    JobSpec(
        "snapshot_monthly", snapshot_monthly, "50 23 * * *", "Asia/Taipei", True,
        "Monthly KPI snapshot (nightly upsert of the current month)",
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
    JobSpec(
        "consensus_daily", consensus_daily, "10 9 * * *", "Asia/Taipei", True,
        "Analyst target price + rating distribution (all instruments)",
    ),
    # Rule-signal scan (P2 batch 2): post-close, after quotes refresh, before the alert
    # scan so any signal transition is recorded ahead of the on_alert dispatch pass.
    JobSpec(
        "signal_scan", signal_scan, "55 14 * * mon-fri", "Asia/Taipei", True,
        "Technical rule-signal scan + state-transition events",
    ),
    # on_alert scan (spec 04.9 R7): post-close, after quotes refresh, before insight cron.
    JobSpec(
        "alert_scan", alert_scan, "0 15 * * mon-fri", "Asia/Taipei", True,
        "Risk-alert scan + on_alert AI dispatch",
    ),
    # Loop-2 evaluate (spec 04.4): daily, after the alert scan / insight cron settle.
    JobSpec(
        "evaluate_insights", evaluate_insights, "0 18 * * *", "Asia/Taipei", True,
        "Daily insight backtest scoring (Loop 2)",
    ),
    # Loop-3 calibrate (spec 04.5): weekly (Sun), after a week of evaluations accrue.
    JobSpec(
        "generate_calibrations", generate_calibrations, "0 19 * * sun", "Asia/Taipei", True,
        "Weekly calibration version generation (Loop 3)",
    ),
    # Ops 保全 (spec 19.3): daily SQLite backup + integrity check (01:30 Asia/Taipei).
    JobSpec(
        "backup_daily", backup_daily, "30 1 * * *", "Asia/Taipei", True,
        "Daily SQLite backup + integrity check",
    ),
    # News pipeline (batch ④): nightly, before the morning insight crons so cards read
    # fresh organized news. Runs after quotes/chips ingest settle.
    JobSpec(
        "news_daily", news_daily, "0 6 * * *", "Asia/Taipei", True,
        "Nightly news fetch + AI-organize into the news DB",
    ),
]

DEFAULT_BOARD: dict[Market, str] = {Market.US: "", Market.MY: ".KL", Market.TW: "TWSE"}
_DEFAULT_BOARD = DEFAULT_BOARD  # back-compat alias (internal callers below)

# Reporting-currency FX pairs needed for the combined view (reporting ccy = TWD).
# Public: the api-layer instrument service reuses the same fixed set.
REPORTING_FX_PAIRS: list[FxPair] = [
    FxPair(base=Currency.USD, quote=Currency.TWD),
    FxPair(base=Currency.USD, quote=Currency.MYR),
    FxPair(base=Currency.MYR, quote=Currency.TWD),
]
_FX_PAIRS = REPORTING_FX_PAIRS  # back-compat alias (internal callers below)


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


def refresh_instrument_quote(
    conn: sqlite3.Connection, *, symbol: str, market: Market, board: str | None,
    now: datetime,
) -> str:
    """Fetch the latest quote for ONE instrument (+ the reporting FX pairs).

    Used by the registration flow (POST /api/instruments) so a newly registered
    symbol gets a price immediately instead of waiting for its market's next
    post-close cron. Idempotent upserts; a provider failure raises (the caller
    treats the fetch as best-effort and never fails the registration over it).
    """
    ref = InstrumentRef(symbol=symbol, market=market, board=board or _DEFAULT_BOARD[market])
    summary = refresh_quotes(conn, default_registry(conn), [ref], _FX_PAIRS, now=now)
    return _summarize(summary)


# Smart backfill windows: the default floor is config-driven
# (``history_backfill_days``, 5y since owner 2026-07-08 — env-overridable); a symbol
# whose position began EARLIER backfills from its first acquisition date; the FX
# pairs backfill from the earliest ledger flow date — so the trend replay / XIRR
# have a rate on-or-before every flow.


def earliest_acquisitions(conn: sqlite3.Connection) -> dict[str, date]:
    """Per-symbol earliest acquisition date: min(first BUY trade, opening build)."""
    out: dict[str, date] = {}
    for row in conn.execute(
        "SELECT symbol, MIN(trade_date) AS d FROM transactions "
        "WHERE side='BUY' GROUP BY symbol"
    ):
        out[row["symbol"]] = date.fromisoformat(row["d"])
    for row in conn.execute(
        "SELECT symbol, MIN(build_date) AS d FROM opening_inventory GROUP BY symbol"
    ):
        d = date.fromisoformat(row["d"])
        if row["symbol"] not in out or d < out[row["symbol"]]:
            out[row["symbol"]] = d
    return out


def earliest_ledger_flow(conn: sqlite3.Connection) -> date | None:
    """The earliest dated flow across all four ledgers (None on an empty ledger)."""
    dates: list[str] = []
    for sql in (
        "SELECT MIN(trade_date) AS d FROM transactions",
        "SELECT MIN(date) AS d FROM dividends",
        "SELECT MIN(date) AS d FROM fx_conversions",
        "SELECT MIN(build_date) AS d FROM opening_inventory",
    ):
        row = conn.execute(sql).fetchone()
        if row is not None and row["d"]:
            dates.append(row["d"])
    return date.fromisoformat(min(dates)) if dates else None


def backfill_history_all(
    conn: sqlite3.Connection, *, now: datetime, days: int | None = None
) -> str:
    """Backfill daily close history for ALL instruments + the reporting FX pairs.

    ``days=None`` (the default) uses the SMART windows: the config-driven floor
    (``history_backfill_days``, 5y default), extended per symbol to its first
    acquisition date when that is older, and for FX to the earliest ledger flow
    date. An explicit ``days`` keeps the old uniform-window behavior. Idempotent
    upserts; per-key failures degrade into the summary, never raise.
    """
    instruments, fx_pairs = build_worklist(conn, None)
    registry = default_registry(conn)
    default_days = days or get_settings().history_backfill_days
    default_start = (now - timedelta(days=default_days)).date()

    if days is not None:
        p_summary = refresh_history(conn, registry, instruments, default_start, now=now)
        f_summary = refresh_fx_history(conn, registry, fx_pairs, default_start, now=now)
        return f"prices: {_summarize(p_summary)} · fx: {_summarize(f_summary)}"

    acq = earliest_acquisitions(conn)
    by_start: dict[date, list[InstrumentRef]] = {}
    for ref in instruments:
        first = acq.get(ref.symbol)
        start = min(default_start, first) if first is not None else default_start
        by_start.setdefault(start, []).append(ref)
    p_ok: dict[str, str] = {}
    p_failed: list[str] = []
    for start, refs in sorted(by_start.items()):
        s = refresh_history(conn, registry, refs, start, now=now)
        p_ok.update(s.ok)
        p_failed.extend(s.failed)
    p_summary = RefreshSummary(ok=p_ok, failed=p_failed, fetched_at=now)

    flow = earliest_ledger_flow(conn)
    fx_start = min(default_start, flow) if flow is not None else default_start
    f_summary = refresh_fx_history(conn, registry, fx_pairs, fx_start, now=now)
    return (
        f"prices: {_summarize(p_summary)} · fx(from {fx_start.isoformat()}): "
        f"{_summarize(f_summary)}"
    )


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
    # finished_at shares *now*'s timezone (M1 fix): a UTC finish next to a +08:00 start
    # reads as a negative-duration run in any naive display.
    conn.execute(
        "UPDATE job_runs SET finished_at = ?, status = ?, detail = ? WHERE id = ?",
        (datetime.now(tz=now.tzinfo or UTC).isoformat(), status, detail, run_id),
    )
    conn.commit()
    return run_id


def start_job_run(conn: sqlite3.Connection, job_id: str, *, now: datetime) -> int:
    """Insert a 'running' ``job_runs`` row (finished_at NULL) and return its id.

    Used by ``POST /api/scheduler/jobs/{id}/run`` to obtain the run id synchronously
    (on the request conn) before the background thread finalizes the row.
    """
    cur = conn.execute(
        "INSERT INTO job_runs (job_id, started_at, status) VALUES (?, ?, 'running')",
        (job_id, now.isoformat()),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def finish_job_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    detail: str,
    now: datetime | None = None,
) -> None:
    """Finalize a running ``job_runs`` row with its terminal status + detail.

    ``finished_at`` shares *now*'s timezone when given (started_at comes from get_now
    in +08:00; a UTC finish next to it reads as a negative-duration run).
    """
    finished_at = datetime.now(tz=now.tzinfo if now is not None else UTC).isoformat()
    conn.execute(
        "UPDATE job_runs SET finished_at = ?, status = ?, detail = ? WHERE id = ?",
        (finished_at, status, detail, run_id),
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
            finish_job_run(conn, int(rid["id"]), status=status, detail=detail, now=now)
    except Exception:  # noqa: BLE001 — background worker must never raise out of the thread
        return


def start_insight_run(conn: sqlite3.Connection, insight_type_id: int, *, now: datetime) -> int:
    """Insert a 'running' insight ``job_runs`` row (kind=insight payload) and return its id.

    Used by the async ``POST /api/insight-types/{id}/run`` to obtain a run id synchronously;
    the background runner finalizes THIS row (via ``generate.run_insight_type(run_id=...)``).
    """
    cur = conn.execute(
        "INSERT INTO job_runs (job_id, started_at, status, payload) "
        "VALUES (?, ?, 'running', ?)",
        (insight_job_id(insight_type_id), now.isoformat(), str(insight_type_id)),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def run_insight_func(insight_type_id: int, *, now: datetime, run_id: int) -> None:
    """Daemon target: dispatch the registered insight runner in a fresh session.

    The request handler already inserted the running row via :func:`start_insight_run`; this
    opens its OWN connection and calls the runner with ``run_id`` so the same row is
    finalized. Fully exception-safe (a fire-and-forget worker must never raise out).
    """
    try:
        runner = _INSIGHT_RUNNER
        if runner is None:
            return
        with session() as conn:
            runner(conn, insight_type_id, now=now, run_id=run_id)
    except Exception:  # noqa: BLE001 — background worker must never raise out of the thread
        return


# log_export_run REMOVED (2026-07-03, human decision): exports are user actions,
# recorded by the api-layer 系統操作記錄 middleware — not scheduler work. The runs
# view filters legacy ``export:*`` rows.


def _insight_payload(conn: sqlite3.Connection, job_id: str) -> int | None:
    """The insight_type_id payload of a kind=insight schedule row, or None when not one."""
    row = conn.execute(
        "SELECT kind, payload FROM schedule_config WHERE job_id = ?", (job_id,)
    ).fetchone()
    if row is None or row["kind"] != "insight" or row["payload"] is None:
        return None
    try:
        return int(row["payload"])
    except (TypeError, ValueError):
        return None


def _record_skipped_overlap(
    conn: sqlite3.Connection, job_id: str, payload: int, *, now: datetime
) -> None:
    """Insert a completed ``skipped`` job_runs row for a cron/manual overlap (M5).

    Mirrors the shape ``llm_insight.generate._write_job_run`` uses (raw SQL — sharing a
    table is not importing a module) so the run shows in the task's history/diagnose.
    """
    conn.execute(
        "INSERT INTO job_runs (job_id, started_at, finished_at, status, detail, payload, "
        "reason, cost_usd, is_shadow) VALUES (?, ?, ?, 'skipped', ?, ?, "
        "'already_running', '0', 0)",
        (
            job_id, now.isoformat(), now.isoformat(),
            "already_running: 前一次執行尚未完成，本次排程觸發已略過", str(payload),
        ),
    )
    conn.commit()


def dispatch_job(conn: sqlite3.Connection, job_id: str, *, now: datetime) -> int | None:
    """Run one scheduled job, dispatching by ``kind`` (spec 04.2).

    A ``kind=insight`` row is dispatched to the REGISTERED insight runner against its
    payload (the insight_type_id); the runner owns its own ``job_runs`` record. Any other
    job runs through the static JOBS registry via :func:`run_job` (returning its run id).
    A kind=insight row with no registered runner is a safe no-op (returns None), as is an
    UNKNOWN job_id (no schedule row + not a static job — e.g. a stale live trigger firing
    after its task was deleted; H1 fix — logged, never a KeyError). A kind=insight fire
    while the task's latest run is still unfinished (a manual run in flight) SKIPS with a
    ``job_runs`` row (reason ``already_running``) — the cron overlap guard (M5), mirroring
    the manual endpoint's 409 guard.
    """
    payload = _insight_payload(conn, job_id)
    if payload is not None:
        runner = _INSIGHT_RUNNER
        if runner is None:
            logger.info("kind=insight job %s fired but no runner is registered; skipping", job_id)
            return None
        if latest_run_unfinished(conn, job_id):
            logger.info(
                "kind=insight job %s fired while a run is in flight; skipping (overlap guard)",
                job_id,
            )
            _record_skipped_overlap(conn, job_id, payload, now=now)
            return None
        try:
            runner(conn, payload, now=now)
        except Exception:  # noqa: BLE001 — a runner failure must never crash the scheduler
            logger.exception("insight runner failed for %s", job_id)
        return None
    if job_id not in _jobs_by_id():
        logger.warning(
            "dispatch_job: unknown job id %s (no schedule row, not a static job); skipping",
            job_id,
        )
        return None
    return run_job(conn, job_id, now=now)


def trigger_job(job_id: str) -> None:
    """Manual ad-hoc run of a job (used by the scheduler cron triggers).

    Opens its own session and dispatches by kind (kind=insight → registered runner;
    otherwise the static job). Fire-and-forget: any failure is swallowed by ``dispatch_job``.
    The clock is :func:`shared.clock.app_now` (Asia/Taipei) — the SAME day anchor as the
    API's ``get_now`` (M1 fix): a cron run and a manual run of the same Taipei trading day
    must produce the same day-anchored cache fingerprint.
    """
    with session() as conn:
        dispatch_job(conn, job_id, now=app_now())
