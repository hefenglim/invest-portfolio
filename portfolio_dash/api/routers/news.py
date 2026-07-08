"""News browse API (batch ④): read the separate news DB for the news-library page.

Read-only over ``news.db``: a filtered/paginated list of organized news with per-item
token + cost, aggregate totals for cost assessment, and the distinct stocks/sources that
populate the filter dropdowns. Money/cost is a Decimal STRING on the wire (the frontend
never computes). No LLM here — the organizing happens in the nightly ``news_daily`` job.
"""

from typing import Any

from fastapi import APIRouter, Query

from portfolio_dash.news import store as news_store
from portfolio_dash.shared.wire import decimal_str

router = APIRouter()

_MAX_LIMIT = 200


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
