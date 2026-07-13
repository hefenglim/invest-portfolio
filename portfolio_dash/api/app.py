"""FastAPI app factory: lifespan (DB + scheduler), /api routers, static web/ frontend."""

import os
import sqlite3
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from starlette.types import Scope

from portfolio_dash.api import action_log
from portfolio_dash.api.alert_inputs import scan_alert_compute
from portfolio_dash.api.auth_store import ensure_auth_seeded, require_session, session_user
from portfolio_dash.api.deps import get_conn
from portfolio_dash.api.dividend_inbox import scan_job as dividend_scan_job
from portfolio_dash.api.errors import register_error_handlers
from portfolio_dash.api.insight_service import (
    evaluate_due as insight_evaluate_due,
)
from portfolio_dash.api.insight_service import (
    generate_calibrations_for_all as insight_generate_calibrations,
)
from portfolio_dash.api.insight_service import run_for_id as insight_run_for_id
from portfolio_dash.api.news_service import run_news_daily
from portfolio_dash.api.routers import (
    accounts,
    actions,
    auth,
    cash,
    dashboard,
    datasources,
    db_stats,
    dividend_inbox,
    export,
    health,
    input_center,
    insights,
    instruments,
    ledgers,
    llm_settings,
    news,
    notify,
    prompts,
    scheduler,
    signals,
    snapshots_router,
    strategy,
    symbol,
    system_log,
    ui_prefs,
    users,
)
from portfolio_dash.api.signals_service import scan_signals as signal_scan_runner
from portfolio_dash.api.snapshots import snapshot_job
from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.llm_insight.alerts_bridge import ensure_tables as ensure_alert_events_tables
from portfolio_dash.llm_insight.composer_store import ensure_seeded as ensure_composer_seeded
from portfolio_dash.llm_insight.evaluations_store import ensure_tables as ensure_evaluations_tables
from portfolio_dash.llm_insight.insights_store import ensure_tables as ensure_insights_tables
from portfolio_dash.llm_insight.system_prompt import ensure_system_prompt_seeded
from portfolio_dash.news.organizer_prompt import ensure_news_prompt_seeded
from portfolio_dash.ops import notify as notify_ops
from portfolio_dash.pricing import datasources_store, snapshots_store
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.scheduler.jobs import (
    ensure_scheduler_seeded,
    register_alert_compute_runner,
    register_calibration_runner,
    register_dividend_scan_runner,
    register_evaluation_runner,
    register_insight_runner,
    register_news_runner,
    register_signal_scan_runner,
    register_snapshot_runner,
)
from portfolio_dash.scheduler.runtime import build_scheduler
from portfolio_dash.shared.db import session
from portfolio_dash.shared.logging_config import configure_logging
from portfolio_dash.strategy.rules_config import ensure_alert_rules_seeded
from portfolio_dash.strategy.signal_states import ensure_table as ensure_signal_states_table
from portfolio_dash.strategy.target_weights import ensure_target_weights_seeded

_WEB_DIR = Path(__file__).resolve().parents[2] / "web"


class _NoCacheStaticFiles(StaticFiles):
    """StaticFiles that adds ``Cache-Control: no-cache`` to every response (2026-07-07).

    Bare StaticFiles sends only ETag/Last-Modified — NO Cache-Control — so browsers fall
    back to HEURISTIC freshness (~10% of the asset's age since Last-Modified) and can keep
    serving a cached ``web/*.js`` for days without ever revalidating. After a deploy that
    ships an HTML page together with a new helper it calls (e.g. insights.html →
    ``fmt.aiAttrib``), a returning browser then mixes the fresh HTML with a STALE cached
    script and the page dies client-side (live incident 2026-07-07: insights/news/index
    lost every AI card to a swallowed ``f.aiAttrib is not a function``).

    ``no-cache`` keeps caching but forces conditional revalidation on every use: an ETag
    304 while unchanged, the new body immediately after every future deploy. This is the
    class fix that fits the no-build-step rule (there is no bundler to fingerprint
    filenames); the ``?v=<version>`` query on the HTML tags (see web/*.html + the
    ``test_static_cache_discipline`` contract tests) additionally flushes clients that
    cached assets BEFORE this header existed.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Configure structured JSON-lines logging once on real-server boot (spec 19.4).
    # In lifespan (not create_app) so hermetic TestClient(app) tests — which skip
    # lifespan — never write log files.
    configure_logging()
    with session() as conn:
        # First-run bootstrap: create EVERY table the running app reads + seed config, so a
        # fresh 0-byte DB is usable out of the box (guarded by tests/contract/
        # test_first_run_bootstrap.py — an empty DB hid the missing `prices` table until the
        # first holding queried it).
        bootstrap_db(conn)  # ledger tables (accounts, instruments, transactions, ...)
        create_pricing_tables(conn)  # prices + fx_rates (the dashboard valuation reads these)
        datasources_store.ensure_seeded(conn)  # data_sources + tiers + health (spec 14)
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
        ensure_evaluations_tables(conn)  # insight_evaluations table (spec 04c)
        ensure_signal_states_table(conn)  # signal_states derived cache (P2 batch 2)
        notify_ops.ensure_seeded(conn)  # notify_config single-row + one-time topic (WP 3B)
    # Wire the kind=insight scheduler dispatch + manual-run daemon to the api service seam
    # (scheduler triggers only; it never imports api — spec 04.2 / architecture.md).
    register_insight_runner(insight_run_for_id)
    # Wire the Loop-2/3 evolution runners (price-/master-bearing reads live in the api seam).
    register_evaluation_runner(insight_evaluate_due)
    register_calibration_runner(insight_generate_calibrations)
    # 待確認匯入 daily scan (R5): full scan (events + pending count) via the api seam.
    register_dividend_scan_runner(dividend_scan_job)
    # 月度快照 (R6 item 8): nightly current-month KPI upsert via the api seam.
    register_snapshot_runner(snapshot_job)
    # News pipeline (batch ④): nightly fetch + AI-organize into the separate news DB.
    register_news_runner(run_news_daily)
    # Rule-signal scan (P2 batch 2): held-symbol signal evaluation + transition events.
    register_signal_scan_runner(signal_scan_runner)
    # Alert-compute (P3 batch 2): the FULL rule set (market-risk rules need pricing/consensus
    # reads that live in the api seam) — the scheduler's alert_scan runs through this.
    register_alert_compute_runner(scan_alert_compute)
    scheduler = None
    if os.environ.get("PD_DISABLE_SCHEDULER") != "1":
        scheduler = build_scheduler()
        scheduler.start()
    app.state.scheduler = scheduler
    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)


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

    app.include_router(auth.router, prefix="/api")
    app.include_router(users.router, prefix="/api")
    app.include_router(health.router, prefix="/api")
    app.include_router(dashboard.router, prefix="/api")
    app.include_router(instruments.router, prefix="/api")
    app.include_router(ledgers.router, prefix="/api")
    app.include_router(input_center.router, prefix="/api")
    app.include_router(dividend_inbox.router, prefix="/api")
    app.include_router(cash.router, prefix="/api")
    app.include_router(actions.router, prefix="/api")
    app.include_router(accounts.router, prefix="/api")
    app.include_router(datasources.router, prefix="/api")
    app.include_router(llm_settings.router, prefix="/api")
    app.include_router(strategy.router, prefix="/api")
    app.include_router(symbol.router, prefix="/api")
    app.include_router(export.router, prefix="/api")
    app.include_router(scheduler.router, prefix="/api")
    app.include_router(signals.router, prefix="/api")
    app.include_router(system_log.router, prefix="/api")
    app.include_router(db_stats.router, prefix="/api")
    app.include_router(ui_prefs.router, prefix="/api")
    app.include_router(snapshots_router.router, prefix="/api")
    app.include_router(prompts.router, prefix="/api")
    app.include_router(news.router, prefix="/api")
    app.include_router(insights.router, prefix="/api")
    app.include_router(notify.router, prefix="/api")
    if _WEB_DIR.is_dir():
        app.mount("/", _NoCacheStaticFiles(directory=_WEB_DIR, html=True), name="web")
    return app
