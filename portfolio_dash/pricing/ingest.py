"""External-snapshot ingest functions (spec 20.4).

Each function calls a single-source client and appends raw responses to
``external_snapshots`` (append-only; spec 20.4). They take injectable client
callables (defaulting to the real clients) so tests monkeypatch without network.

Layering: the TW symbol universe is read here by **direct SQL** on ``instruments``
(``SELECT symbol FROM instruments WHERE market='TW'``) — ingest must not depend on
``data_ingestion``. Nothing is converted to money here; the raw payload is stored
verbatim (Decimal discipline lives in ``portfolio/external_signals.py``). FinMind
``Decimal`` values that arrive from the sentiment/index clients are serialized as
canonical strings (``str(Decimal)``) so no float ever reaches storage.
"""

import sqlite3
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from portfolio_dash.pricing import index_source, sentiment_source
from portfolio_dash.pricing import snapshots_store as S
from portfolio_dash.pricing.finmind_datasets import fetch_dataset

# Default lookback window for FinMind date-range batch fetches.
_FINMIND_START = "2025-01-01"

FetchDataset = Callable[..., list[dict[str, Any]]]


def _resolve_fetch_dataset(override: "FetchDataset | None") -> FetchDataset:
    """The dataset client to use: an explicit override, else the module-level default.

    Reading the module-level ``fetch_dataset`` here (where it is not shadowed by a
    parameter) lets the scheduler-job tests monkeypatch ``ingest.fetch_dataset`` while
    the direct ingest tests still pass an explicit ``fetch_dataset=`` callable.
    """
    return override if override is not None else fetch_dataset


def tw_universe(conn: sqlite3.Connection) -> list[str]:
    """TW symbols (holdings + watchlist) via direct SQL — no data_ingestion import."""
    rows = conn.execute(
        "SELECT symbol FROM instruments WHERE market = 'TW' ORDER BY symbol"
    ).fetchall()
    return [r["symbol"] for r in rows]


def _latest_as_of(rows: list[dict[str, Any]], *, default: date) -> date:
    """The newest ISO ``date`` field across rows, or ``default`` when absent."""
    dates: list[date] = []
    for row in rows:
        raw = row.get("date")
        if isinstance(raw, str) and raw:
            try:
                dates.append(date.fromisoformat(raw[:10]))
            except ValueError:
                continue
    return max(dates) if dates else default


def _ingest_finmind_dataset(
    conn: sqlite3.Connection,
    *,
    dataset: str,
    symbols: list[str],
    now: datetime,
    fetch_dataset: FetchDataset,
) -> int:
    """Fetch one FinMind dataset per symbol and append non-empty snapshots. Returns count."""
    written = 0
    for symbol in symbols:
        rows = fetch_dataset(
            conn, dataset=dataset, data_id=symbol, start_date=_FINMIND_START
        )
        if not rows:
            continue
        S.add_snapshot(
            conn,
            source="finmind",
            dataset=dataset,
            symbol=symbol,
            as_of=_latest_as_of(rows, default=now.date()),
            payload={"rows": rows},
            fetched_at=now,
        )
        written += 1
    return written


def ingest_chips(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    fetch_dataset: FetchDataset | None = None,
) -> int:
    """Ingest institutional + margin chips for the TW universe. Returns rows written."""
    fetch = _resolve_fetch_dataset(fetch_dataset)
    symbols = tw_universe(conn)
    written = 0
    for dataset in ("institutional", "margin"):
        written += _ingest_finmind_dataset(
            conn, dataset=dataset, symbols=symbols, now=now, fetch_dataset=fetch
        )
    return written


def ingest_valuation(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    fetch_dataset: FetchDataset | None = None,
) -> int:
    """Ingest PER/PBR/yield valuation for the TW universe. Returns rows written."""
    fetch = _resolve_fetch_dataset(fetch_dataset)
    return _ingest_finmind_dataset(
        conn, dataset="valuation", symbols=tw_universe(conn), now=now, fetch_dataset=fetch
    )


def ingest_fundamentals(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    fetch_dataset: FetchDataset | None = None,
) -> int:
    """Ingest monthly revenue + financial statements for the TW universe."""
    fetch = _resolve_fetch_dataset(fetch_dataset)
    symbols = tw_universe(conn)
    written = 0
    for dataset in ("monthly_revenue", "financials"):
        written += _ingest_finmind_dataset(
            conn, dataset=dataset, symbols=symbols, now=now, fetch_dataset=fetch
        )
    return written


def ingest_sentiment(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    fetch_vix: Callable[[], Decimal | None] | None = None,
    fetch_fear_greed: Callable[[], dict[str, Any] | None] | None = None,
) -> int:
    """Ingest VIX + CNN Fear & Greed (symbol-less). Each missing source degrades.

    Client callables resolve to the live ``sentiment_source`` functions at call time
    when not overridden, so a monkeypatch of the module is honoured.
    """
    vix_fn = fetch_vix or sentiment_source.fetch_vix
    fng_fn = fetch_fear_greed or sentiment_source.fetch_fear_greed
    written = 0
    vix = vix_fn()
    if vix is not None:
        S.add_snapshot(
            conn, source="sentiment", dataset="vix", symbol=None, as_of=now.date(),
            payload={"close": str(vix)}, fetched_at=now,
        )
        written += 1
    fng = fng_fn()
    if fng is not None:
        S.add_snapshot(
            conn, source="sentiment", dataset="fng", symbol=None, as_of=now.date(),
            payload={"score": str(fng["score"]), "rating": fng["rating"]}, fetched_at=now,
        )
        written += 1
    return written


def ingest_index(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    fetch_indices: Callable[[], dict[str, Decimal]] | None = None,
) -> int:
    """Ingest the three benchmark index closes as one symbol-less snapshot. Degrades empty."""
    fetch = fetch_indices or index_source.fetch_indices
    quotes = fetch()
    if not quotes:
        return 0
    S.add_snapshot(
        conn, source="index", dataset="index_quotes", symbol=None, as_of=now.date(),
        payload={"quotes": {sym: str(val) for sym, val in quotes.items()}}, fetched_at=now,
    )
    return 1
