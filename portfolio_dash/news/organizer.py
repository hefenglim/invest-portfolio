"""LLM news organizer: fetched article text -> structured, faithful news (default model).

Uses ``shared.llm.complete_structured`` (default role) with the editable news-organizer
prompt to turn a fetched article body into ``{title, news_date, body_summary,
related_stocks}``. Batch-only (the nightly pipeline calls it), budget-governed (an
exhausted budget raises ``LLMBudgetExceeded`` up to the pipeline). Invariant #1: this
extracts qualitative text + ticker mentions, never numbers of record — the insight cards
still compute every figure locally.
"""

import sqlite3
from datetime import datetime

from pydantic import BaseModel, Field

from portfolio_dash.news.sources import NewsLink
from portfolio_dash.news.store import OrganizedNews
from portfolio_dash.shared import llm

_AGENT = "news_organize"


class NewsExtract(BaseModel):
    """The forced-JSON shape the organizer LLM returns for one article."""

    title: str = ""
    news_date: str = ""  # YYYY-MM-DD (may be blank -> caller fills the fallback)
    body_summary: str = ""
    related_stocks: list[str] = Field(default_factory=list)


def _valid_date(value: str) -> bool:
    try:
        datetime.strptime(value[:10], "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False


def organize(
    link: NewsLink,
    article_text: str,
    prompt: str,
    *,
    conn: sqlite3.Connection,
    now: datetime,
) -> OrganizedNews:
    """Organize one fetched article into an :class:`OrganizedNews` row.

    Merges the model output with the discovery fallbacks (title/date/source/lang from the
    :class:`NewsLink`) so a blank field never produces a hollow row. Raises
    :exc:`~portfolio_dash.shared.llm_config.LLMError` (budget/unavailable/not-activated)
    up to the pipeline, which decides whether to stop or degrade.
    """
    full_prompt = f"{prompt}\n\n<article>\n{article_text}\n</article>"
    completion = llm.complete_structured_meta(full_prompt, NewsExtract, agent=_AGENT, conn=conn)
    ext = completion.value
    title = ext.title.strip() or link.title
    date = ext.news_date.strip() if _valid_date(ext.news_date.strip()) else (link.date or "")
    if not date:
        date = now.date().isoformat()
    stocks = sorted({s.strip() for s in ext.related_stocks if s and s.strip()})
    stamp = now.isoformat()
    return OrganizedNews(
        link=link.link,
        title=title,
        news_date=date[:10],
        body_summary=ext.body_summary.strip(),
        related_stocks=stocks,
        source=link.source,
        lang=link.lang,
        fetched_at=stamp,
        organized_at=stamp,
    )
