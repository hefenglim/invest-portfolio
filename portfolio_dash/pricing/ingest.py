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

from portfolio_dash.pricing import (
    consensus_source,
    datasources_store,
    fundamentals_source,
    index_source,
    sentiment_source,
)
from portfolio_dash.pricing import snapshots_store as S
from portfolio_dash.pricing.defaults import default_registry
from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.finmind_datasets import fetch_dataset
from portfolio_dash.pricing.providers.yfinance_provider import yf_symbol
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.shared.enums import Market

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


def all_universe(conn: sqlite3.Connection) -> list[InstrumentRef]:
    """Every registered instrument as an InstrumentRef, via direct SQL (all markets).

    Consensus is fetched across US/TW/MY (unlike the TW-only FinMind chips), so this
    reads the full ``instruments`` table. ``board`` carries the TPEx flag so ``yf_symbol``
    maps櫃買 counters to ``.TWO``. No ``data_ingestion`` import (layering, spec 20.3).
    """
    rows = conn.execute(
        "SELECT symbol, market, board FROM instruments ORDER BY symbol"
    ).fetchall()
    return [
        InstrumentRef(symbol=r["symbol"], market=Market(r["market"]), board=r["board"] or "")
        for r in rows
    ]


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


FetchConsensus = Callable[..., dict[str, Any] | None]


def ingest_consensus(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    fetch_consensus: FetchConsensus | None = None,
) -> int:
    """Ingest analyst-consensus snapshots per registered instrument (all markets).

    Maps each instrument to its yfinance symbol (``yf_symbol`` — reuses the TPEx
    ``.TWO`` mapping), fetches the consensus payload, and appends a snapshot keyed by the
    PORTFOLIO symbol (not the yf symbol) so the variable layer looks it up by the same
    symbol it renders per-symbol cards for. A per-symbol failure/absence writes no row
    and never stops the rest (per-symbol isolation). Returns rows written.

    The client resolves to the live ``consensus_source.fetch_consensus`` at call time
    when not overridden, so a monkeypatch of the module is honoured (scheduler-job tests).
    """
    fetch = fetch_consensus or consensus_source.fetch_consensus
    as_of = now.date()
    written = 0
    for ref in all_universe(conn):
        try:
            payload = fetch(yf_symbol(ref), as_of=as_of)
        except Exception:  # noqa: BLE001 — one bad symbol must not drop the rest
            continue
        if not payload:
            continue
        S.add_snapshot(
            conn,
            source=consensus_source.SOURCE,
            dataset=consensus_source.DATASET,
            symbol=ref.symbol,
            as_of=as_of,
            payload=payload,
            fetched_at=now,
        )
        written += 1
    return written


FetchFundamentals = Callable[..., dict[str, Any] | None]


def ingest_fundamentals_union(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    sources: tuple[str, ...] | None = None,
    universe: list[InstrumentRef] | None = None,
    fetchers: dict[str, FetchFundamentals] | None = None,
) -> int:
    """Ingest fundamentals snapshots with UNION semantics (W3, AI-D13/D14).

    Unlike every quote/FX/dividend fetch (first-success-wins fallback chain), EVERY
    enabled provider in ``sources`` writes its own ``external_snapshots`` row per symbol —
    the table's ``(source, dataset, symbol, as_of)`` key already keeps them apart, and the
    variable layer assembles one block per source (no merge, never averaged).

    "Enabled" = the registry's ``capable_ids(DataType.FUNDAMENTALS, market)`` — provider
    capability AND the key gate in one check, so a keyless Finnhub/Alpha Vantage writes
    nothing and raises nothing. ``sources`` restricts the union per job (the daily job
    runs yfinance + finnhub; the Saturday job runs alphavantage alone — its free quota
    cannot survive a full-universe pass). ``universe`` restricts the symbols (the AV leg
    covers HELD symbols only; the held set is a ``portfolio/`` replay result, computed by
    the api layer and INJECTED — pricing/ must not derive it, per the injection convention
    in architecture.md). Per-(symbol, source) isolation: one failure/absence writes no
    row and never stops the rest. Returns rows written.
    """
    wanted = sources if sources is not None else fundamentals_source.SOURCES
    fetch_map = fetchers if fetchers is not None else fundamentals_source.FETCHERS
    refs = universe if universe is not None else all_universe(conn)
    as_of = now.date()
    # Resolve the enabled sources ONCE per market (not per symbol). A ledger-only DB
    # (bootstrap_db) has no data_sources table, so the keyed providers' token getters
    # raise OperationalError inside supports() — degrade to the key-less yfinance leg,
    # mirroring the "degrade if the table is absent" convention in architecture.md.
    enabled: dict[Market, list[str]] = {}
    try:
        registry = default_registry(conn)
        for market in {ref.market for ref in refs}:
            enabled[market] = [
                s for s in registry.capable_ids(DataType.FUNDAMENTALS, market)
                if s in wanted
            ]
    except sqlite3.OperationalError:
        enabled = {
            market: (["yfinance"] if "yfinance" in wanted else [])
            for market in {ref.market for ref in refs}
        }
    written = 0
    for ref in refs:
        for source in enabled.get(ref.market, []):
            fetch = fetch_map.get(source)
            if fetch is None:
                continue
            # The token string for keyed sources; the fetch seams also fall back to the
            # env var, mirroring the provider ctors (same row, same convention). The
            # table can be absent on a ledger-only DB — same degrade as above.
            try:
                token = datasources_store.get_api_key(conn, source)
            except sqlite3.OperationalError:
                token = None
            try:
                payload = fetch(ref, as_of=as_of, token=token)
            except Exception:  # noqa: BLE001 — one bad (symbol, source) drops no other
                continue
            if not payload:
                continue
            S.add_snapshot(
                conn,
                source=source,
                dataset=fundamentals_source.DATASET,
                symbol=ref.symbol,
                as_of=as_of,
                payload=payload,
                fetched_at=now,
            )
            written += 1
    return written
