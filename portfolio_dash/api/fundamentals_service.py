"""Fundamentals AV-leg runner (W3, AI-D16) — the api seam that knows the HELD set.

The Saturday ``fundamentals_av_weekly`` scheduler job covers Alpha Vantage for HELD
symbols only: AV's free quota (25 calls/day) cannot survive a full-universe pass. The
held set is a ``portfolio/``-adjacent replay result that ``scheduler/`` and ``pricing/``
cannot compute for themselves, so the app registers this runner at startup and the job
dispatches into it — the same injection pattern as ``signal_scan`` / ``alert_compute``
(architecture.md: the binder is the layer already above both).

The held check itself reuses the SAME cheap net-shares predicate as the target-weights
view (``current_shares`` per account, any-account positive = held) — no second holdings
definition.
"""

import sqlite3
from datetime import datetime
from decimal import Decimal

from portfolio_dash.data_ingestion.holdings import current_shares
from portfolio_dash.data_ingestion.store import list_accounts, list_instruments
from portfolio_dash.pricing import ingest
from portfolio_dash.pricing.refs import InstrumentRef

_ZERO = Decimal("0")


def _held_refs(conn: sqlite3.Connection) -> list[InstrumentRef]:
    """Held symbols as InstrumentRefs (``board`` carries the TPEx flag for ``yf_symbol``
    mapping, mirroring ``ingest.all_universe``)."""
    account_ids = [a.account_id for a in list_accounts(conn)]
    return [
        InstrumentRef(symbol=inst.symbol, market=inst.market, board=inst.board)
        for inst in list_instruments(conn)
        if any(current_shares(conn, aid, inst.symbol) > _ZERO for aid in account_ids)
    ]


def run_fundamentals_av(conn: sqlite3.Connection, *, now: datetime) -> int:
    """Alpha Vantage fundamentals for HELD symbols only. Returns snapshots written."""
    refs = _held_refs(conn)
    if not refs:
        return 0
    return ingest.ingest_fundamentals_union(
        conn, now=now, sources=("alphavantage",), universe=refs
    )
