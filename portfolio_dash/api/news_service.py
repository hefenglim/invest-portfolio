"""News pipeline wiring (the conn-bearing seam): real clients -> pure pipeline -> news DB.

Registered as the ``news_daily`` runner at app startup (``register_news_runner``), so the
scheduler never imports this. Builds the FinMind (中文, token-optional) / yfinance (英文) /
Yahoo-TW link clients + the HTML fetcher + the LLM organizer, resolves the held universe
from the dashboard, and runs the pure :func:`news.pipeline.run_news_pipeline` against the
separate news DB. Everything network/LLM lives behind the injected seams the pipeline
takes, so the pipeline itself stays unit-tested and pure.
"""

import logging
import sqlite3
from collections.abc import Callable
from datetime import date, datetime, timedelta

from portfolio_dash.data_ingestion.store import list_instruments
from portfolio_dash.news import fetcher, organizer, pipeline
from portfolio_dash.news import sources as news_sources
from portfolio_dash.news import store as news_store
from portfolio_dash.news.organizer_prompt import get_news_prompt
from portfolio_dash.news.store import OrganizedNews
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.pricing import finmind_datasets
from portfolio_dash.shared.enums import Currency

logger = logging.getLogger(__name__)

_FINMIND_WINDOW_DAYS = 5   # FinMind is single-day-per-request; walk the last N days
_NEWS_LOOKBACK_DAYS = 7    # discovery lower bound for yfinance/Yahoo date filtering
_PER_SYMBOL_CAP = 5


def _finmind_client(conn: sqlite3.Connection, now: datetime) -> news_sources.FinMindClient:
    """A FinMind news client that walks the last ``_FINMIND_WINDOW_DAYS`` days (single-day API)."""
    def client(data_id: str, start_date: str) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        try:
            begin = max(date.fromisoformat(start_date),
                        now.date() - timedelta(days=_FINMIND_WINDOW_DAYS))
        except ValueError:
            begin = now.date() - timedelta(days=_FINMIND_WINDOW_DAYS)
        d = begin
        while d <= now.date():
            rows.extend(finmind_datasets.fetch_taiwan_stock_news(
                conn, data_id=data_id, start_date=d.isoformat()))
            d += timedelta(days=1)
        return rows
    return client


def _yf_client() -> news_sources.YfClient:
    """A yfinance news client (lazy import; returns [] on any failure)."""
    def client(ticker: str) -> list[dict[str, object]]:
        try:
            import yfinance as yf
            news = yf.Ticker(ticker).news
            return list(news) if news else []
        except Exception:  # noqa: BLE001 — one ticker's yfinance hiccup must not sink the job
            return []
    return client


def run_news_for(
    conn: sqlite3.Connection,
    symbols_with_market: list[tuple[str, str]],
    *,
    now: datetime,
    progress: Callable[[str], None] | None = None,
) -> dict[str, int | bool]:
    """Run the news pipeline over an EXPLICIT ``(symbol, market)`` universe. Run summary.

    The shared core of BOTH the nightly held-universe job (:func:`run_news_daily`) and the
    manual scoped run (``POST /api/news/run`` — held ∪ watchlist for ``all``, or a single
    symbol). Wires the real FinMind / yfinance / Yahoo-TW clients + the HTML fetcher + the
    LLM organizer, and runs the pure pipeline against the separate news DB. The organizer
    records ``llm_usage`` on *conn* (the ledger DB) and is budget-governed; an exhausted
    budget ends the run (partial). Everything network/LLM lives behind the injected seams,
    so the pipeline itself stays unit-tested and pure.

    ``progress`` (FU-D46, additive+optional): a short zh status callback invoked from the
    injected seams as the pipeline walks symbols (discovery is called once per symbol, in
    order — the (i/n) counter is honest) and organizes articles. It carries display text
    only, never numbers of record; ``None`` (the default) preserves the old behavior.
    """
    holdings = sorted(set(symbols_with_market))
    prompt = get_news_prompt(conn)["body"]
    finmind = _finmind_client(conn, now)
    yfc = _yf_client()
    start = (now.date() - timedelta(days=_NEWS_LOOKBACK_DAYS)).isoformat()
    seen = {"n": 0}  # discovery call counter — the pipeline walks holdings in order

    def discover(symbol: str, market: str) -> list[news_sources.NewsLink]:
        if progress is not None:
            seen["n"] += 1
            progress(f"蒐集 {symbol} 新聞（{seen['n']}/{len(holdings)}）")
        return news_sources.discover_links(
            symbol, market, finmind_client=finmind, yf_client=yfc,
            yahoo_fetcher=fetcher.fetch_html, finmind_start=start,
        )

    def do_fetch(url: str) -> fetcher.FetchOutcome:
        # Return the RICH outcome (classified status + detail) so the pipeline records the
        # fetch status on the row and the WARNING log (emitted inside fetch_article) fires
        # for every non-ok fetch — an empty body is now auditable, not silent.
        return fetcher.fetch_article(url)

    def do_organize(link: news_sources.NewsLink, text: str) -> OrganizedNews:
        if progress is not None:
            progress(f"AI 整理：{link.title[:24]}")
        return organizer.organize(link, text, prompt, conn=conn, now=now)

    with news_store.news_session() as nconn:
        result = pipeline.run_news_pipeline(
            nconn, holdings, discover=discover, fetch=do_fetch, organize=do_organize,
            now=now, per_symbol_cap=_PER_SYMBOL_CAP,
        )
    logger.info("news run complete over %d symbol(s): %s", len(holdings), result)
    return result


def run_news_daily(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    reporting: Currency = Currency.TWD,
    progress: Callable[[str], None] | None = None,
) -> dict[str, int | bool]:
    """Run the nightly news pipeline for every HELD symbol. Returns the run summary.

    The nightly job stays held-only (holdings from the dashboard) → :func:`run_news_for`.
    The manual endpoint is the one that widens the universe to the watchlist. ``progress``
    (FU-D46) is the optional per-step status callback the scheduler seam forwards.
    """
    data = build_dashboard(conn, now=now, reporting=reporting)
    holdings = sorted({(h.symbol, h.market.value) for h in data.holdings})
    if progress is None:
        # Keep the delegation TRULY additive: without a callback the call is
        # byte-identical to the pre-FU-D46 form, so existing stubs/monkeypatches of
        # run_news_for (no `progress` param) keep working unchanged.
        return run_news_for(conn, holdings, now=now)
    return run_news_for(conn, holdings, now=now, progress=progress)


def resolve_news_scope(
    conn: sqlite3.Connection, scope: str
) -> list[tuple[str, str]] | None:
    """Resolve a manual-run ``scope`` to a ``(symbol, market)`` universe (registry read).

    ``"all"`` → EVERY registered, NON-archived instrument (held ∪ active watchlist — the
    manual run widens past the nightly held-only job; archived/stopped-tracking symbols
    (FU-D13) drop out of the broad scope). A bare symbol → ``[(symbol, market)]`` with the
    market taken from the registry — an EXPLICIT single-symbol scope is still honoured even
    when archived (the user asked for that name by name). An unknown symbol (or an otherwise
    invalid scope) → ``None`` so the caller returns a 400. Deterministic ordering.
    """
    instruments = list_instruments(conn)
    if scope == "all":
        return sorted({(i.symbol, i.market.value) for i in instruments if not i.archived})
    for i in instruments:
        if i.symbol == scope:
            return [(i.symbol, i.market.value)]
    return None
