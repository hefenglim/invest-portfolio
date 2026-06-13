"""FastAPI app factory: lifespan (DB + scheduler), /api routers, static web/ frontend."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from portfolio_dash.api.errors import register_error_handlers
from portfolio_dash.api.routers import dashboard, health, instruments, ledgers
from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.scheduler.jobs import ensure_scheduler_seeded
from portfolio_dash.scheduler.runtime import build_scheduler
from portfolio_dash.shared.db import session

_WEB_DIR = Path(__file__).resolve().parents[2] / "web"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    with session() as conn:
        bootstrap_db(conn)
        ensure_scheduler_seeded(conn)
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
    if _WEB_DIR.is_dir():
        app.mount("/", StaticFiles(directory=_WEB_DIR, html=True), name="web")
    return app
