"""Scheduler job registry, DB schedule config, and run log.

`scheduler/` triggers `pricing` (and later `llm_insight`) only — it holds no
business logic. This module is import-safe without APScheduler so it is fully
unit-testable; the APScheduler wiring lives in ``runtime.py``.
"""

import inspect
import logging
import sqlite3
import threading
from collections.abc import Callable, Set
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from portfolio_dash.data_ingestion.store import list_corporate_actions
from portfolio_dash.llm_insight import alerts_bridge
from portfolio_dash.llm_insight.insights_store import InsightTrigger
from portfolio_dash.ops import backup as backup_ops
from portfolio_dash.ops import notify_dispatch
from portfolio_dash.pricing import datasources_store, ingest
from portfolio_dash.pricing.benchmarks import benchmark_refs
from portfolio_dash.pricing.cross import fetched_pairs
from portfolio_dash.pricing.defaults import default_registry
from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.finmind_datasets import FinMindQuotaError, FinMindTierError
from portfolio_dash.pricing.refresh import (
    describe_refresh,
    refresh_dividends,
    refresh_fx_history,
    refresh_history,
    refresh_quotes,
)
from portfolio_dash.pricing.refs import FxPair, InstrumentRef
from portfolio_dash.pricing.registry import Registry
from portfolio_dash.pricing.results import RefreshSummary
from portfolio_dash.pricing.store import SplitFactorFn
from portfolio_dash.shared import config_store
from portfolio_dash.shared.account_ref import account_ref
from portfolio_dash.shared.alert_rule_names import rule_name
from portfolio_dash.shared.clock import app_now
from portfolio_dash.shared.config import get_settings
from portfolio_dash.shared.corporate_actions import ActionIndex, split_factor
from portfolio_dash.shared.db import session
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.instrument_scope import tracked_instruments
from portfolio_dash.strategy.alerts import Alert, compute_alerts

logger = logging.getLogger(__name__)


def split_factor_fn(conn: sqlite3.Connection) -> SplitFactorFn:
    """Bind the corporate-action ledger into the price write seam's factor lookup (D17).

    ``pricing/`` may not import ``data_ingestion`` (``architecture.md``), so the ratio
    lookup is injected as a callable and THIS layer — which sits above both — is where
    the two meet. The honest cost, acknowledged in spec §5.1: ``scheduler/`` gains a
    file-level ``data_ingestion`` import it did not have. It is a legal downward edge
    (the ``api``/``scheduler`` layer already has others) and cheaper than either
    rejected alternative: ``pricing → data_ingestion`` has no edge at all, and moving
    the SELECT into ``shared/`` breaks §6.0's "no module writes its own SELECT".

    Build ONCE per refresh — never per row and never per symbol. :class:`ActionIndex`
    exists for exactly this: a per-symbol history backfill loop would otherwise re-read
    and re-group the whole action ledger once per instrument.

    A ledger row too malformed to be a :class:`CorporateAction` contributes **no factor**
    and is recorded on the index's ``unreadable`` list — it does not raise (changed
    2026-08-11). This is a scheduled background job: raising here stops the price refresh,
    and the owner sees that only as prices that quietly stopped updating, which is a worse
    failure than the one raising was meant to prevent. The wrong price basis such a row
    leaves behind is not silent either — the SAME row makes ``build_book`` blank the
    portfolio XIRR with a named reason and mark the position 待釐清. Same conversion
    (:func:`convert_stored`) and same behaviour as ``store.load_ledger_bundle``.
    """
    index = ActionIndex.from_stored(list_corporate_actions(conn))

    def factor_of(symbol: str, *, after: date, through: date) -> Decimal:
        return split_factor(index, symbol, after=after, through=through)

    return factor_of

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


# --- Digest runner registration (P3 batch 3 · Wave 1) -------------------------
# Same seam as the news/insight runners: the app registers the digest assembler at startup
# so ``scheduler/`` never imports ``api``/``digest_service``. The runner (api/digest_service
# .run_digest) reads the computed dashboard + stored prices, stores the digest, and pushes
# (counts/percentages only — B3-D4). Signature: ``fn(conn, kind, now) -> str``.
DigestRunner = Callable[..., "str | JobOutcome"]
_DIGEST_RUNNER: DigestRunner | None = None


def register_digest_runner(fn: DigestRunner | None) -> None:
    """Register (or clear with None) the daily/weekly digest runner (app wiring seam)."""
    global _DIGEST_RUNNER
    _DIGEST_RUNNER = fn


def digest_daily(conn: sqlite3.Connection, *, now: datetime) -> "JobOutcome":
    """Daily close digest: assemble + store + push via the registered runner (kind=daily).

    No runner wired (scheduler-only process) → a safe no-op that says so; the digest resumes
    once the app registers the runner on the next fire. The runner's own verdict (DEF-067:
    a degraded block or a push that missed a channel is ``partial``) passes through."""
    runner = _DIGEST_RUNNER
    if runner is None:
        return _no_runner("每日摘要")
    set_progress("digest_daily", "組裝每日收盤摘要")
    return _outcome_of(runner(conn, "daily", now=now))


def digest_weekly(conn: sqlite3.Connection, *, now: datetime) -> "JobOutcome":
    """Weekly action list: assemble + store + push via the registered runner (kind=weekly)."""
    runner = _DIGEST_RUNNER
    if runner is None:
        return _no_runner("每週行動清單")
    set_progress("digest_weekly", "組裝每週行動清單")
    return _outcome_of(runner(conn, "weekly", now=now))


# The Loop-2/3/4 runners (price-bearing evaluate + master-bearing calibrate) live in
# ``api/insight_service.py`` and are registered at startup, so ``scheduler/`` never imports
# ``api`` (architecture.md). The static evaluate/calibrate JOBS dispatch through these.
# A runner RETURNS its pass summary and the job prints ``str(summary)`` as the run detail
# (R6 DEF-073: the summary's ``__str__`` is the zh sentence, so this layer needs no import
# of the api type and holds no wording of its own for what the pass did).
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

@dataclass(frozen=True)
class JobOutcome:
    """A job's terminal verdict, returned INSTEAD of a bare detail string (M10-02).

    ``run_job`` / ``run_job_func`` used to know two outcomes: the func returned → ``ok``, the
    func raised → ``error``. A quote refresh deliberately never raises for a lost symbol
    (``pricing/refresh.py``: failed keys are recorded, not raised — never crash the dashboard),
    so a run that lost every holding was structurally invisible as anything but ``ok``. A job
    that can finish without raising and still not have succeeded returns one of these; the
    wrappers write ``status`` verbatim. ``partial`` is ``job_runs``' existing vocabulary — the
    insight runner has written it since R6 — not a new state.

    ``results`` is whatever structured detail the job wants a synchronous caller to have
    (the refresh-quotes door serves it as an additive ``results`` block). It is built from
    the job's own data, never parsed back out of ``detail``; the ``job_runs`` row stores
    ``status`` + ``detail`` only.
    """

    status: str
    detail: str
    results: dict[str, Any] = field(default_factory=dict)


def _outcome_of(result: object) -> JobOutcome:
    """Normalise a job func's return: a bare string is, as it always was, ``ok``.

    ⚠ DEF-067: that default is exactly how ``dividends_daily`` reported 「成功」 over
    「0 檔事件已更新，10 檔失敗」. A job that can lose part of its work without raising
    returns a :class:`JobOutcome` (see :func:`sweep_outcome`); a bare string is reserved
    for a job whose only failure mode is an exception. ``tests/scheduler/
    test_def067_sweep_verdicts.py`` pins each job's verdict.
    """
    return result if isinstance(result, JobOutcome) else JobOutcome("ok", str(result))


def sweep_outcome(detail: str, *, total: int, failed: int) -> JobOutcome:
    """The verdict of a per-key sweep (DEF-067, 2026-09-26) — the detail is unchanged.

    Every key lost → ``error``: the word a raised run already writes and the 排程中心 maps
    to 失敗 (no new state). Some lost → ``partial`` (部分). None → ``ok``. An empty
    work-list is ``ok``: nothing was asked, so nothing was lost.
    """
    if total > 0 and failed >= total:
        return JobOutcome("error", detail)
    return JobOutcome("partial" if failed > 0 else "ok", detail)


def _no_runner(what: str) -> JobOutcome:
    """A runner-seam job in a process that registered no runner (a scheduler-only process;
    the app registers every runner at startup, so this never happens in deployment).

    It read 「no … runner registered」 (DEF-073). The wording matches the evaluate /
    calibrate jobs' (「…執行器未接線，未執行」), and so does the status: ``ok`` — a safe
    no-op, as ``tests/scheduler/test_ingest_jobs.py`` pins, not a lost fetch."""
    return JobOutcome("ok", f"{what}執行器未接線，未執行")


# A job does its own trigger+wiring and returns a short run summary for job_runs.detail —
# or a JobOutcome when the summary must carry a status the wrappers cannot infer.
JobFunc = Callable[..., str | JobOutcome]


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

    READ FIRST (DEF-065): ``ensure_scheduler_seeded`` runs this on every call, including the
    GET routes of the 排程 page, and N ``INSERT OR IGNORE`` + ``commit`` that insert nothing
    still take the write lock. The job ids already present are read first; nothing is
    written unless a registered job has no row.
    """
    have = {str(r[0]) for r in conn.execute("SELECT job_id FROM schedule_config")}
    if all(job.id in have for job in JOBS):
        return
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

# A benchmark fetch never fails its job (FU-D27): the sentence says it was skipped instead.
_BENCHMARK_FAILED = "更新失敗，已略過（不影響其他標的）"


_OK_PER_SOURCE_SHOWN = 8


def _failed_item(key: str, reasons: dict[str, str]) -> str:
    """One failed key, with its reason when the fetch recorded one (never invented)."""
    reason = reasons.get(key)
    return f"{key}：{reason}" if reason else key


def _summarize(summary: RefreshSummary) -> str:
    """THE run-detail sentence for a quote / history / FX / benchmark refresh (zh).

    「3 項已更新，1 項失敗（來源 twse：2330、2603；yfinance：AAPL）；失敗：8299」.

    DEF-030 follow-up (2026-09-23): this was the last English sentence the 排程中心 printed —
    「3 ok, 1 failed [twse: 2330, 2603; yfinance: AAPL] failed: 8299」 — beside the dividend
    jobs that DEF-015 had already moved to 「14 檔事件已更新，1 檔失敗（TSLA：…）」. Nine call
    sites in this module (quotes, the one-symbol quote, history, benchmarks, backfill) render
    through here, so one formatter owns the wording. ``項`` rather than ``檔``: the worklist
    carries FX pairs and benchmark indices beside the symbols.

    Item 8 (2026-07-03) still holds — the sentence names WHICH source answered WHAT. The
    per-source OK list truncates at 8 with 「等 N 項」 (it is colour, not evidence); the
    FAILED list never does (M10-02, owner ruling 2026-09-06). A failed key carries its reason
    when the fetch recorded one in ``failed_reasons`` (DEF-015); otherwise the key alone —
    ``failed`` may already hold a zh refusal line such as 「2330：收盤價非正數（0），已拒絕
    寫入」, which is printed as it is.
    """
    head = f"{len(summary.ok)} 項已更新"
    if summary.failed:
        head += f"，{len(summary.failed)} 項失敗"
    if summary.ok:
        by_src: dict[str, list[str]] = {}
        for key, src in summary.ok.items():
            by_src.setdefault(src, []).append(key)
        srcs = "；".join(
            f"{src}：{'、'.join(sorted(keys)[:_OK_PER_SOURCE_SHOWN])}"
            + (f" 等 {len(keys)} 項" if len(keys) > _OK_PER_SOURCE_SHOWN else "")
            for src, keys in sorted(by_src.items())
        )
        head += f"（來源 {srcs}）"
    if summary.failed:
        # getattr: the scheduler's own test doubles predate the DEF-015 field.
        reasons: dict[str, str] = getattr(summary, "failed_reasons", None) or {}
        items = [_failed_item(k, reasons) for k in sorted(summary.failed)]
        sep = "；" if any("：" in i for i in items) else "、"
        head += "；失敗：" + sep.join(items)
    return head


# Held-symbols seam (M10-02): the partial verdict below asks "did a HELD instrument fail?",
# and the held set is a ``data_ingestion``/``portfolio`` computation (``current_shares``:
# opening + buys − sells + reinvest shares, every corporate action applied in date order)
# that ``scheduler/`` may not import (architecture.md; the guard in
# ``tests/scheduler/test_ingest_jobs.py``). Same injection pattern as the fundamentals AV
# runner above: the app registers ``api.routers.actions.held_symbols`` at startup.
# Unregistered, the verdict degrades CONSERVATIVELY — every lost instrument counts — so a
# process that forgot the wiring over-reports; it never hides a lost holding.
HeldSymbolsFn = Callable[[sqlite3.Connection], set[str]]
_HELD_SYMBOLS_FN: HeldSymbolsFn | None = None


def register_held_symbols_fn(fn: HeldSymbolsFn | None) -> None:
    """Register (or clear with None) the held-symbols reader (app wiring seam)."""
    global _HELD_SYMBOLS_FN
    _HELD_SYMBOLS_FN = fn


def _quote_outcome(
    conn: sqlite3.Connection,
    summary: RefreshSummary,
    instruments: list[InstrumentRef],
    fx_pairs: list[FxPair],
) -> JobOutcome:
    """The quote job's verdict + structured counts, from the worklist and ``summary.ok``.

    Threshold (owner ruling 2026-09-06): **any HELD instrument failed → ``partial``**. A lost
    FX pair or a watchlist-only symbol is written into ``detail`` but does not change the
    verdict — the status chip answers "is my holdings valuation complete?".
    **Every instrument of the market lost → ``error``** (owner ruling 2026-09-26, DEF-067 ①):
    the same verdict as every other sweep (:func:`sweep_outcome`). It read 部分 even when
    nothing at all had been updated. A market with nothing to quote is not an error.

    What failed is derived as ``worklist − summary.ok``, NOT from ``summary.failed``: that list
    mixes bare keys with zh refusal lines (``2330：收盤價非正數…``), and parsing a symbol back
    out of a sentence is exactly the string-only bookkeeping this fix removes.
    """
    ok = summary.ok
    instruments_failed = sorted(ref.symbol for ref in instruments if ref.symbol not in ok)
    fx_failed = sorted(
        key for key in (f"{p.base.value}{p.quote.value}" for p in fx_pairs) if key not in ok
    )
    held: set[str] | None = None
    held_fn = _HELD_SYMBOLS_FN
    if held_fn is not None:
        try:
            held = held_fn(conn)
        except Exception as exc:  # noqa: BLE001 — cannot tell what is held → count everything
            logger.warning("held-symbols seam failed; counting every lost instrument: %s", exc)
    held_failed = [s for s in instruments_failed if held is None or s in held]
    fetched = [ref.symbol for ref in instruments if ref.symbol in ok]
    return JobOutcome(
        status=(
            "error" if instruments and not fetched
            else "partial" if held_failed else "ok"
        ),
        detail=_summarize(summary),
        results={
            "instruments": len(instruments),
            # DEF-068: the count the toast prints as 「N 檔已更新」 — served, not derived
            # in the browser (which only formats).
            "instruments_updated": len(fetched),
            "instruments_failed": instruments_failed,
            "held_failed": held_failed,
            "fx_pairs": sorted(f"{p.base.value}{p.quote.value}" for p in fx_pairs),
            "fx_failed": fx_failed,
            "lagging": _lagging_symbols(conn, fetched, held),
        },
    )


def combine_quote_results(outcomes: list[JobOutcome]) -> dict[str, Any]:
    """ONE summary over the quote jobs of a refresh request (DEF-068, 2026-09-26).

    The toast used to read ``held_failed`` alone and always add 「其餘已更新」 — with every
    provider down, 0 instruments had been updated. Built from each job's structured
    ``results`` (never from a detail sentence). A market has its own instruments, so their
    counts add; the FX pairs are the SAME two USD legs in every market job, so a pair failed
    only if every job that asked for it failed. ``all_failed``: at least one instrument was
    asked for and none was updated (an empty ledger is not a failed refresh).
    """
    total = updated = 0
    failed: set[str] = set()
    held_failed: set[str] = set()
    asked: set[str] = set()
    fx_ok: set[str] = set()
    for outcome in outcomes:
        r = outcome.results
        total += int(r.get("instruments", 0))
        updated += int(r.get("instruments_updated", 0))
        failed.update(r.get("instruments_failed", []))
        held_failed.update(r.get("held_failed", []))
        pairs = set(r.get("fx_pairs", []))
        asked |= pairs
        fx_ok |= pairs - set(r.get("fx_failed", []))
    return {
        "instruments": total,
        "instruments_updated": updated,
        "instruments_not_updated": len(failed),
        "instruments_failed": sorted(failed),
        "held_failed": sorted(held_failed),
        "fx_failed": sorted(asked - fx_ok),
        "all_failed": total > 0 and updated == 0,
    }


def _lagging_symbols(
    conn: sqlite3.Connection, fetched: list[str], held: set[str] | None
) -> list[str]:
    """HELD symbols the provider answered with a close OLDER than the run's newest (L24).

    Demo audit 2026-09-16: after 更新報價, 0056 stayed on 2026-09-11 while every other TW
    holding moved to 09-14. That is not a FAILURE — the fetch succeeded and stored the date
    the provider had — so ``held_failed`` was empty, the verdict was ``ok``, and the only
    place the user could learn it was the freshness table. The verdict stays ``ok`` (the
    data is exactly what the source has); the outcome just NAMES the laggards so the toast
    can. Compared within one run: the worklist is per market, so the newest date across
    ``fetched`` is the market's newest session, and a symbol behind it is behind its peers.

    Read straight from ``prices`` (a ``pricing`` table — the direct-SQL convention,
    ``architecture.md``): ``MAX(as_of_date)`` per instrument, an EXISTENCE-and-DATE read
    that no split can change. Degrades to ``[]`` when the table is absent (ledger-only DB).
    """
    if not fetched:
        return []
    marks = ",".join("?" for _ in fetched)
    try:
        rows = conn.execute(
            f"SELECT instrument, MAX(as_of_date) FROM prices WHERE instrument IN ({marks}) "
            "GROUP BY instrument",
            fetched,
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    latest = {str(r[0]): str(r[1]) for r in rows if r[1] is not None}
    if not latest:
        return []
    newest = max(latest.values())
    return sorted(
        s for s, d in latest.items()
        if d < newest and (held is None or s in held)
    )


def refresh_quotes_for(
    conn: sqlite3.Connection,
    market: Market,
    *,
    now: datetime,
    progress_job_id: str | None = None,
) -> JobOutcome:
    """Refresh latest quotes + FX for one market's instruments.

    FU-D46: when invoked as a scheduled job the caller passes its ``progress_job_id``
    so the in-flight registry shows the stage. Providers batch latest quotes per
    market (one routed call for the whole list), so one honest stage message —
    no fake per-symbol counter.

    Returns a :class:`JobOutcome` (M10-02), not the detail string: the ``RefreshSummary``
    is the only place the counts exist structurally, and flattening it here was the root
    cause — from this line on, the counts lived only inside a sentence.
    """
    instruments, fx_pairs = build_worklist(conn, market)
    if progress_job_id is not None:
        set_progress(
            progress_job_id, f"擷取 {market.value} 報價＋匯率（{len(instruments)} 檔）"
        )
    summary = refresh_quotes(
        conn, default_registry(conn), instruments, fx_pairs, now=now,
        factor_of=split_factor_fn(conn),
    )
    return _quote_outcome(conn, summary, instruments, fx_pairs)


def quotes_tw(conn: sqlite3.Connection, *, now: datetime) -> JobOutcome:
    return refresh_quotes_for(conn, Market.TW, now=now, progress_job_id="quotes_tw")


def quotes_us(conn: sqlite3.Connection, *, now: datetime) -> JobOutcome:
    return refresh_quotes_for(conn, Market.US, now=now, progress_job_id="quotes_us")


def quotes_my(conn: sqlite3.Connection, *, now: datetime) -> JobOutcome:
    return refresh_quotes_for(conn, Market.MY, now=now, progress_job_id="quotes_my")


def _refresh_benchmark_history(conn: sqlite3.Connection, start: date, *, now: datetime) -> str:
    """Refresh benchmark index history (FU-D27) — NEVER blocks instrument refresh.

    Benchmarks are fetched via the SAME ``registry.fetch_quote_history`` path as
    instruments (their refs route correctly through ``yf_symbol``) and stored under their
    stable ``prices.instrument`` keys (they are not registered instruments). A benchmark
    fetch failure degrades silently (logged + summarized) so a bad index fetch can never
    fail the daily instrument history job.

    ``factor_of`` IS bound (2026-09-10, site-architecture map D-11). Until then this call
    omitted it on the argument that "an index is never the subject of a corporate action" —
    true of ``^GSPC`` / ``^KLSE``, which can never carry an ``instruments`` row, and FALSE
    of ``0050``: an ETF the owner may also hold. A held 0050 with a SPLIT row is written by
    the instrument sweeps as ``close_raw × factor`` while this call wrote the SAME
    ``(instrument, as_of_date)`` rows as ``close_raw × 1`` — and it runs LAST in both jobs,
    so it always had the final word: every deep backfill quietly reverted the pre-split rows
    the reconcile had just repaired. With the ledger bound, the two writers produce
    byte-identical rows again (the property ``pricing/benchmarks.py`` relies on); for the
    two true indices the factor is the identity by construction, so nothing changes there.
    Bound once per call, never per ref (trap #21).
    """
    summary = _benchmark_history_summary(conn, start, now=now)
    return _BENCHMARK_FAILED if summary is None else _summarize(_empty_as_failed(summary))


def _benchmark_history_summary(
    conn: sqlite3.Connection, start: date, *, now: datetime
) -> RefreshSummary | None:
    """:func:`_refresh_benchmark_history`'s fetch, as data (``None`` = it raised)."""
    try:
        return refresh_history(
            conn, default_registry(conn), benchmark_refs(), start, now=now,
            factor_of=split_factor_fn(conn),
        )
    except Exception as exc:  # noqa: BLE001 - benchmark fetch must never block instrument refresh
        logger.warning("benchmark history refresh failed: %s", exc)
        return None


def _empty_as_failed(summary: RefreshSummary) -> RefreshSummary:
    """Fold "the provider answered with no bars" back into ``failed`` — the contract every
    caller of ``refresh_history`` had before DEF-067 ④ (2026-09-26) set it apart. Right for
    a multi-year backfill, where no series at all means the provider does not have the
    symbol; only the 7-day sweep (:func:`history_daily`) reads ``empty`` on its own."""
    if not summary.empty:
        return summary
    return summary.model_copy(update={"failed": [*summary.failed, *summary.empty], "empty": []})


#: The reason an unproven empty answer is counted as lost (DEF-067 ④, see history_daily).
_UNPROVEN_EMPTY = "來源回應空白，且本輪沒有任何標的從同一來源取得資料，視為無法連線"


def _trust_empty(
    registry: Registry, summary: RefreshSummary, market_of: dict[str, Market],
    answered: set[str],
) -> RefreshSummary:
    """Keep an ``empty`` symbol as 「區間內無 K 棒」 only when the run PROVES a provider of
    its market was reachable (it returned bars for something); otherwise it is lost.

    yfinance, first in every history chain, reports a network failure as an empty frame —
    it does not raise (``YfConfig.debug.hide_exceptions``). Trusting every empty answer would
    turn an all-providers-down night back into 成功, the defect DEF-067 was opened for.
    """
    if not summary.empty:
        return summary
    trusted: list[str] = []
    unproven: list[str] = []
    for sym in summary.empty:
        market = market_of.get(sym)
        chain = set(registry.capable_ids(DataType.QUOTE_HISTORY, market)) if market else set()
        (trusted if chain & answered else unproven).append(sym)
    return summary.model_copy(update={
        "empty": trusted,
        "failed": [*summary.failed, *unproven],
        "failed_reasons": {**summary.failed_reasons, **{u: _UNPROVEN_EMPTY for u in unproven}},
    })


def _summarize_history(summary: RefreshSummary) -> str:
    """:func:`_summarize` + 「N 項區間內無 K 棒（休市或已下市）：…」 (DEF-067 ④)."""
    text = _summarize(summary)
    if summary.empty:
        text += (f"；{len(summary.empty)} 項區間內無 K 棒（休市或已下市）："
                 f"{'、'.join(sorted(summary.empty))}")
    return text


def history_daily(conn: sqlite3.Connection, *, now: datetime) -> JobOutcome:
    """Backfill a recent history window for all instruments + benchmarks (FU-D27).

    Deep backfill is manual; this is the recent-window sweep. Benchmarks share the same
    7-day window and degrade silently. FU-D46: the registry routes quote HISTORY
    per instrument anyway (``Registry.fetch_quote_history`` is a per-ref loop), so the
    loop lives here and reports honest per-symbol progress; per-ref summaries merge
    into the same single summary as the old one-call form.
    """
    instruments, _ = build_worklist(conn, None)
    start = (now - timedelta(days=_HISTORY_LOOKBACK_DAYS)).date()
    registry = default_registry(conn)
    factor_of = split_factor_fn(conn)  # once for the whole sweep, never per symbol
    ok: dict[str, str] = {}
    failed: list[str] = []
    empty: list[str] = []
    total = len(instruments)
    for i, ref in enumerate(instruments, start=1):
        set_progress("history_daily", f"回補 {ref.symbol} ({i}/{total})")
        s = refresh_history(conn, registry, [ref], start, now=now, factor_of=factor_of)
        ok.update(s.ok)
        failed.extend(s.failed)
        empty.extend(s.empty)
    set_progress("history_daily", "回補基準指數")
    bench = _benchmark_history_summary(conn, start, now=now)
    # DEF-067 ④ (owner ruling 2026-09-26): only a PROVIDER failure is lost. An answer with
    # no bars in the 7-day window (a holiday closure, a delisted watchlist symbol) is
    # 「區間內無 K 棒」 — once the run proves a provider of that market answered at all
    # (``_trust_empty``: instruments AND benchmarks count as that evidence).
    answered = set(ok.values()) | (set(bench.ok.values()) if bench is not None else set())
    market_of = {ref.symbol: ref.market for ref in [*instruments, *benchmark_refs()]}
    summary = _trust_empty(
        registry, RefreshSummary(ok=ok, failed=failed, empty=empty, fetched_at=now),
        market_of, answered,
    )
    bench_text = (
        _BENCHMARK_FAILED if bench is None
        else _summarize_history(_trust_empty(registry, bench, market_of, answered))
    )
    # DEF-067: the verdict counts INSTRUMENTS only, derived as worklist − ok − trusted empty
    # (never parsed back out of ``failed``, which mixes keys with zh refusal lines). A
    # benchmark failure stays in the sentence and out of the verdict (FU-D27).
    lost = sum(1 for ref in instruments if ref.symbol not in ok and ref.symbol not in summary.empty)
    return sweep_outcome(
        f"{_summarize_history(summary)}・基準指數：{bench_text}",
        total=len(instruments), failed=lost,
    )


def dividends_daily(conn: sqlite3.Connection, *, now: datetime) -> JobOutcome:
    """Sweep dividend/ex-div events for all instruments.

    DEF-067 (2026-09-26): this returned the bare sentence, so ``_outcome_of`` recorded
    「成功　0 檔事件已更新，10 檔失敗（…）」 with every provider down. A symbol whose source
    answered with no dividend records (``summary.empty``, DEF-047) is not a failure.
    """
    instruments, _ = build_worklist(conn, None)
    set_progress("dividends_daily", f"掃描 {len(instruments)} 檔股利事件")
    summary = refresh_dividends(conn, default_registry(conn), instruments, now=now)
    # DEF-015: the SAME sentence as the 收件匣 scan — which symbol failed, and why.
    return dividend_sweep_outcome(summary, total=len(instruments))


def dividend_sweep_outcome(summary: RefreshSummary, *, total: int) -> JobOutcome:
    """The dividend sweep's verdict + THE sentence (``describe_refresh``) — shared by
    ``dividends_daily``, the inbox scan's fallback path and ``api/dividend_inbox.scan_job``
    so the three can never disagree about the same refresh."""
    return sweep_outcome(
        describe_refresh(summary), total=total, failed=len(set(summary.failed))
    )


# --- 待確認匯入 daily scan (R5 item 2, 2026-07-03) ------------------------------
# The full scan (event refresh + PENDING COUNT) lives in the api layer
# (api/dividend_inbox.scan_job) and is registered here at app startup — the same
# runner seam the insight jobs use, so scheduler/ never imports api/. A
# scheduler-only process without the runner falls back to the event refresh
# (the inbox computes on read, so items still appear).
DividendScanRunner = Callable[..., "str | JobOutcome"]
_DIVIDEND_SCAN_RUNNER: DividendScanRunner | None = None


def register_dividend_scan_runner(fn: DividendScanRunner | None) -> None:
    """Register (or clear with None) the dividend-inbox scan runner (app wiring seam)."""
    global _DIVIDEND_SCAN_RUNNER
    _DIVIDEND_SCAN_RUNNER = fn


# 月度快照 runner seam (R6 item 8) — the writer needs build_dashboard (portfolio
# via the api service), registered at app startup like the other runners.
SnapshotRunner = Callable[..., "str | JobOutcome"]
_SNAPSHOT_RUNNER: SnapshotRunner | None = None


def register_snapshot_runner(fn: SnapshotRunner | None) -> None:
    """Register (or clear with None) the monthly-snapshot runner (app wiring seam)."""
    global _SNAPSHOT_RUNNER
    _SNAPSHOT_RUNNER = fn


def snapshot_monthly(conn: sqlite3.Connection, *, now: datetime) -> JobOutcome:
    """Daily: upsert the current month's KPI snapshot (month-rollover = final).

    DEF-067 class scan: the runner's bare string stays ``ok`` — it writes the row or
    raises; a KPI it cannot compute is stored NULL (honest degradation), and the price /
    FX fetch that lost it is reported by its own quote job."""
    runner = _SNAPSHOT_RUNNER
    if runner is None:
        return _no_runner("月度快照")
    set_progress("snapshot_monthly", "寫入本月 KPI 快照")
    return _outcome_of(runner(conn, now=now))


# signal_scan runner seam (P2 batch 2): the scan reads pricing/portfolio + the rule engine
# and writes signal_states/alert_events; that orchestration lives in the api seam
# (api/signals_service.scan_signals), registered at app startup — so scheduler/ never
# imports api (architecture.md). A scheduler-only process without the runner is a safe
# no-op (state resumes seeding once the app wires it on the next scan).
SignalScanRunner = Callable[..., "str | JobOutcome"]
_SIGNAL_SCAN_RUNNER: SignalScanRunner | None = None


def register_signal_scan_runner(fn: SignalScanRunner | None) -> None:
    """Register (or clear with None) the signal-scan runner (app wiring seam)."""
    global _SIGNAL_SCAN_RUNNER
    _SIGNAL_SCAN_RUNNER = fn


def signal_scan(conn: sqlite3.Connection, *, now: datetime) -> JobOutcome:
    """Post-close: evaluate held-symbol rule signals → detect transitions → events.

    A separate static job (jobs here are one-purpose; the blueprint allows this or an
    alert_scan pre-step — the runner-seam job is the lowest-coupling option and is
    independently triggerable via ``POST /api/scheduler/jobs/signal_scan/run``). No runner
    wired → safe no-op summary.

    FU-D46 mirror (W6): when the runner accepts a ``progress`` keyword it receives the
    in-flight progress callback — the first post-upgrade scan replays the full price
    history into ``signal_history`` (minutes), and the owner watching the jobs page should
    see which symbol is being backfilled, not a silent spinner.
    """
    runner = _SIGNAL_SCAN_RUNNER
    if runner is None:
        return _no_runner("技術訊號掃描")
    set_progress("signal_scan", "掃描技術訊號")
    kwargs: dict[str, Any] = {"now": now}
    if _accepts_progress(runner):
        def _report(msg: str) -> None:
            set_progress("signal_scan", msg)

        kwargs["progress"] = _report
    return _outcome_of(runner(conn, **kwargs))


def dividend_inbox_scan(conn: sqlite3.Connection, *, now: datetime) -> JobOutcome:
    """Daily: refresh dividend events for acquired symbols + report pending count.

    The runner's verdict passes through (DEF-067 — ``scan_job`` returns a JobOutcome)."""
    set_progress("dividend_inbox_scan", "掃描配息事件")
    runner = _DIVIDEND_SCAN_RUNNER
    if runner is not None:
        return _outcome_of(runner(conn, now=now))
    acq = earliest_acquisitions(conn)
    instruments, _ = build_worklist(conn, None)
    refs = [r for r in instruments if r.symbol in acq]
    if not refs:
        return JobOutcome("ok", "無持倉可偵測")  # the inbox's own words for this case
    summary = refresh_dividends(conn, default_registry(conn), refs, now=now)
    return dividend_sweep_outcome(summary, total=len(refs))  # DEF-015: the one sentence


# --- External-snapshot ingest jobs (spec 20.4) --------------------------------
# Map each ingest job to the data source whose health it escalates on a fail streak.
_INGEST_JOB_SOURCE: dict[str, str] = {
    "finmind_chips_daily": "finmind",
    "finmind_valuation_daily": "finmind",
    "finmind_fundamentals_monthly": "finmind",
    "sentiment_daily": "yfinance",
    "index_quotes_daily": "yfinance",
    "consensus_daily": "yfinance",
    "fundamentals_daily": "yfinance",
    "fundamentals_av_weekly": "alphavantage",
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
    conn: sqlite3.Connection,
    job_id: str,
    fn: Callable[[], int],
    *,
    now: datetime,
    expected: int | None = None,
) -> JobOutcome:
    """Run one ingest, escalating source health to ``error`` on failure.

    On success returns a short summary (its ``job_runs`` row will log ``ok``, resetting
    the streak). A FinMind tier/quota error (spec 20.15.4) is a clear, actionable
    failure: it marks health ``error`` with the reason IMMEDIATELY (no 3-streak needed),
    writes no snapshot, then re-raises so ``run_job`` records the error row. Any other
    failure escalates health only when THIS run makes the trailing error streak reach the
    threshold (spec 20.12). Either way the exception re-raises for the ``job_runs`` log.

    ``expected`` (DEF-067, 2026-09-26) is for an ingest whose snapshot count is FIXED —
    sentiment is always VIX + Fear & Greed, the index job always one close set. There a
    shortfall can only be a lost fetch (``pricing/ingest.py`` turns each into a ``None`` and
    writes nothing), so 0 of N is ``error`` and fewer than N is ``partial``. A per-symbol
    ingest has no such number: its count mixes "the source failed" with "the source has no
    coverage for this symbol", and only ``pricing/ingest.py`` can tell them apart.
    """
    set_progress(job_id, "擷取外部快照資料")
    try:
        written = fn()
        detail = f"寫入 {written} 筆外部快照"
        if expected is None:
            return JobOutcome("ok", detail)
        return sweep_outcome(
            f"{detail}（應有 {expected} 筆）" if written < expected else detail,
            total=expected, failed=max(0, expected - written),
        )
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


def finmind_chips_daily(conn: sqlite3.Connection, *, now: datetime) -> JobOutcome:
    """Post-close: institutional + margin chips for the TW universe (FinMind)."""
    return _run_ingest(
        conn, "finmind_chips_daily", lambda: ingest.ingest_chips(conn, now=now), now=now
    )


def finmind_valuation_daily(conn: sqlite3.Connection, *, now: datetime) -> JobOutcome:
    """Daily: PER/PBR/yield valuation for the TW universe (FinMind)."""
    return _run_ingest(
        conn, "finmind_valuation_daily", lambda: ingest.ingest_valuation(conn, now=now),
        now=now,
    )


def finmind_fundamentals_monthly(conn: sqlite3.Connection, *, now: datetime) -> JobOutcome:
    """Monthly: revenue + financial statements for the TW universe (FinMind)."""
    return _run_ingest(
        conn, "finmind_fundamentals_monthly",
        lambda: ingest.ingest_fundamentals(conn, now=now), now=now,
    )


def sentiment_daily(conn: sqlite3.Connection, *, now: datetime) -> JobOutcome:
    """Daily: VIX (yfinance ^VIX) + CNN Fear & Greed snapshots — always two (DEF-067)."""
    return _run_ingest(
        conn, "sentiment_daily", lambda: ingest.ingest_sentiment(conn, now=now), now=now,
        expected=2,
    )


def index_quotes_daily(conn: sqlite3.Connection, *, now: datetime) -> JobOutcome:
    """Trading-day: TAIEX/SPX/KLCI index closes (yfinance) — one snapshot (DEF-067)."""
    return _run_ingest(
        conn, "index_quotes_daily", lambda: ingest.ingest_index(conn, now=now), now=now,
        expected=1,
    )


def consensus_daily(conn: sqlite3.Connection, *, now: datetime) -> JobOutcome:
    """Daily: analyst target-price + rating-distribution snapshots for all instruments.

    Slot: 09:10 Asia/Taipei — analyst consensus is a slow-moving, timezone-agnostic
    signal (not a market close), so it runs once in the morning, staggered just after
    the 08:00 sentiment job and before the intraday quote crons, on all days (yfinance
    serves whatever the latest consensus is regardless of any single market's session).
    """
    return _run_ingest(
        conn, "consensus_daily", lambda: ingest.ingest_consensus(conn, now=now), now=now
    )


def fundamentals_daily(conn: sqlite3.Connection, *, now: datetime) -> JobOutcome:
    """Daily: fundamentals blocks from yfinance + Finnhub, UNION semantics (W3, AI-D16).

    Every enabled source writes its own snapshot row per symbol (no fallback chain — a
    keyless Finnhub simply writes nothing). Fundamentals move slowly, so one morning run
    right after consensus_daily is enough; the yfinance leg derives its ratios from the
    light statement endpoints at the fetch seam (never Ticker.info).
    """
    return _run_ingest(
        conn, "fundamentals_daily",
        lambda: ingest.ingest_fundamentals_union(
            conn, now=now, sources=("yfinance", "finnhub")
        ),
        now=now,
    )


# Fundamentals AV-leg runner seam (W3, AI-D16): the Saturday Alpha Vantage pass covers
# HELD symbols only (free quota 25 calls/day cannot survive a full-universe pass), and
# the held set is a portfolio/ replay result that scheduler/ + pricing/ cannot compute.
# The app registers the api-side runner at startup — the same injection pattern as
# signal_scan / alert_compute (architecture.md); no runner registered -> safe no-op.
FundamentalsRunner = Callable[..., int]
_FUNDAMENTALS_RUNNER: FundamentalsRunner | None = None


def register_fundamentals_runner(fn: FundamentalsRunner | None) -> None:
    """Register (or clear with None) the fundamentals AV-leg runner (app wiring seam)."""
    global _FUNDAMENTALS_RUNNER
    _FUNDAMENTALS_RUNNER = fn


def fundamentals_av_weekly(conn: sqlite3.Connection, *, now: datetime) -> JobOutcome:
    """Saturday: Alpha Vantage fundamentals blocks for HELD symbols, via the registered
    runner (``api.fundamentals_service.run_fundamentals_av``)."""
    runner = _FUNDAMENTALS_RUNNER
    if runner is None:
        return _no_runner("週六基本面")
    return _run_ingest(
        conn, "fundamentals_av_weekly", lambda: runner(conn, now=now), now=now
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


# Alert-card held-set seam (DEF-041, owner ruling 2026-09-24): the on_alert 持倉提點 card is
# dispatched only for a symbol currently HELD, and "held" is read from the COMPUTED book — the
# dashboard replay's holdings with shares != 0, across accounts — which ``scheduler/`` may not
# build (architecture.md). The app registers ``api.insight_service.held_symbols_for_alerts``.
# Unregistered while an insight runner IS registered, the dispatcher is told "cannot tell"
# (None): symbol alerts are held back unconsumed and the run detail says so — never carded
# for a watchlist symbol, never silently dropped. Deliberately NOT the quote job's
# ``_HELD_SYMBOLS_FN`` above: that one is the REGISTRY's 「持有」
# (``holdings.held_among`` — a position today or on any later ledger date, DEF-075), while
# this door reads the VALUATION book cut at today (DEF-041); they differ only on a symbol
# whose every row is still ahead.
AlertHeldFn = Callable[..., Set[str]]
_ALERT_HELD_FN: AlertHeldFn | None = None


def register_alert_held_fn(fn: AlertHeldFn | None) -> None:
    """Register (or clear with None) the alert-card held-set reader (app wiring seam)."""
    global _ALERT_HELD_FN
    _ALERT_HELD_FN = fn


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


# What a skipped (non-card) alert is about, for the run detail (DEF-037).
_SCOPE_ZH: dict[str | None, str] = {
    "account": "帳戶", "sector": "產業", "currency": "幣別", "task": "洞察任務",
    None: "範圍未標示",
}


def _skipped_note(skipped: list[alerts_bridge.AlertEvent]) -> str:
    """「；略過 N 條非個股預警（不產個股卡）：匯率漂移 帳戶 {account:moomoo_my}、…」 or "".

    DEF-062: a rule is named by the one name table (``shared.alert_rule_names``), never by
    its id — this detail is what 排程中心 prints for the run."""
    if not skipped:
        return ""
    items = []
    for ev in skipped:
        subject = ev.symbol or ""
        if ev.scope == "account" and subject:
            subject = account_ref(subject)  # the fetch layer renders the display name
        items.append(
            f"{rule_name(ev.rule_id)} {_SCOPE_ZH.get(ev.scope, ev.scope or '')} {subject}".strip()
        )
    return f"；略過 {len(skipped)} 條非個股預警（不產個股卡）：{'、'.join(items)}"


def _not_held_note(not_held: list[alerts_bridge.AlertEvent]) -> str:
    """「；略過 N 條觀察標的預警（未持有，不產卡）：高點回撤 1234、…」 or "" (DEF-041, DEF-062)."""
    if not not_held:
        return ""
    items = [f"{rule_name(ev.rule_id)} {ev.symbol or ''}".strip() for ev in not_held]
    return f"；略過 {len(not_held)} 條觀察標的預警（未持有，不產卡）：{'、'.join(items)}"


def _held_unknown_note(held_unknown: list[alerts_bridge.AlertEvent]) -> str:
    """The symbol alerts held back because the held set could not be read (DEF-041)."""
    if not held_unknown:
        return ""
    return (
        f"；{len(held_unknown)} 條個股預警暫不派發（無法判定是否持有，下次掃描重試）"
    )


def alert_scan(conn: sqlite3.Connection, *, now: datetime) -> JobOutcome:
    """Compute alerts → record events → dispatch subscribing on_alert combos (R7).

    The registered insight runner produces one short-horizon card per subscribing combo
    per (rule, symbol), 24h-debounced. Returns a short summary for the ``job_runs`` detail.

    DEF-037 (2026-09-23): each event is recorded with its STRUCTURED scope + subject + the
    rule engine's own title/detail (``Alert.scope`` / ``Alert.subject``), never with a
    subject recovered from the id's suffix. That recovery (``_alert_symbol``, now deleted)
    handed ``fx_drift:moomoo_my``'s account id to the per-symbol card as its symbol; the
    dispatcher now sends only ``symbol`` / ``portfolio`` scopes to a card, and this detail
    names every alert it therefore skipped.
    """
    alerts_bridge.ensure_tables(conn)
    set_progress("alert_scan", "計算預警規則")
    alerts = _compute_alerts_for_scan(conn, now=now)
    rules_seen: list[str] = []
    for alert in alerts:
        alerts_bridge.record_event(
            conn, rule_id=alert.rule, symbol=alert.subject, now=now,
            href=alert.href,  # FU-D17: stored so the push can carry a clickable deep link
            scope=alert.scope, title=alert.title, detail=alert.detail,
        )
        if alert.rule not in rules_seen:
            rules_seen.append(alert.rule)
    runner = _INSIGHT_RUNNER
    dispatched = 0
    skipped: list[alerts_bridge.AlertEvent] = []
    not_held: list[alerts_bridge.AlertEvent] = []
    held_unknown: list[alerts_bridge.AlertEvent] = []
    if runner is not None:
        set_progress("alert_scan", "派發 AI 預警卡")
        held_fn = _ALERT_HELD_FN

        def _held() -> Set[str] | None:
            # DEF-041: the computed book's held set, read lazily (only when a symbol alert
            # with a subscriber is waiting); None = not wired → the dispatcher holds back.
            return held_fn(conn, now=now) if held_fn is not None else None

        result = alerts_bridge.dispatch_alert_events_ex(
            conn, runner, now=now, held_symbols=_held
        )
        dispatched, skipped = result.dispatched, result.skipped
        not_held, held_unknown = result.not_held, result.held_unknown
    else:
        # No runner wired (scheduler-only process): still consume events so they do not
        # pile up; cards are produced once the app wires the runner on the next scan.
        for event in alerts_bridge.unconsumed_events(conn):
            alerts_bridge.mark_consumed(conn, event.id)
    # WP 3B: push unnotified events (this scan's + signal_scan's 14:55 events) to the
    # enabled channels. Uses the SEPARATE notified_at marker (independent of `consumed`
    # above). Wrapped so a push-path failure can never fail the alert scan itself.
    # DEF-067: a crashed push path no longer reads 成功 — the alerts were computed and
    # recorded, but nobody was told, so the run is ``partial`` (never ``error``: the scan
    # itself still never fails over the push path, F3.6).
    status = "ok"
    try:
        set_progress("alert_scan", "推播通知")
        notify_detail = notify_dispatch.dispatch_notifications(conn, now=now)
    except Exception as exc:  # noqa: BLE001 - the push path must never break the scan
        logger.warning("notify dispatch failed in alert_scan: %s", exc)
        notify_detail = "推播失敗（預警已記錄）"
        status = "partial"
    # DEF-062: the fired rules by name (「單一標的集中度、波動突升」), never by id.
    # DEF-073: the sentence is zh — it read 「18 alert(s) […], 7 dispatched」.
    names = "、".join(rule_name(r) for r in rules_seen)
    return JobOutcome(status, (
        f"預警 {len(alerts)} 條" + (f"（{names}）" if names else "")
        + f"，派發 AI 預警卡 {dispatched} 張；"
        f"{notify_detail}{_skipped_note(skipped)}{_not_held_note(not_held)}"
        f"{_held_unknown_note(held_unknown)}"
    ))


# --- Loop-2 evaluate + Loop-3 calibrate jobs (spec 04.4 / 4.5) ----------------
# Both dispatch to a runner registered by the app (price-/master-bearing reads live in
# ``api/insight_service.py``); a scheduler-only process with no runner is a safe no-op.


def evaluate_insights(conn: sqlite3.Connection, *, now: datetime) -> str:
    """Loop-2 daily: score every due insight via the registered evaluate runner (spec 4.4).

    The runner (``insight_service.evaluate_due``) reads price-at-create vs price-at-due,
    feeds the actual into the pure quant scorer, runs master narrative scoring (skipped when
    master unset), and writes ``insight_evaluations`` rows. Missing actual → pending_data
    (anti-poison). No runner wired → safe no-op summary (cards/evaluation resume once the
    app wires it). The detail is the runner's summary (「評分 N 張、延後 M 張；晉升：…」, R6
    DEF-073 — it read 「evaluate pass complete」 whatever the pass did).
    """
    runner = _EVALUATION_RUNNER
    if runner is None:
        return "評分執行器未接線，未執行"
    set_progress("evaluate_insights", "評分到期洞察")
    summary = runner(conn, now=now)
    return str(summary) if summary is not None else "評分完成"


def generate_calibrations(conn: sqlite3.Connection, *, now: datetime) -> str:
    """Loop-3 weekly: generate calibration versions via the registered calibration runner.

    The runner (``insight_service.generate_calibrations_for_all``) applies the §4.5 triggers
    + the min_samples gate + the §4.8 validator. Master unset → the runner pauses (no crash);
    no runner wired → safe no-op summary. The detail is the runner's summary (「產生 N 版；
    略過 M 個任務（…樣本 k／門檻 8）；驗證器拒絕 J 版（…）」, R6 DEF-073 — it read
    「calibration pass complete」 for all three).
    """
    runner = _CALIBRATION_RUNNER
    if runner is None:
        return "校正執行器未接線，未執行"
    set_progress("generate_calibrations", "產生校準版本")
    summary = runner(conn, now=now)
    return str(summary) if summary is not None else "校正完成"


def _accepts_progress(fn: Callable[..., object]) -> bool:
    """True when *fn* can take a ``progress`` keyword (additive runner seam, FU-D46).

    Signature inspection keeps the seam additive-safe: an older/stub runner without
    the parameter is simply called without it (never a TypeError probe, which could
    mask a genuine TypeError from inside the runner).
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):  # builtins / exotic callables — assume not
        return False
    return "progress" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )


def news_run_outcome(result: object, *, symbols: int | None = None) -> JobOutcome:
    """The verdict + zh sentence of one news-pipeline run (DEF-067 / DEF-073).

    「AI 整理 2 則，僅存標題 1 則，已收錄略過 3 則；AI 額度用盡，提前結束」. It read
    「news: organized 2, headline 1, skipped 3 (budget stop)」 under a 成功 chip even when the
    budget cut the run short. Shared with the manual ``POST /api/news/run`` worker
    (``symbols`` = its universe size) so both doors say the same thing.
    """
    if not isinstance(result, dict):
        return JobOutcome("ok", "新聞管線完成")
    text = (f"AI 整理 {result.get('organized', 0)} 則，"
            f"僅存標題 {result.get('headline_only', 0)} 則，"
            f"已收錄略過 {result.get('skipped_existing', 0)} 則")
    if result.get("refetched"):
        text += f"，重抓舊文 {result['refetched']} 則"
    if symbols is not None:
        text = f"{symbols} 檔標的：{text}"
    if result.get("stopped_budget"):
        return JobOutcome("partial", text + "；AI 額度用盡，提前結束")
    return JobOutcome("ok", text)


def news_daily(conn: sqlite3.Connection, *, now: datetime) -> JobOutcome:
    """Batch ④ nightly: run the news pipeline (discover→fetch→organize→store) via the
    registered runner (``news_service.run_news_daily``). No runner wired → safe no-op.

    FU-D46: when the registered runner accepts a ``progress`` keyword (the real
    ``run_news_daily`` does — additive optional param), it receives a callback that
    updates this job's in-flight progress message per pipeline step; a stub/legacy
    runner without the parameter is called exactly as before.
    """
    runner = _NEWS_RUNNER
    if runner is None:
        return _no_runner("新聞管線")
    set_progress("news_daily", "執行新聞管線")
    kwargs: dict[str, Any] = {"now": now}
    if _accepts_progress(runner):
        def _report(msg: str) -> None:
            set_progress("news_daily", msg)

        kwargs["progress"] = _report
    return news_run_outcome(runner(conn, **kwargs))


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
    set_progress("backup_daily", "資料庫完整性檢查")
    ok, detail = backup_ops.check_integrity()
    if not ok:
        logger.warning("backup_daily integrity_check failed: %s", detail)
        raise RuntimeError(f"資料庫完整性檢查未通過：{detail}")
    if _prior_consecutive_failures(conn, "backup_daily") >= _FAIL_STREAK_THRESHOLD:
        logger.warning(
            "backup_daily recovered after %d+ consecutive failed run(s); backup resuming",
            _FAIL_STREAK_THRESHOLD,
        )
    set_progress("backup_daily", "寫入備份檔")
    path = backup_ops.backup_database(now=now)
    return f"備份完成：{path.name}"


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
    # Fundamentals union (W3, AI-D16): yfinance + Finnhub daily; Alpha Vantage on
    # Saturday, HELD symbols only, via the registered runner (free quota 25/day).
    JobSpec(
        "fundamentals_daily", fundamentals_daily, "20 9 * * *", "Asia/Taipei", True,
        "Fundamentals blocks: yfinance + Finnhub union (all instruments)",
    ),
    JobSpec(
        "fundamentals_av_weekly", fundamentals_av_weekly, "40 9 * * sat", "Asia/Taipei",
        True, "Alpha Vantage fundamentals (held symbols only; free quota 25/day)",
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
    # Digests (P3 batch 3): the daily close summary fires just after the alert scan (15:00)
    # so it reflects that day's alerts/signals; the weekly action list fires Sunday evening.
    JobSpec(
        "digest_daily", digest_daily, "10 15 * * mon-fri", "Asia/Taipei", True,
        "Daily close digest (assemble + push)",
    ),
    JobSpec(
        "digest_weekly", digest_weekly, "0 17 * * sun", "Asia/Taipei", True,
        "Weekly action list (assemble + push)",
    ),
]

DEFAULT_BOARD: dict[Market, str] = {Market.US: "", Market.MY: ".KL", Market.TW: "TWSE"}
_DEFAULT_BOARD = DEFAULT_BOARD  # back-compat alias (internal callers below)

# Reporting-currency FX pairs the providers are asked for (reporting ccy = TWD).
# Public: the api-layer instrument service reuses the same fixed set.
# MYR/TWD is NOT here since 2026-09-16 (owner ruling, demo audit M1 (b)): it is DERIVED
# from the two USD legs by `pricing/cross.py` right after every FX write, so the three
# pairs always close a triangle. Asking a provider for it too would put two writers on one
# row; `fetched_pairs` filters it out even if someone adds it back.
REPORTING_FX_PAIRS: list[FxPair] = fetched_pairs([
    FxPair(base=Currency.USD, quote=Currency.TWD),
    FxPair(base=Currency.USD, quote=Currency.MYR),
    FxPair(base=Currency.MYR, quote=Currency.TWD),
])
_FX_PAIRS = REPORTING_FX_PAIRS  # back-compat alias (internal callers below)


def build_worklist(
    conn: sqlite3.Connection, market: Market | None
) -> tuple[list[InstrumentRef], list[FxPair]]:
    """Build the pricing work-list from the ``instruments`` table.

    Board comes from the stored ``instruments.board`` column when set, else the
    deterministic market default (US ``""`` / MY ``".KL"`` / TW ``"TWSE"``). FX pairs
    are the fixed reporting-currency set. Archived symbols (FU-D13) are excluded — a
    stopped-tracking symbol should not consume quote/history/dividend fetch budget. The
    filter is the ONE shared definition (``shared/instrument_scope.py``, DEF-064) that the
    snapshot-ingest, insight, signal, news and alert universes read too.
    """
    refs = [
        InstrumentRef(symbol=t.symbol, market=t.market,
                      board=t.board or _DEFAULT_BOARD[t.market])
        for t in tracked_instruments(conn, market=market)
    ]
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
    summary = refresh_quotes(
        conn, default_registry(conn), [ref], _FX_PAIRS, now=now,
        factor_of=split_factor_fn(conn),
    )
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


def _backfill_benchmarks(
    conn: sqlite3.Connection, registry: Registry, start: date, *, now: datetime
) -> str:
    """Backfill benchmark index history from ``start`` (FU-D27) — silent-degrade.

    Uses the shared ``registry`` (no second construction) and the same idempotent history
    path as instruments. A benchmark failure is logged + summarized, never raised, so a bad
    index backfill can never fail the whole-portfolio backfill job.

    ``factor_of`` is bound here too (2026-09-10): the deep backfill rewrites the WHOLE
    history, so this was the call that undid every reconcile of a held 0050's pre-split
    rows. Mechanism in :func:`_refresh_benchmark_history`.
    """
    try:
        summary = refresh_history(
            conn, registry, benchmark_refs(), start, now=now, factor_of=split_factor_fn(conn),
        )
        return _summarize(_empty_as_failed(summary))  # multi-year window: see the helper
    except Exception as exc:  # noqa: BLE001 - benchmark backfill must never fail the job
        logger.warning("benchmark backfill failed: %s", exc)
        return _BENCHMARK_FAILED


# FU-D46: the pseudo job id backfill progress reports under. The manual action
# (POST /api/actions/backfill-history) runs synchronously on the request thread and is
# NOT marked in the in-flight registry, so these calls are no-ops there today; any
# future wrapper that marks this id (async backfill) lights them up unchanged.
_BACKFILL_PROGRESS_ID = "backfill_history"


def _backfill_prices_per_symbol(
    conn: sqlite3.Connection,
    registry: Registry,
    groups: list[tuple[date, list[InstrumentRef]]],
    total: int,
    *,
    now: datetime,
) -> RefreshSummary:
    """Per-symbol price backfill over ``(start, refs)`` groups, reporting progress.

    ``Registry.fetch_quote_history`` routes per instrument anyway, so single-ref calls
    are behaviorally identical to the old batched call; the loop lives here purely so
    每檔 progress (「回補 {sym} (i/n)」) is honest. Summaries merge into one.

    This is the DEEP backfill — the exact operation spec §5.1 names as the artifact's
    origin ("import the ledger, then backfill history") — so the factor is bound here,
    once for the whole run rather than once per symbol.
    """
    factor_of = split_factor_fn(conn)
    ok: dict[str, str] = {}
    failed: list[str] = []
    done = 0
    for start, refs in groups:
        for ref in refs:
            done += 1
            set_progress(_BACKFILL_PROGRESS_ID, f"回補 {ref.symbol} ({done}/{total})")
            s = _empty_as_failed(  # multi-year window: no series at all IS a failure
                refresh_history(conn, registry, [ref], start, now=now, factor_of=factor_of)
            )
            ok.update(s.ok)
            failed.extend(s.failed)
    return RefreshSummary(ok=ok, failed=failed, fetched_at=now)


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
        p_summary = _backfill_prices_per_symbol(
            conn, registry, [(default_start, instruments)], len(instruments), now=now
        )
        set_progress(_BACKFILL_PROGRESS_ID, "回補匯率")
        f_summary = refresh_fx_history(conn, registry, fx_pairs, default_start, now=now)
        # Benchmarks (FU-D27): uniform-window explicit-days runs use the same window.
        set_progress(_BACKFILL_PROGRESS_ID, "回補基準指數")
        b_summary = _backfill_benchmarks(conn, registry, default_start, now=now)
        return (
            f"價格：{_summarize(p_summary)}・匯率：{_summarize(f_summary)}・"
            f"基準指數：{b_summary}"
        )

    acq = earliest_acquisitions(conn)
    by_start: dict[date, list[InstrumentRef]] = {}
    for ref in instruments:
        first = acq.get(ref.symbol)
        start = min(default_start, first) if first is not None else default_start
        by_start.setdefault(start, []).append(ref)
    p_summary = _backfill_prices_per_symbol(
        conn, registry, sorted(by_start.items()), len(instruments), now=now
    )

    flow = earliest_ledger_flow(conn)
    fx_start = min(default_start, flow) if flow is not None else default_start
    set_progress(_BACKFILL_PROGRESS_ID, "回補匯率")
    f_summary = refresh_fx_history(conn, registry, fx_pairs, fx_start, now=now)
    # Benchmarks (FU-D27): no acquisition date — backfill from the earliest ledger flow
    # (like the FX pairs) so the "all" TWR window has a benchmark on the portfolio's full
    # span; the floor otherwise. Silent-degrade — a benchmark fetch never fails the job.
    set_progress(_BACKFILL_PROGRESS_ID, "回補基準指數")
    b_summary = _backfill_benchmarks(conn, registry, fx_start, now=now)
    return (
        f"價格：{_summarize(p_summary)}・匯率（自 {fx_start.isoformat()}）："
        f"{_summarize(f_summary)}・基準指數（自 {fx_start.isoformat()}）：{b_summary}"
    )


def _jobs_by_id() -> dict[str, JobSpec]:
    return {j.id: j for j in JOBS}


def failure_detail(exc: BaseException) -> str:
    """The ``job_runs.detail`` of a run that raised — a sentence, never the bare ``str(exc)``.

    DEF-030 (2026-09-23): the 排程中心 printed 「失敗 'insight:10'」 because the async worker
    wrote ``str(KeyError('insight:10'))`` — the repr of a dict key — straight into the status
    chip. The class name stays (it is what an operator searches the log for); the message
    follows when there is one. Every worker in this module finalizes a failed row here.
    """
    name = type(exc).__name__
    msg = str(exc).strip()
    return f"執行失敗：{name}：{msg}" if msg else f"執行失敗：{name}"


JobKind = Literal["system", "insight"]


def job_kind(conn: sqlite3.Connection, job_id: str) -> JobKind | None:
    """What runs *job_id*: ``insight`` (a kind=insight binding), ``system`` (a registered
    static job), or None — nothing in this process can run it.

    DEF-030: the ONE dispatch question. The cron path (:func:`dispatch_job`) always asked
    it; the manual 立即執行 worker (:func:`run_job_func`) looked every id up in the STATIC
    registry, which by construction never holds a dynamic ``insight:<id>`` row — so every
    manual run of a scheduled insight task failed with ``KeyError``. The router asks this
    before it accepts a run, so an id nothing can run is a 404, not a background failure.
    """
    if _insight_payload(conn, job_id) is not None:
        return "insight"
    if job_id in _jobs_by_id():
        return "system"
    return None


def insight_task_of(conn: sqlite3.Connection, job_id: str) -> int | None:
    """The insight task a kind=insight schedule row runs (its ``payload``), or None."""
    return _insight_payload(conn, job_id)


def unknown_job_message(job_id: str) -> str:
    """The zh sentence for an id nothing can run (the 404 message and the worker's detail)."""
    return f"找不到排程工作「{job_id}」：它不是已登錄的系統工作，也不是 AI 洞察任務的排程"


_NO_INSIGHT_RUNNER = "執行失敗：AI 洞察執行器未載入（此程序沒有註冊洞察執行器）"


# --- In-flight job registry (FU-D36 / FU-D46) ---------------------------------
# A process-local map of the job_ids whose func is EXECUTING right now, so the status
# endpoint (api/routers/scheduler.py::job_status) can honestly distinguish 執行中 (the
# func is running) from 已排入 (a run row exists but the worker has not picked it up
# yet — the brief window between ``start_job_run`` on the request thread and the daemon
# thread marking itself running). Both the synchronous cron path (:func:`run_job`) and
# the async manual path (:func:`run_job_func`) mark/clear it; a lock guards every access.
# FU-D46: the value carries ``{since, progress}`` — jobs update ``progress`` mid-run via
# :func:`set_progress` so the status endpoint can show WHAT a running job is doing.
# This is bookkeeping only — no business logic, never persisted (a process restart clears
# it, which is correct: a dropped daemon thread is not still running).
_INFLIGHT_LOCK = threading.Lock()


@dataclass
class _Inflight:
    """Registry value: when the func started executing + its live progress message."""

    since: str
    progress: str | None = None


_INFLIGHT_JOBS: dict[str, _Inflight] = {}


def _mark_running(job_id: str) -> None:
    """Mark a job as executing (idempotent under the lock; a re-mark resets progress)."""
    with _INFLIGHT_LOCK:
        _INFLIGHT_JOBS[job_id] = _Inflight(since=datetime.now(UTC).isoformat())


def _clear_running(job_id: str) -> None:
    """Clear a job's executing mark (no-op if absent). Call ONLY after the run row is
    finalized, so the status endpoint never briefly reads a finished run as 已排入.
    Clearing drops the progress message too — progress can never outlive the run."""
    with _INFLIGHT_LOCK:
        _INFLIGHT_JOBS.pop(job_id, None)


def set_progress(job_id: str, msg: str) -> None:
    """Update a RUNNING job's live progress message (FU-D46). Lock-guarded.

    A strict no-op when the job is not marked in-flight (e.g. the same function
    invoked synchronously outside the run wrappers), so wiring progress calls into
    shared code paths is always safe. Transient bookkeeping only — never persisted;
    it dies with the run (:func:`_clear_running`).
    """
    with _INFLIGHT_LOCK:
        entry = _INFLIGHT_JOBS.get(job_id)
        if entry is not None:
            entry.progress = msg


def running_job_ids() -> set[str]:
    """A thread-safe snapshot copy of the job_ids whose func is executing right now."""
    with _INFLIGHT_LOCK:
        return set(_INFLIGHT_JOBS)


def running_progress() -> dict[str, str | None]:
    """Thread-safe snapshot: running job_id -> its current progress message (or None)."""
    with _INFLIGHT_LOCK:
        return {job_id: entry.progress for job_id, entry in _INFLIGHT_JOBS.items()}


def run_job(conn: sqlite3.Connection, job_id: str, *, now: datetime) -> int:
    """Execute one job, logging start/finish to ``job_runs``; return its run id.

    A job exception is caught and logged as ``status="error"`` (never re-raised), so
    one failing job cannot crash the scheduler or other jobs. The ``job_runs`` row is
    inserted before the job func runs, so the returned id is always valid regardless of
    job success/failure (consumed by the manual-refresh action to report ``run_ids``).

    FU-D36: the job is marked in the in-flight registry while its func runs and cleared
    only AFTER the row is finalized, so a concurrent status poll reads it as 執行中
    throughout the run (never briefly as 已排入 on the way out).
    """
    return run_job_outcome(conn, job_id, now=now)[0]


def run_job_outcome(
    conn: sqlite3.Connection, job_id: str, *, now: datetime
) -> tuple[int, JobOutcome]:
    """:func:`run_job`, also handing back the recorded outcome (M10-02).

    The synchronous refresh-quotes door needs the verdict and the structured counts the
    job produced, and the only alternative was to read ``job_runs.detail`` back and parse
    the numbers out of the sentence. The row is written exactly as before.
    """
    spec = _jobs_by_id()[job_id]
    cur = conn.execute(
        "INSERT INTO job_runs (job_id, started_at) VALUES (?, ?)",
        (job_id, now.isoformat()),
    )
    run_id = int(cur.lastrowid or 0)
    conn.commit()
    _mark_running(job_id)
    try:
        try:
            outcome = _outcome_of(spec.func(conn, now=now))
        except Exception as exc:  # noqa: BLE001 — swallow + log; never crash the scheduler
            outcome = JobOutcome("error", failure_detail(exc))
        # finished_at shares *now*'s timezone (M1 fix): a UTC finish next to a +08:00 start
        # reads as a negative-duration run in any naive display.
        conn.execute(
            "UPDATE job_runs SET finished_at = ?, status = ?, detail = ? WHERE id = ?",
            (datetime.now(tz=now.tzinfo or UTC).isoformat(), outcome.status, outcome.detail,
             run_id),
        )
        conn.commit()
    finally:
        _clear_running(job_id)
    return run_id, outcome


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


def start_run(conn: sqlite3.Connection, job_id: str, *, now: datetime) -> int:
    """Pre-insert the ``running`` row of a manual run, in the shape its KIND writes (DEF-030).

    A kind=insight row gets :func:`start_insight_run`'s shape (``payload`` = the task id), so
    a run started from the 排程中心 is the same row the task door and the cron path write —
    the per-task run history (``GET /api/insight-tasks/{id}/runs``) reads it by payload.
    """
    payload = _insight_payload(conn, job_id)
    if payload is not None:
        return start_insight_run(conn, payload, now=now)
    return start_job_run(conn, job_id, now=now)


def run_job_func(job_id: str, *, now: datetime) -> None:
    """Execute a job in a fresh session, finalizing its latest running row.

    For the async ``/run`` endpoint: the request handler already inserted the running
    row via :func:`start_run`; this opens its OWN connection (the request conn is closed
    by then) and finalizes it. This is a fire-and-forget daemon-thread target, so the
    WHOLE body is exception-safe — any failure (job func, or even the surrounding DB
    access) is swallowed so it never crashes the worker thread.

    DEF-030: dispatches by KIND, exactly like the cron path — a kind=insight row goes to
    :func:`_execute_insight` (the registered insight runner finalizes THIS row), a static
    job to its registry func. It used to look every id up in the static registry and write
    the resulting ``KeyError`` text into the row. An id nothing can run is finalized as an
    error sentence rather than left ``running`` (the router refuses it with 404 first).
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
            run_id = int(rid["id"])
            payload = _insight_payload(conn, job_id)
            if payload is not None:
                _execute_insight(
                    conn, job_id, payload, now=now, run_id=run_id,
                    trigger=InsightTrigger(source="manual"),
                )
                return
            spec = _jobs_by_id().get(job_id)
            if spec is None:
                finish_job_run(
                    conn, run_id, status="error",
                    detail=f"執行失敗：{unknown_job_message(job_id)}", now=now,
                )
                return
            # FU-D36: mark 執行中 once we own the row; the window before this (from
            # start_run inserting the row) is the honest 已排入 state. Clear only
            # after finish_job_run commits, so the poll never reads a done run as queued.
            _mark_running(job_id)
            try:
                try:
                    outcome = _outcome_of(spec.func(conn, now=now))
                except Exception as exc:  # noqa: BLE001 — swallow + log; never crash the thread
                    outcome = JobOutcome("error", failure_detail(exc))
                finish_job_run(
                    conn, run_id, status=outcome.status, detail=outcome.detail, now=now
                )
            finally:
                _clear_running(job_id)
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
    opens its OWN connection and runs :func:`_execute_insight` with ``run_id`` so the same row
    is finalized. Fully exception-safe (a fire-and-forget worker must never raise out).
    DEF-030: it used to return silently with no runner registered (or swallow a raising
    runner), leaving the row ``running`` forever — and the 409 overlap guard then refused
    every later run of the task.
    """
    try:
        with session() as conn:
            _execute_insight(
                conn, insight_job_id(insight_type_id), insight_type_id, now=now,
                run_id=run_id, trigger=InsightTrigger(source="manual"),
            )
    except Exception:  # noqa: BLE001 — background worker must never raise out of the thread
        return


def _execute_insight(
    conn: sqlite3.Connection,
    job_id: str,
    insight_type_id: int,
    *,
    now: datetime,
    run_id: int | None,
    trigger: InsightTrigger,
) -> None:
    """Run one kind=insight job — the ONE executor behind every door (DEF-030).

    Three doors reach it: the cron fire (:func:`dispatch_job`, ``run_id=None`` — the runner
    inserts its own completed row, ``trigger.source="schedule"``), the 排程中心's 立即執行
    (:func:`run_job_func`) and the task door (:func:`run_insight_func`), both with the id of
    the row the request pre-inserted (``source="manual"``). The runner writes the row the
    same way on all three (``generate._write_job_run``), and a failure is recorded the same
    way on all three: the pre-inserted row is finalized with :func:`failure_detail`; on the
    cron path a completed error row is written when the runner wrote none — before, a
    failing SCHEDULED task logged an exception and left nothing in the 排程中心 at all.
    """
    runner = _INSIGHT_RUNNER
    if runner is None:
        if run_id is not None:
            finish_job_run(conn, run_id, status="error", detail=_NO_INSIGHT_RUNNER, now=now)
        else:
            logger.info("kind=insight job %s fired but no runner is registered; skipping", job_id)
        return
    before = _latest_run_id(conn, job_id)
    # FU-D36/D46: mark 執行中 (+ progress) for the status endpoint, on every door alike; the
    # runner finalizes its own row, so clear only after it returns (finalize-then-clear).
    _mark_running(job_id)
    try:
        set_progress(job_id, "產生 AI 洞察卡")
        kwargs: dict[str, Any] = {"now": now, "trigger": trigger}
        if run_id is not None:
            kwargs["run_id"] = run_id
        try:
            runner(conn, insight_type_id, **kwargs)
        except Exception as exc:  # noqa: BLE001 — a runner failure must never crash the caller
            logger.exception("insight runner failed for %s", job_id)
            _record_insight_failure(
                conn, job_id, insight_type_id, exc, now=now, run_id=run_id, since_id=before
            )
    finally:
        _clear_running(job_id)


def _latest_run_id(conn: sqlite3.Connection, job_id: str) -> int:
    row = conn.execute("SELECT MAX(id) AS m FROM job_runs WHERE job_id = ?", (job_id,)).fetchone()
    return int(row["m"]) if row is not None and row["m"] is not None else 0


def _record_insight_failure(
    conn: sqlite3.Connection,
    job_id: str,
    insight_type_id: int,
    exc: BaseException,
    *,
    now: datetime,
    run_id: int | None,
    since_id: int,
) -> None:
    """Write the failed insight run to ``job_runs`` exactly once (DEF-030).

    Pre-inserted row (manual doors): finalize it unless the runner already did. Cron door:
    insert a completed ``error`` row unless the runner wrote one for this invocation (a row
    newer than ``since_id`` — e.g. the main run was recorded and a later shadow pass raised).
    """
    detail = failure_detail(exc)
    if run_id is not None:
        row = conn.execute(
            "SELECT finished_at FROM job_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is not None and row["finished_at"] is None:
            finish_job_run(conn, run_id, status="error", detail=detail, now=now)
        return
    if _latest_run_id(conn, job_id) > since_id:
        return
    finished = datetime.now(tz=now.tzinfo or UTC).isoformat()
    conn.execute(
        "INSERT INTO job_runs (job_id, started_at, finished_at, status, detail, payload, "
        "cost_usd, is_shadow) VALUES (?, ?, ?, 'error', ?, ?, '0', 0)",
        (job_id, now.isoformat(), finished, detail, str(insight_type_id)),
    )
    conn.commit()


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
            "前一次執行尚未完成，本次排程觸發已略過", str(payload),
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
        if _INSIGHT_RUNNER is None:
            logger.info("kind=insight job %s fired but no runner is registered; skipping", job_id)
            return None
        if latest_run_unfinished(conn, job_id):
            logger.info(
                "kind=insight job %s fired while a run is in flight; skipping (overlap guard)",
                job_id,
            )
            _record_skipped_overlap(conn, job_id, payload, now=now)
            return None
        # DEF-030: the SAME executor as both manual doors (in-flight mark + progress, the
        # runner call, the failure record) — only the trigger and the absent run_id differ.
        _execute_insight(
            conn, job_id, payload, now=now, run_id=None,
            trigger=InsightTrigger(source="schedule"),
        )
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
