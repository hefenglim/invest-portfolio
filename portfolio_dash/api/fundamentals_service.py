"""Fundamentals AV-leg runner (W3, AI-D16) — the api seam that knows the HELD set.

The Saturday ``fundamentals_av_weekly`` scheduler job covers Alpha Vantage for HELD
symbols only: AV's free quota (25 calls/day) cannot survive a full-universe pass. The
held set is a ``portfolio/``-adjacent replay result that ``scheduler/`` and ``pricing/``
cannot compute for themselves, so the app registers this runner at startup and the job
dispatches into it — the same injection pattern as ``signal_scan`` / ``alert_compute``
(architecture.md: the binder is the layer already above both).

The held check is the registry's 「持有」 — ``data_ingestion/holdings.py::held_among`` over
``holds_position``, the predicate the watchlist and target-weights badges read (DEF-075) — no
second holdings definition.
"""

import sqlite3
from datetime import date, datetime

from portfolio_dash.data_ingestion.holdings import held_among
from portfolio_dash.data_ingestion.store import list_instruments
from portfolio_dash.pricing import ingest
from portfolio_dash.pricing.refs import InstrumentRef


def _held_refs(conn: sqlite3.Connection, *, today: date) -> list[InstrumentRef]:
    """Held symbols as InstrumentRefs (``board`` carries the TPEx flag for ``yf_symbol``
    mapping, mirroring ``ingest.all_universe``).

    DEF-075: "held" is ``held_among`` (a position today or on any later ledger date). It was
    ``current_shares > 0``, so a position closed only by a FUTURE-dated sale — still held
    today — and a declared short got no Alpha Vantage fundamentals.
    """
    instruments = list_instruments(conn)
    held = held_among(conn, [i.symbol for i in instruments], today=today)
    return [
        InstrumentRef(symbol=inst.symbol, market=inst.market, board=inst.board)
        for inst in instruments
        if inst.symbol in held
    ]


def run_fundamentals_av(conn: sqlite3.Connection, *, now: datetime) -> int:
    """Alpha Vantage fundamentals for HELD symbols only. Returns snapshots written."""
    refs = _held_refs(conn, today=now.date())
    if not refs:
        return 0
    return ingest.ingest_fundamentals_union(
        conn, now=now, sources=("alphavantage",), universe=refs
    )
