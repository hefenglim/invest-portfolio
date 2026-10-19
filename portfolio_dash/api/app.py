"""FastAPI app factory: lifespan (DB + scheduler), /api routers, static web/ frontend."""

import os
import sqlite3
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Request, Response

from portfolio_dash.api import action_log, settlement_service
from portfolio_dash.api.auth_store import ensure_auth_seeded, require_session, session_user
from portfolio_dash.api.composition import bind_runtime
from portfolio_dash.api.deps import get_conn
from portfolio_dash.api.dividend_inbox import ensure_tables as ensure_dividend_inbox_tables
from portfolio_dash.api.errors import register_error_handlers
from portfolio_dash.api.gzip import GZipMiddleware
from portfolio_dash.api.option_expiry import ensure_tables as ensure_option_expiry_tables
from portfolio_dash.api.read_only import is_read_only
from portfolio_dash.api.rebates import ensure_tables as ensure_rebate_tables
from portfolio_dash.api.routers import (
    accounts,
    actions,
    auth,
    broker_import,
    cash,
    dashboard,
    datasources,
    db_stats,
    digest,
    dividend_inbox,
    export,
    fee_rules,
    health,
    input_center,
    insights,
    instruments,
    ledger_audit,
    ledgers,
    llm_fail_log,
    llm_settings,
    news,
    notify,
    option_expiry,
    performance,
    prompts,
    rebates,
    scheduler,
    settlement,
    signals,
    snapshots_router,
    strategy,
    symbol,
    system_log,
    ui_prefs,
    users,
    whatsnew,
)
from portfolio_dash.api.settlement_service import on_boot as settlement_on_boot
from portfolio_dash.api.snapshots import ensure_table as ensure_snapshots_table
from portfolio_dash.api.static_cache import StaticCacheFiles
from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.moomoo_merge import (
    migrate_moomoo_accounts,
    needs_moomoo_merge,
)
from portfolio_dash.llm_insight.alerts_bridge import ensure_tables as ensure_alert_events_tables
from portfolio_dash.llm_insight.alerts_bridge import suppress_stale_quota_low
from portfolio_dash.llm_insight.composer_store import ensure_seeded as ensure_composer_seeded
from portfolio_dash.llm_insight.evaluations_store import ensure_tables as ensure_evaluations_tables
from portfolio_dash.llm_insight.insights_store import ensure_tables as ensure_insights_tables
from portfolio_dash.llm_insight.system_prompt import ensure_system_prompt_seeded
from portfolio_dash.news.organizer_prompt import ensure_news_prompt_seeded
from portfolio_dash.news.store import get_news_connection
from portfolio_dash.ops import digest as digest_ops
from portfolio_dash.ops import notify as notify_ops
from portfolio_dash.ops.backup import pre_write_snapshot
from portfolio_dash.portfolio import settlement_store
from portfolio_dash.portfolio.settlement_models import ACTOR_BOOT
from portfolio_dash.portfolio.settlement_store import create_tables as create_settle_tables
from portfolio_dash.pricing import datasources_store, snapshots_store
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.scheduler.jobs import (
    close_abandoned_runs,
    ensure_scheduler_seeded,
    migrate_job_runs_results,
)
from portfolio_dash.scheduler.runtime import build_scheduler
from portfolio_dash.shared import flags, result_cache
from portfolio_dash.shared.clock import app_now
from portfolio_dash.shared.config import get_settings
from portfolio_dash.shared.db import install_actor, session
from portfolio_dash.shared.flight import begin_write, end_write
from portfolio_dash.shared.llm_fail_log import (
    ensure_table as ensure_llm_fail_log_table,
)
from portfolio_dash.shared.logging_config import configure_logging
from portfolio_dash.shared.ui_prefs import ensure_ui_prefs_seeded
from portfolio_dash.shared.whatsnew import ensure_whatsnew_seeded
from portfolio_dash.strategy.rules_config import ensure_alert_rules_seeded
from portfolio_dash.strategy.signal_history import (
    ensure_table as ensure_signal_history_table,
)
from portfolio_dash.strategy.signal_states import ensure_table as ensure_signal_states_table
from portfolio_dash.strategy.target_weights import ensure_target_weights_seeded

_WEB_DIR = Path(__file__).resolve().parents[2] / "web"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Configure structured JSON-lines logging once on real-server boot (spec 19.4).
    # In lifespan (not create_app) so hermetic TestClient(app) tests — which skip
    # lifespan — never write log files.
    configure_logging()
    # The PD_* flags (shared/flags.py) are read per call; read them all once here so a bad
    # value stops the boot with its own message instead of answering 500 on each request.
    flags.current()
    # Every boot write is stamped `system:boot` in the settlement store's change trail
    # (spec §7 "Who made the change"): a no-op on a fresh database until the trail exists,
    # installed again right after the settle DDL below.
    with session(actor=ACTOR_BOOT) as conn:
        # First-run bootstrap: create EVERY table the running app reads + seed config, so a
        # fresh 0-byte DB is usable out of the box (guarded by tests/contract/
        # test_first_run_bootstrap.py — an empty DB hid the missing `prices` table until the
        # first holding queried it).
        bootstrap_db(conn)  # ledger tables (accounts, instruments, transactions, ...)
        create_pricing_tables(conn)  # prices + fx_rates (the dashboard valuation reads these)
        datasources_store.ensure_seeded(conn)  # data_sources + tiers + health (spec 14)
        # Batch B one-time Moomoo merge (2026-07-21): relabel every ledger row + rules binding
        # from the two legacy account ids (moomoo_my_us / moomoo_my_my) to the merged dual-market
        # moomoo_my, atomically. Runs BEFORE seed_accounts so the merge (not the re-seed) owns the
        # legacy-row rewrite; seed_accounts then idempotently re-asserts the canonical config. The
        # S0 predicate gates a pre-migration backup snapshot to EXACTLY one per actual migration —
        # a reboot after success sees no legacy ids and takes neither snapshot nor migration.
        if needs_moomoo_merge(conn):
            pre_write_snapshot(prefix="pre_migrate_", now=app_now())
            migrate_moomoo_accounts(conn)
        # Seed the broker accounts from the single canonical config (DEFAULT_ACCOUNTS). Idempotent
        # upsert: adding a future account THERE auto-seeds it on next launch; today there is no
        # account-edit UI so the defaults are fixed config. (When an add/edit-account UI lands,
        # switch this to a settings_meta-gated seed-once so launches don't clobber user edits.)
        seed_accounts(conn)
        snapshots_store.ensure_tables(conn)  # external_snapshots (spec 20.4)
        ensure_scheduler_seeded(conn)  # also seeds the 5 ingest jobs' schedule rows
        ensure_alert_rules_seeded(conn)
        ensure_target_weights_seeded(conn)  # per-symbol target weights (P3 batch 2, D8)
        ensure_auth_seeded(conn)
        ensure_system_prompt_seeded(conn)
        ensure_news_prompt_seeded(conn)  # editable news-organizer prompt (batch ④)
        ensure_composer_seeded(conn)  # insight-composer tables (spec 04a)
        ensure_insights_tables(conn)  # insights cards table (spec 04b)
        ensure_alert_events_tables(conn)  # alert_events + dispatch log (spec 04b R7)
        # 3B one-time cleanup: quota_low is now gated on ai_active — neutralize any
        # pre-gate pending quota_low event so no stale AI card / push fires retroactively.
        suppress_stale_quota_low(conn, now=app_now())
        ensure_evaluations_tables(conn)  # insight_evaluations table (spec 04c)
        ensure_signal_states_table(conn)  # signal_states derived cache (P2 batch 2)
        ensure_signal_history_table(conn)  # signal_history per-day states (W6, AI-D27)
        ensure_llm_fail_log_table(conn)  # llm_fail_log capture ring (AI-D64)
        notify_ops.ensure_seeded(conn)  # notify_config single-row + one-time topic (WP 3B)
        # DEF-065 on an upgraded database: a rule newer than the stored config is filled in
        # HERE, so GET /api/notify/config never persists the self-heal (v0.1.29 release rehearsal).
        notify_ops.heal_config(conn)
        digest_ops.ensure_seeded(conn)  # digests + digest_config single-row (P3 batch 3)
        # DEF-065: every table a GET reads is created HERE, never by the GET. These six were
        # created (and ui_prefs / whatsnew SEEDED) by their first request — a write on a read
        # path, and for the two config_store categories a race: settings.html fires three
        # GET /api/ui-prefs at once, and two first seeds collided on settings_meta (500).
        # tests/contract/test_def065_get_never_writes.py boots a fresh DB and calls every GET
        # route to keep it that way.
        ensure_ui_prefs_seeded(conn)
        ensure_whatsnew_seeded(conn)
        ensure_dividend_inbox_tables(conn)  # pending_dividend_skips
        ensure_rebate_tables(conn)  # rebate_skips
        ensure_option_expiry_tables(conn)  # option_expiry_skips (options spec §7.2)
        ensure_snapshots_table(conn)  # portfolio_snapshots (月度快照)
        action_log.ensure_table(conn)  # the log the middleware writes and 系統紀錄 reads
        # The settlement store (PERF spec 2026-10-18 §6, §7), after every table it borrows.
        # Order matters (plan W0.6): (1) every step above; (2) the settle DDL and the
        # job_runs.results migration (P3); (3) the trigger set, verified or recreated, on this
        # `system:boot` session — after every migration, since the ledgers' columns decide each
        # trigger's text; (4) the settlement's boot hook; (5) LAST, record
        # `trigger_schema_version` — no boot DDL may move `schema_version` after it, or every
        # read would find the store unverified.
        create_settle_tables(conn)
        migrate_job_runs_results(conn)
        close_abandoned_runs(conn, now=app_now())  # job rows left by a gone process
        install_actor(conn, ACTOR_BOOT)
        trigger_check = settlement_store.install_or_verify(
            conn,
            calc_version_changed=not settlement_store.store_state(
                conn, reporting=get_settings().reporting_currency).calc_ok,
        )
        settlement_plan = settlement_on_boot(conn, trigger_check=trigger_check)
        settlement_store.record_schema_version(conn)
    # What the boot's trigger check found (read by tests and diagnostics; never by a route).
    app.state.settle_trigger_check = trigger_check
    # The news library lives in its own file (news/store.py); its first open creates and
    # migrates it, so open it once here rather than inside the first GET /api/news.
    get_news_connection().close()
    # Every scheduler seam (the twelve runners + the settlement's two + the job worker),
    # bound in ONE place shared with the job worker child (PERF spec §16).
    bind_runtime()
    scheduler = None
    if os.environ.get("PD_DISABLE_SCHEDULER") != "1":
        scheduler = build_scheduler()
        scheduler.start()
    app.state.scheduler = scheduler
    # A push held through quiet hours is sent by a one-shot job on the live scheduler.
    settlement_service.bind_scheduler(scheduler)
    # The boot run starts only now, after the boot session COMMITTED: the child reads its
    # request from its own connection (inline mode runs it here, synchronously).
    settlement_service.start_after_boot(settlement_plan)
    try:
        yield
    finally:
        settlement_service.shutdown()
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        # The result cache holds a READ-ONLY connection per database for the life of the
        # process, and a read-only connection cannot checkpoint: left open, the last close no
        # longer folds the WAL back and deletes it. Close them, then checkpoint, so a clean
        # stop leaves the database in one file (a backup restored by file copy must never meet
        # a stale `-wal`; fold verification 2026-10-19).
        result_cache.reset()
        with session() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def create_app() -> FastAPI:
    app = FastAPI(
        title="portfolio-dash", lifespan=_lifespan,
        dependencies=[Depends(require_session)],
    )
    register_error_handlers(app)

    # 系統操作記錄 (2026-07-03, item 8): record every mutating /api call — actor,
    # zh action label, endpoint, HTTP outcome, duration; NEVER bodies. Best-effort
    # on its own session: a logging failure must never break the request.
    @app.middleware("http")
    async def _action_log_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not action_log.should_log(request.method, request.url.path):
            return await call_next(request)
        start = time.perf_counter()
        response = await call_next(request)
        try:
            duration_ms = int((time.perf_counter() - start) * 1000)

            def _write(conn_w: sqlite3.Connection) -> None:
                token = request.cookies.get("pd_session")
                username = session_user(conn_w, token) if token else None
                action_log.record(
                    conn_w, ts=datetime.now(UTC), username=username,
                    method=request.method, path=request.url.path,
                    status=response.status_code, duration_ms=duration_ms,
                    # DEF-049: the route's own words for what it did (the batch undo's
                    # batch / rows / acknowledgements), when it set any.
                    detail=getattr(request.state, action_log.DETAIL_ATTR, None),
                )

            # Respect the get_conn dependency override so the log lands in the SAME
            # DB the routes use (hermetic tests inject an in-memory golden conn).
            override = app.dependency_overrides.get(get_conn)
            if override is not None:
                _write(override())
            else:
                with session() as conn:
                    _write(conn)
        except Exception:  # noqa: BLE001 — the log is an observer, never a gate
            pass
        return response

    # Shared builds and the result cache (`shared/flight.py` 2026-10-13, `shared/
    # result_cache.py` PERF 2026-10-18): no request joins or caches a dashboard build while a
    # mutating request runs, and none joins one that started before it — so a page reloaded
    # right after 儲存 never receives an answer computed before the save. Every method but
    # GET / HEAD / OPTIONS counts, except the declared read-only POSTs (`api/read_only.py`:
    # 試算, rebalance preview, prompt preview), which write nothing and so share like a GET.
    # Since PERF-S10 (2026-10-18) `get_conn` is function-scoped, so the request's commit runs
    # before the response starts, inside this window; `shared/db.session()` still brackets its
    # own commit in a window, which covers the sessions opened outside a request (threads,
    # jobs).
    @app.middleware("http")
    async def _write_window_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if is_read_only(request.method, request.url.path):
            return await call_next(request)
        begin_write()
        try:
            return await call_next(request)
        finally:
            end_write()

    app.include_router(auth.router, prefix="/api")
    app.include_router(users.router, prefix="/api")
    app.include_router(health.router, prefix="/api")
    app.include_router(dashboard.router, prefix="/api")
    app.include_router(instruments.router, prefix="/api")
    app.include_router(ledgers.router, prefix="/api")
    app.include_router(input_center.router, prefix="/api")
    app.include_router(broker_import.router, prefix="/api")
    app.include_router(dividend_inbox.router, prefix="/api")
    app.include_router(cash.router, prefix="/api")
    app.include_router(rebates.router, prefix="/api")
    app.include_router(option_expiry.router, prefix="/api")
    app.include_router(actions.router, prefix="/api")
    app.include_router(accounts.router, prefix="/api")
    app.include_router(fee_rules.router, prefix="/api")
    app.include_router(datasources.router, prefix="/api")
    app.include_router(llm_settings.router, prefix="/api")
    app.include_router(strategy.router, prefix="/api")
    app.include_router(symbol.router, prefix="/api")
    app.include_router(export.router, prefix="/api")
    app.include_router(scheduler.router, prefix="/api")
    app.include_router(settlement.router, prefix="/api")
    app.include_router(signals.router, prefix="/api")
    app.include_router(llm_fail_log.router, prefix="/api")
    app.include_router(system_log.router, prefix="/api")
    app.include_router(db_stats.router, prefix="/api")
    app.include_router(ledger_audit.router, prefix="/api")
    app.include_router(ui_prefs.router, prefix="/api")
    app.include_router(snapshots_router.router, prefix="/api")
    app.include_router(prompts.router, prefix="/api")
    app.include_router(news.router, prefix="/api")
    app.include_router(insights.router, prefix="/api")
    app.include_router(notify.router, prefix="/api")
    app.include_router(performance.router, prefix="/api")
    app.include_router(digest.router, prefix="/api")
    app.include_router(whatsnew.router, prefix="/api")
    if _WEB_DIR.is_dir():
        # `no-cache` for pages and unversioned files, `immutable` for an asset requested with
        # the current `?v=` stamp (`api/static_cache.py`, PERF spec §13).
        app.mount("/", StaticCacheFiles(directory=_WEB_DIR, html=True), name="web")
    # Outermost layer: compresses every JSON / text response >= 1 KiB, never a ZIP
    # (`api/gzip.py`, PERF spec §13).
    app.add_middleware(GZipMiddleware)
    return app
