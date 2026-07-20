"""News pipeline orchestration (pure over injected seams): discover -> fetch -> organize -> store.

The nightly job feeds this. All I/O is INJECTED (discover/fetch/organize callables + the
news-DB connection), so this controller is unit-testable with no network and no LLM. It
enforces the per-symbol cap, cross-symbol de-dup (a link already organized is skipped), the
fetch-fail degrade (store headline-only), and the budget stop (an exhausted/unconfigured
LLM ends the run — partial, produced rows kept).

Two observability upgrades (2026-07-21): (1) every real fetch records its
:class:`~portfolio_dash.news.fetcher.FetchOutcome` status + bumps an attempt counter on the
row, so an empty body is auditable, not silent; (2) a RETRY STAGE re-fetches aged
empty-body rows each run — even after discovery stops surfacing the link — so a transient
miss is no longer permanent. The ``fetch`` seam accepts either the legacy ``str | None`` or
a rich ``FetchOutcome`` (normalized here), keeping older callers/tests unchanged.
"""

import sqlite3
from collections.abc import Callable
from datetime import datetime

from portfolio_dash.news.fetcher import FetchOutcome
from portfolio_dash.news.sources import NewsLink
from portfolio_dash.news.store import (
    OrganizedNews,
    add_mention,
    is_fully_organized,
    list_refetch_candidates,
    record_fetch_attempt,
    upsert_news,
)
from portfolio_dash.shared.llm_config import AINotActivated, LLMBudgetExceeded, LLMUnavailable

# Injected seams:
#   Discover(symbol, market) -> [NewsLink]
#   Fetch(url)               -> article text | None | FetchOutcome  (all degrade-safe)
#   Organize(link, text)     -> OrganizedNews  (raises LLMError)
Discover = Callable[[str, str], list[NewsLink]]
Fetch = Callable[[str], "str | None | FetchOutcome"]
Organize = Callable[[NewsLink, str], OrganizedNews]


def _as_outcome(result: "str | None | FetchOutcome") -> FetchOutcome:
    """Normalize a fetch-seam return into a :class:`FetchOutcome`.

    A rich outcome passes through (real fetcher → classified status); a legacy ``str`` is
    an ``ok`` body; ``None`` is a generic ``empty`` degrade (older seams/tests that yield
    only ``str | None`` keep working unchanged).
    """
    if isinstance(result, FetchOutcome):
        return result
    if result is None:
        return FetchOutcome(None, "empty")
    return FetchOutcome(result, "ok")


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
    refetch_max_age_days: int = 14,
    refetch_max_attempts: int = 3,
    refetch_limit: int = 10,
) -> dict[str, int | bool]:
    """Run the discover→fetch→organize→store loop over ``holdings`` (``(symbol, market)``).

    Returns counts + a ``stopped_budget`` flag. A link already in the news DB is skipped
    (cross-run + cross-symbol de-dup). A fetch miss stores a headline-only row (and records
    the fetch status/attempt). A transient :exc:`LLMUnavailable` degrades THAT article to
    headline-only and continues; a persistent :exc:`LLMBudgetExceeded` / :exc:`AINotActivated`
    ends the run (partial).

    After the discovery loop (and only if the budget did not stop the run), a RETRY STAGE
    re-fetches up to ``refetch_limit`` aged empty-body rows that discovery no longer surfaces
    — bounded by ``refetch_max_age_days`` / ``refetch_max_attempts``; a recovered article
    flows through the normal organize+store path and counts toward ``organized``.
    """
    organized = headline = skipped = refetched = 0
    stopped_budget = False
    processed: set[str] = set()  # links fetched this run — the retry stage skips them
    held = {sym for sym, _ in holdings}  # SR fix: only held tickers enter the mentions index
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
            # SR fix: skip only FULLY-organized links (a headline-only degrade is retried);
            # either way record THIS symbol's mention of an already-stored article.
            if is_fully_organized(conn_news, link.link):
                add_mention(conn_news, link.link, symbol)
                skipped += 1
                continue
            taken += 1
            outcome = _as_outcome(fetch(link.link))
            processed.add(link.link)
            if outcome.text is None:
                upsert_news(conn_news, _headline_only(link, now=now),
                            discovered_for=symbol, index_symbols=held)
                record_fetch_attempt(conn_news, link.link, status=outcome.status)
                headline += 1
                continue
            try:
                item = organize(link, outcome.text)
            except (LLMBudgetExceeded, AINotActivated):
                # persistent — store this one headline-only and end the run.
                upsert_news(conn_news, _headline_only(link, now=now),
                            discovered_for=symbol, index_symbols=held)
                record_fetch_attempt(conn_news, link.link, status=outcome.status)
                headline += 1
                stopped_budget = True
                break
            except LLMUnavailable:
                # transient — degrade this article, keep going.
                upsert_news(conn_news, _headline_only(link, now=now),
                            discovered_for=symbol, index_symbols=held)
                record_fetch_attempt(conn_news, link.link, status=outcome.status)
                headline += 1
                continue
            upsert_news(conn_news, item, discovered_for=symbol, index_symbols=held)
            record_fetch_attempt(conn_news, link.link, status=outcome.status)
            organized += 1
    if not stopped_budget:
        refetched, recovered, stopped_budget = _run_refetch_stage(
            conn_news, held=held, fetch=fetch, organize=organize, now=now,
            processed=processed, max_age_days=refetch_max_age_days,
            max_attempts=refetch_max_attempts, limit=refetch_limit,
        )
        organized += recovered
    return {
        "organized": organized,
        "headline_only": headline,
        "skipped_existing": skipped,
        "refetched": refetched,
        "stopped_budget": stopped_budget,
    }


def _run_refetch_stage(
    conn_news: sqlite3.Connection,
    *,
    held: set[str],
    fetch: Fetch,
    organize: Organize,
    now: datetime,
    processed: set[str],
    max_age_days: int,
    max_attempts: int,
    limit: int,
) -> tuple[int, int, bool]:
    """Re-fetch aged empty-body rows discovery no longer surfaces. Returns
    ``(attempted, recovered, stopped_budget)``.

    Every candidate that is actually fetched bumps its attempt counter (so a persistently
    failing link eventually crosses ``max_attempts`` and drops out of the backlog). A
    recovered body organizes through the normal path; existing mentions are preserved by the
    upsert merge, and new related tickers are still gated to ``held``. Links already fetched
    in this run's discovery loop (``processed``) are skipped so no link is fetched twice.
    """
    candidates = list_refetch_candidates(
        conn_news, max_age_days=max_age_days, max_attempts=max_attempts, limit=limit
    )
    attempted = recovered = 0
    stopped_budget = False
    for cand in candidates:
        if cand.link in processed:
            continue
        link = NewsLink(title=cand.title, link=cand.link, source=cand.source,
                        date=cand.news_date, lang=cand.lang)
        outcome = _as_outcome(fetch(link.link))
        attempted += 1
        if outcome.text is None:
            record_fetch_attempt(conn_news, link.link, status=outcome.status)
            continue
        try:
            item = organize(link, outcome.text)
        except (LLMBudgetExceeded, AINotActivated):
            record_fetch_attempt(conn_news, link.link, status=outcome.status)
            stopped_budget = True
            break
        except LLMUnavailable:
            record_fetch_attempt(conn_news, link.link, status=outcome.status)
            continue
        upsert_news(conn_news, item, index_symbols=held)
        record_fetch_attempt(conn_news, link.link, status=outcome.status)
        recovered += 1
    return attempted, recovered, stopped_budget
