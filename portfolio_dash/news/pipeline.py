"""News pipeline orchestration (pure over injected seams): discover -> fetch -> organize -> store.

The nightly job feeds this. All I/O is INJECTED (discover/fetch/organize callables + the
news-DB connection), so this controller is unit-testable with no network and no LLM. It
enforces the per-symbol cap, cross-symbol de-dup (a link already organized is skipped), the
fetch-fail degrade (store headline-only), and the budget stop (an exhausted/unconfigured
LLM ends the run — partial, produced rows kept).
"""

import sqlite3
from collections.abc import Callable
from datetime import datetime

from portfolio_dash.news.sources import NewsLink
from portfolio_dash.news.store import OrganizedNews, link_exists, upsert_news
from portfolio_dash.shared.llm_config import AINotActivated, LLMBudgetExceeded, LLMUnavailable

# Injected seams:
#   Discover(symbol, market) -> [NewsLink]
#   Fetch(url)               -> article text | None (degrade)
#   Organize(link, text)     -> OrganizedNews  (raises LLMError)
Discover = Callable[[str, str], list[NewsLink]]
Fetch = Callable[[str], str | None]
Organize = Callable[[NewsLink, str], OrganizedNews]


def _headline_only(link: NewsLink, *, now: datetime) -> OrganizedNews:
    """A degrade row when the body could not be fetched: title/source only, empty summary."""
    stamp = now.isoformat()
    return OrganizedNews(
        link=link.link, title=link.title, news_date=(link.date or now.date().isoformat())[:10],
        body_summary="", related_stocks=[], source=link.source, lang=link.lang,
        fetched_at=stamp, organized_at=stamp,
    )


def run_news_pipeline(
    conn_news: sqlite3.Connection,
    holdings: list[tuple[str, str]],
    *,
    discover: Discover,
    fetch: Fetch,
    organize: Organize,
    now: datetime,
    per_symbol_cap: int = 5,
) -> dict[str, int | bool]:
    """Run the discover→fetch→organize→store loop over ``holdings`` (``(symbol, market)``).

    Returns counts + a ``stopped_budget`` flag. A link already in the news DB is skipped
    (cross-run + cross-symbol de-dup). A fetch miss stores a headline-only row. A transient
    :exc:`LLMUnavailable` degrades THAT article to headline-only and continues; a persistent
    :exc:`LLMBudgetExceeded` / :exc:`AINotActivated` ends the run (partial).
    """
    organized = headline = skipped = 0
    stopped_budget = False
    llm_off = False
    for symbol, market in holdings:
        if stopped_budget:
            break
        try:
            links = discover(symbol, market)
        except Exception:  # noqa: BLE001 — one symbol's discovery failing must not stop the run
            links = []
        taken = 0
        for link in links:
            if taken >= per_symbol_cap:
                break
            if link_exists(conn_news, link.link):
                skipped += 1
                continue
            taken += 1
            text = fetch(link.link)
            if text is None or llm_off:
                upsert_news(conn_news, _headline_only(link, now=now), discovered_for=symbol)
                headline += 1
                continue
            try:
                item = organize(link, text)
            except (LLMBudgetExceeded, AINotActivated):
                # persistent — store this one headline-only and end the run.
                upsert_news(conn_news, _headline_only(link, now=now), discovered_for=symbol)
                headline += 1
                stopped_budget = True
                break
            except LLMUnavailable:
                # transient — degrade this article, keep going.
                upsert_news(conn_news, _headline_only(link, now=now), discovered_for=symbol)
                headline += 1
                continue
            upsert_news(conn_news, item, discovered_for=symbol)
            organized += 1
    return {
        "organized": organized,
        "headline_only": headline,
        "skipped_existing": skipped,
        "stopped_budget": stopped_budget,
    }
