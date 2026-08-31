"""Quote/FX refresh orchestrator.

Ties the `Registry` fallback-chain fetch to the idempotent `store` upserts and
summarizes the run as a `RefreshSummary` (winning source per key, failed keys,
fetch timestamp). Called by the scheduler or a manual-trigger route — never
synchronously from a page render (`data-and-pricing.md`: refresh is decoupled
from page load; the dashboard reads what is in SQLite).
"""

import sqlite3
from datetime import date, datetime
from decimal import Decimal

from portfolio_dash.pricing.refs import FxPair, InstrumentRef
from portfolio_dash.pricing.registry import Registry
from portfolio_dash.pricing.results import FxRow, PriceRow, RefreshSummary
from portfolio_dash.pricing.store import (
    SplitFactorFn,
    _no_factor,
    storable_close,
    storable_rate,
    upsert_dividend_events,
    upsert_fx,
    upsert_prices,
)
from portfolio_dash.shared.wire import decimal_str


def _refuse_nonpositive_closes(
    rows: list[PriceRow],
) -> tuple[list[PriceRow], set[str], list[str]]:
    """Split a provider batch into ``(rows the seam accepts, symbols that got nothing,
    zh refusal lines)``.

    ``upsert_prices`` refuses a non-positive close outright (QA-09), and a refresh upserts the
    WHOLE batch in one call — so without this the first unusable row would cost every other
    symbol in the sweep its update. That is the opposite of the contract this module states
    two docstrings down: a failed fetch is recorded in the summary, never raised
    (``data-and-pricing.md`` — never crash, never fabricate). The refused symbol simply keeps
    its last-known price, which the read already labels stale.

    The predicate is :func:`pricing.store.storable_close`, imported rather than repeated: a
    second copy of ``close > 0`` here could drift out of step with the seam, and the symptom
    would be a batch this function believed it had cleaned and the seam then rejected in full.

    One line per symbol, naming the FIRST value refused for it — ``RefreshSummary.failed`` is
    joined into a one-line ``job_runs.detail`` (``scheduler/jobs._summarize``), so it must
    stay short, and it is read by the owner, so it is zh. A symbol whose OTHER rows landed
    (a history backfill with one bad day) is reported but NOT removed from ``ok``: it did
    update, partially, and calling that a total failure would be its own wrong number.
    """
    clean: list[PriceRow] = []
    refused: dict[str, Decimal] = {}
    for r in rows:
        if storable_close(r.close):
            clean.append(r)
        elif r.instrument not in refused:
            refused[r.instrument] = r.close
    landed = {r.instrument for r in clean}
    return (
        clean,
        set(refused) - landed,
        [f"{symbol}：收盤價非正數（{decimal_str(value)}），已拒絕寫入"
         for symbol, value in refused.items()],
    )


def _fx_key(row: FxRow) -> str:
    """The pair key ``Registry`` uses in ``RefreshSummary.ok`` / ``failed`` (``"USDTWD"``).

    Materialised once so the refusal set is built with the SAME key the summary is keyed on —
    a refused pair filtered out of ``ok`` under a differently-spelled key would silently do
    nothing, and the summary would claim a winning source beside its own refusal.
    """
    return f"{row.base.value}{row.quote.value}"


def _refuse_nonpositive_rates(
    rows: list[FxRow],
) -> tuple[list[FxRow], set[str], list[str]]:
    """The FX twin of :func:`_refuse_nonpositive_closes` — ``(rows the seam accepts, pairs that
    got nothing, zh refusal lines)``.

    ``upsert_fx`` refuses a non-positive rate over the WHOLE batch (R3/QA-13), and both FX
    callers hand it the whole provider batch — so without this one bad row aborts the sweep.
    In ``refresh_quotes`` that abort is worse than a lost update: the PRICES have already been
    upserted by then, so the run half-lands and the ``RefreshSummary`` is never returned, which
    leaves the scheduler with no record of which pair caused it. ``data-and-pricing.md`` asks
    for the opposite ("a failed/stale fetch degrades gracefully ... never crash"), and the
    refused pair simply keeps its last-known rate, which the read already labels stale.

    The predicate is :func:`pricing.store.storable_rate`, imported rather than repeated, for
    the reason its docstring gives: a second copy of ``rate > 0`` could drift out of step with
    the seam, and the symptom would be a batch this function believed it had cleaned.

    One line per pair, naming the FIRST rate refused for it. The line is keyed by the human
    ``USD/TWD`` spelling — the app's established owner-facing rendering for a pair
    (``forex/fx_pnl.py``, ``portfolio/dashboard.py``, ``upsert_fx``'s own message) — while the
    returned SET is keyed the way ``RefreshSummary.ok`` is (``USDTWD``), because that set's only
    job is to filter it. A pair whose OTHER rows landed (a history backfill with one bad day)
    is reported but NOT removed from ``ok``: it did update, partially, and calling that a total
    failure would be its own wrong number.
    """
    clean: list[FxRow] = []
    refused: dict[str, FxRow] = {}
    for r in rows:
        if storable_rate(r.rate):
            clean.append(r)
        elif _fx_key(r) not in refused:
            refused[_fx_key(r)] = r
    landed = {_fx_key(r) for r in clean}
    return (
        clean,
        set(refused) - landed,
        [f"{r.base.value}/{r.quote.value}：匯率非正數（{decimal_str(r.rate)}），已拒絕寫入"
         for r in refused.values()],
    )


def refresh_quotes(
    conn: sqlite3.Connection,
    registry: Registry,
    instruments: list[InstrumentRef],
    fx_pairs: list[FxPair],
    *,
    now: datetime,
    factor_of: SplitFactorFn = _no_factor,
) -> RefreshSummary:
    """Fetch latest quotes + FX via ``registry``, upsert into SQLite, summarize.

    Fetch failures degrade gracefully: failed keys are recorded in the summary
    rather than raised, so a partial-provider outage never crashes the refresh
    (`data-and-pricing.md` — never crash the dashboard, never fabricate). A row the write
    seam would REFUSE — a non-positive close or rate — degrades the same way rather than
    aborting the sweep: see :func:`_refuse_nonpositive_closes` / :func:`_refuse_nonpositive_rates`.

    ``factor_of`` is a **pass-through** to the write seam (D17): this module still
    imports nothing but ``pricing.*``; the corporate-action lookup is bound by the
    ``scheduler`` / ``api`` caller. Omitted, it is the identity and nothing changes.
    """
    p_rows, p_sources, p_failed = registry.fetch_quote_latest(instruments)
    f_rows, f_sources, f_failed = registry.fetch_fx(fx_pairs)
    p_rows, p_unusable, p_refusals = _refuse_nonpositive_closes(p_rows)
    f_rows, f_unusable, f_refusals = _refuse_nonpositive_rates(f_rows)
    if p_rows:
        upsert_prices(conn, p_rows, fetched_at=now, factor_of=factor_of)
    if f_rows:
        upsert_fx(conn, f_rows, fetched_at=now)
    return RefreshSummary(
        # A symbol / pair whose only row was refused stored nothing, so reporting a winning
        # source for it would contradict the refusal listed beside it.
        ok={k: v for k, v in {**p_sources, **f_sources}.items()
            if k not in p_unusable and k not in f_unusable},
        failed=[*p_failed, *p_refusals, *f_failed, *f_refusals],
        fetched_at=now,
    )


def refresh_history(
    conn: sqlite3.Connection,
    registry: Registry,
    instruments: list[InstrumentRef],
    start: date,
    *,
    now: datetime,
    factor_of: SplitFactorFn = _no_factor,
) -> RefreshSummary:
    """Fetch historical daily quotes via ``registry`` from ``start``, upsert, summarize.

    Phase B (historical backfill): mirrors `refresh_quotes`'s shape but for the
    `QUOTE_HISTORY` data type — a per-instrument routed fetch over a date range
    rather than a single latest-quote snapshot. Same graceful-degradation contract:
    failed symbols are recorded in the summary, never raised.

    This is the path §5.1 identifies as the artifact's source — backfilling history
    AFTER a split returns closes the provider has already re-stated — so ``factor_of``
    matters most here. Pass-through only (D17); see :func:`refresh_quotes`.
    """
    rows, sources, failed = registry.fetch_quote_history(instruments, start)
    rows, unusable, refusals = _refuse_nonpositive_closes(rows)
    if rows:
        upsert_prices(conn, rows, fetched_at=now, factor_of=factor_of)
    return RefreshSummary(
        ok={k: v for k, v in sources.items() if k not in unusable},
        failed=[*failed, *refusals],
        fetched_at=now,
    )


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
    graceful-degradation contract as the quote history refresh — including an unusable RATE
    (:func:`_refuse_nonpositive_rates`), which used to lose the whole backfill over one day.
    """
    rows, sources, failed = registry.fetch_fx_history(pairs, start)
    rows, unusable, refusals = _refuse_nonpositive_rates(rows)
    if rows:
        upsert_fx(conn, rows, fetched_at=now)
    return RefreshSummary(
        ok={k: v for k, v in sources.items() if k not in unusable},
        failed=[*failed, *refusals],
        fetched_at=now,
    )


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
