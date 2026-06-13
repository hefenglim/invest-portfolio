"""FastAPI app factory: lifespan (DB + scheduler), /api routers, static web/ frontend."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from portfolio_dash.api.errors import register_error_handlers
from portfolio_dash.api.routers import (
    accounts,
    actions,
    dashboard,
    datasources,
    export,
    health,
    input_center,
    instruments,
    ledgers,
    llm_settings,
    scheduler,
    strategy,
    symbol,
)
from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.scheduler.jobs import ensure_scheduler_seeded
from portfolio_dash.scheduler.runtime import build_scheduler
from portfolio_dash.shared.db import session
from portfolio_dash.strategy.rules_config import ensure_alert_rules_seeded

_WEB_DIR = Path(__file__).resolve().parents[2] / "web"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    with session() as conn:
        bootstrap_db(conn)
        ensure_scheduler_seeded(conn)
        ensure_alert_rules_seeded(conn)
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
    app = FastAPI(title="portfolio-dash", lifespan=_lifespan)
    register_error_handlers(app)
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
    if _WEB_DIR.is_dir():
        app.mount("/", StaticFiles(directory=_WEB_DIR, html=True), name="web")
    return app
