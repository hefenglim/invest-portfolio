"""Quote/FX refresh orchestrator.

Ties the `Registry` fallback-chain fetch to the idempotent `store` upserts and
summarizes the run as a `RefreshSummary` (winning source per key, failed keys,
fetch timestamp). Called by the scheduler or a manual-trigger route — never
synchronously from a page render (`data-and-pricing.md`: refresh is decoupled
from page load; the dashboard reads what is in SQLite).
"""

import sqlite3
from datetime import date, datetime

from portfolio_dash.pricing.refs import FxPair, InstrumentRef
from portfolio_dash.pricing.registry import Registry
from portfolio_dash.pricing.results import RefreshSummary
from portfolio_dash.pricing.store import upsert_dividend_events, upsert_fx, upsert_prices


def refresh_quotes(
    conn: sqlite3.Connection,
    registry: Registry,
    instruments: list[InstrumentRef],
    fx_pairs: list[FxPair],
    *,
    now: datetime,
) -> RefreshSummary:
    """Fetch latest quotes + FX via ``registry``, upsert into SQLite, summarize.

    Fetch failures degrade gracefully: failed keys are recorded in the summary
    rather than raised, so a partial-provider outage never crashes the refresh
    (`data-and-pricing.md` — never crash the dashboard, never fabricate).
    """
    p_rows, p_sources, p_failed = registry.fetch_quote_latest(instruments)
    f_rows, f_sources, f_failed = registry.fetch_fx(fx_pairs)
    if p_rows:
        upsert_prices(conn, p_rows, fetched_at=now)
    if f_rows:
        upsert_fx(conn, f_rows, fetched_at=now)
    return RefreshSummary(
        ok={**p_sources, **f_sources},
        failed=[*p_failed, *f_failed],
        fetched_at=now,
    )


def refresh_history(
    conn: sqlite3.Connection,
    registry: Registry,
    instruments: list[InstrumentRef],
    start: date,
    *,
    now: datetime,
) -> RefreshSummary:
    """Fetch historical daily quotes via ``registry`` from ``start``, upsert, summarize.

    Phase B (historical backfill): mirrors `refresh_quotes`'s shape but for the
    `QUOTE_HISTORY` data type — a per-instrument routed fetch over a date range
    rather than a single latest-quote snapshot. Same graceful-degradation contract:
    failed symbols are recorded in the summary, never raised.
    """
    rows, sources, failed = registry.fetch_quote_history(instruments, start)
    if rows:
        upsert_prices(conn, rows, fetched_at=now)
    return RefreshSummary(ok=sources, failed=failed, fetched_at=now)


def refresh_fx_history(
    conn: sqlite3.Connection,
    registry: Registry,
    pairs: list[FxPair],
    start: date,
    *,
    now: datetime,
) -> RefreshSummary:
    """Fetch historical daily FX rates via ``registry`` from ``start``, upsert, summarize.

    Backfills the reporting-currency pairs so the trend replay and XIRR have a
    rate on-or-before EVERY ledger flow date (2026-07-03, R4 item 2). Same
    graceful-degradation contract as the quote history refresh.
    """
    rows, sources, failed = registry.fetch_fx_history(pairs, start)
    if rows:
        upsert_fx(conn, rows, fetched_at=now)
    return RefreshSummary(ok=sources, failed=failed, fetched_at=now)


def refresh_dividends(
    conn: sqlite3.Connection,
    registry: Registry,
    instruments: list[InstrumentRef],
    *,
    now: datetime,
) -> RefreshSummary:
    """Fetch dividend events via ``registry``, upsert into SQLite, summarize.

    Mirrors `refresh_history`'s shape but for the `DIVIDEND` data type — a
    per-instrument routed fetch of corporate-action events. Same graceful-
    degradation contract: failed symbols are recorded in the summary, never
    raised (`data-and-pricing.md` — never crash, never fabricate).
    """
    events, sources, failed = registry.fetch_dividends(instruments)
    if events:
        upsert_dividend_events(conn, events, fetched_at=now)
    return RefreshSummary(ok=sources, failed=failed, fetched_at=now)
