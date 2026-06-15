"""FastAPI app factory: lifespan (DB + scheduler), /api routers, static web/ frontend."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles

from portfolio_dash.api.auth_store import ensure_auth_seeded, require_session
from portfolio_dash.api.errors import register_error_handlers
from portfolio_dash.api.insight_service import (
    evaluate_due as insight_evaluate_due,
)
from portfolio_dash.api.insight_service import (
    generate_calibrations_for_all as insight_generate_calibrations,
)
from portfolio_dash.api.insight_service import run_for_id as insight_run_for_id
from portfolio_dash.api.routers import (
    accounts,
    actions,
    auth,
    dashboard,
    datasources,
    export,
    health,
    input_center,
    insights,
    instruments,
    ledgers,
    llm_settings,
    prompts,
    scheduler,
    strategy,
    symbol,
    users,
)
from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.llm_insight.alerts_bridge import ensure_tables as ensure_alert_events_tables
from portfolio_dash.llm_insight.composer_store import ensure_seeded as ensure_composer_seeded
from portfolio_dash.llm_insight.evaluations_store import ensure_tables as ensure_evaluations_tables
from portfolio_dash.llm_insight.insights_store import ensure_tables as ensure_insights_tables
from portfolio_dash.llm_insight.system_prompt import ensure_system_prompt_seeded
from portfolio_dash.pricing import snapshots_store
from portfolio_dash.scheduler.jobs import (
    ensure_scheduler_seeded,
    register_calibration_runner,
    register_evaluation_runner,
    register_insight_runner,
)
from portfolio_dash.scheduler.runtime import build_scheduler
from portfolio_dash.shared.db import session
from portfolio_dash.shared.logging_config import configure_logging
from portfolio_dash.strategy.rules_config import ensure_alert_rules_seeded

_WEB_DIR = Path(__file__).resolve().parents[2] / "web"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Configure structured JSON-lines logging once on real-server boot (spec 19.4).
    # In lifespan (not create_app) so hermetic TestClient(app) tests — which skip
    # lifespan — never write log files.
    configure_logging()
    with session() as conn:
        bootstrap_db(conn)
        snapshots_store.ensure_tables(conn)  # external_snapshots (spec 20.4)
        ensure_scheduler_seeded(conn)  # also seeds the 5 ingest jobs' schedule rows
        ensure_alert_rules_seeded(conn)
        ensure_auth_seeded(conn)
        ensure_system_prompt_seeded(conn)
        ensure_composer_seeded(conn)  # insight-composer tables (spec 04a)
        ensure_insights_tables(conn)  # insights cards table (spec 04b)
        ensure_alert_events_tables(conn)  # alert_events + dispatch log (spec 04b R7)
        ensure_evaluations_tables(conn)  # insight_evaluations table (spec 04c)
    # Wire the kind=insight scheduler dispatch + manual-run daemon to the api service seam
    # (scheduler triggers only; it never imports api — spec 04.2 / architecture.md).
    register_insight_runner(insight_run_for_id)
    # Wire the Loop-2/3 evolution runners (price-/master-bearing reads live in the api seam).
    register_evaluation_runner(insight_evaluate_due)
    register_calibration_runner(insight_generate_calibrations)
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
    app.include_router(auth.router, prefix="/api")
    app.include_router(users.router, prefix="/api")
    app.include_router(health.router, prefix="/api")
    app.include_router(dashboard.router, prefix="/api")
    app.include_router(instruments.router, prefix="/api")
    app.include_router(ledgers.router, prefix="/api")
    app.include_router(input_center.router, prefix="/api")
    app.include_router(actions.router, prefix="/api")
    app.include_router(accounts.router, prefix="/api")
    app.include_router(datasources.router, prefix="/api")
    app.include_router(llm_settings.router, prefix="/api")
    app.include_router(strategy.router, prefix="/api")
    app.include_router(symbol.router, prefix="/api")
    app.include_router(export.router, prefix="/api")
    app.include_router(scheduler.router, prefix="/api")
    app.include_router(prompts.router, prefix="/api")
    app.include_router(insights.router, prefix="/api")
    if _WEB_DIR.is_dir():
        app.mount("/", StaticFiles(directory=_WEB_DIR, html=True), name="web")
    return app
