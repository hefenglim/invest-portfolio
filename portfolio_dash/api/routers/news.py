"""News browse API (batch ④) + manual fetch trigger (P3 batch 3 · 3C).

Read-only over ``news.db``: a filtered/paginated list of organized news with per-item
token + cost, aggregate totals for cost assessment, and the distinct stocks/sources that
populate the filter dropdowns. Money/cost is a Decimal STRING on the wire (the frontend
never computes). The organizing itself happens OFF the request thread — nightly in the
``news_daily`` job, or on-demand via ``POST /api/news/run`` (async 202 + background thread;
NEVER synchronously on page load — the LLM batch-only invariant, llm-insight.md).
"""

import sqlite3
import threading
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from portfolio_dash.api import news_service
from portfolio_dash.api.deps import get_conn, get_now
from portfolio_dash.api.errors import error_body
from portfolio_dash.news import store as news_store
from portfolio_dash.scheduler.jobs import finish_job_run, latest_run_unfinished, start_job_run
from portfolio_dash.shared.db import session
from portfolio_dash.shared.wire import decimal_str

router = APIRouter()

_MAX_LIMIT = 200
# Manual + nightly runs share ONE in-flight guard (the news_daily job id): a manual fetch is
# refused (409) while the nightly job runs and vice-versa, so two pipelines never overlap.
_NEWS_JOB_ID = "news_daily"


def _item_wire(n: news_store.OrganizedNews) -> dict[str, Any]:
    return {
        "title": n.title,
        "date": n.news_date,
        "summary": n.body_summary,
        "related_stocks": n.related_stocks,
        "source": n.source,
        "lang": n.lang,
        "link": n.link,
        "cost_usd": decimal_str(n.cost_usd),
        "tokens_in": n.tokens_in,
        "tokens_out": n.tokens_out,
        "model": n.model,
        "headline_only": not n.body_summary.strip(),
    }


@router.get("/news")
def list_news(
    symbol: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    source: str | None = None,
    q: str | None = None,
    limit: int = Query(100, ge=1, le=_MAX_LIMIT),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Filtered, paginated organized-news list + totals over the whole filtered set.

    Filters: ``symbol`` (precise mention), ``date_from``/``date_to`` (inclusive
    YYYY-MM-DD), ``source``, ``q`` (keyword over title/summary, LIKE — WPD so the
    keyword filter matches the WHOLE library, not just the loaded page). ``totals``
    carries the count + total cost across every match (not just the page) for the
    cost-assessment header. Empty/absent news DB → empty list.
    """
    with news_store.news_session() as conn:
        rows, totals = news_store.query_news(
            conn, symbol=symbol, date_from=date_from, date_to=date_to,
            source=source, q=q, limit=limit, offset=offset,
        )
    return {
        "items": [_item_wire(n) for n in rows],
        "totals": {
            "count": totals["count"],
            "total_cost_usd": decimal_str(totals["total_cost_usd"]),
        },
        "limit": limit,
        "offset": offset,
    }


@router.get("/news/filters")
def news_filters() -> dict[str, Any]:
    """The distinct stocks + sources present in the news DB (for the filter dropdowns)."""
    with news_store.news_session() as conn:
        return {
            "stocks": news_store.distinct_symbols(conn),
            "sources": news_store.distinct_sources(conn),
        }


# --- manual fetch trigger (P3 batch 3 · 3C) -----------------------------------


class _RunBody(BaseModel):
    scope: str  # "all" (held ∪ watchlist) or a single registered symbol


def _news_run_worker(
    universe: list[tuple[str, str]], *, now: datetime, job_id: str
) -> None:
    """Daemon target: finalize the running news job_runs row after the pipeline completes.

    Mirrors ``scheduler.jobs.run_job_func``: the request handler already inserted the
    running row via ``start_job_run``; this opens its OWN session (the request conn is closed
    by then), runs the pipeline over the resolved universe, and finalizes the row. Fully
    exception-safe — a fire-and-forget worker must never raise out of the thread.
    """
    try:
        with session() as conn:
            row = conn.execute(
                "SELECT id FROM job_runs WHERE job_id = ? AND finished_at IS NULL "
                "ORDER BY id DESC LIMIT 1",
                (job_id,),
            ).fetchone()
            if row is None:
                return
            try:
                result = news_service.run_news_for(conn, universe, now=now)
                detail = (
                    f"manual: organized {result.get('organized', 0)}, "
                    f"headline {result.get('headline_only', 0)}, "
                    f"skipped {result.get('skipped_existing', 0)} "
                    f"over {len(universe)} symbol(s)"
                    + (" (budget stop)" if result.get("stopped_budget") else "")
                )
                status = "ok"
            except Exception as exc:  # noqa: BLE001 — swallow + log via the run row
                detail, status = str(exc), "error"
            finish_job_run(conn, int(row["id"]), status=status, detail=detail, now=now)
    except Exception:  # noqa: BLE001 — background worker must never raise out of the thread
        return


@router.post("/news/run")
def run_news(
    body: _RunBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    """Manually fetch + AI-organize news (async 202). Already-running → 409.

    ``scope`` is ``"all"`` (every registered instrument — held ∪ watchlist) or a single
    registered symbol (market resolved from the registry). NEVER runs the pipeline on the
    request thread — it spawns a background worker (the LLM batch-only invariant).

    Open in guest/demo mode (FU-D4, 2026-07-15): this is a compute+cache action with no
    outbound push, so the demo site can trigger it; the 409 in-flight lock still holds and
    all config writes stay 403.
    """
    universe = news_service.resolve_news_scope(conn, body.scope)
    if universe is None:
        return JSONResponse(
            status_code=400,
            content=error_body(
                "validation_error", "scope 需為 all 或已註冊的代號", field="scope"
            ),
        )
    if latest_run_unfinished(conn, _NEWS_JOB_ID):
        return JSONResponse(
            status_code=409, content=error_body("already_running", "新聞抓取執行中")
        )
    run_id = start_job_run(conn, _NEWS_JOB_ID, now=now)
    thread = threading.Thread(
        target=_news_run_worker,
        kwargs={"universe": universe, "now": now, "job_id": _NEWS_JOB_ID},
        daemon=True,
    )
    thread.start()
    return JSONResponse(status_code=202, content={"run_id": run_id, "scope": body.scope})
