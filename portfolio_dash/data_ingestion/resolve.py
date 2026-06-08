"""Fuzzy symbol resolution: exact match → fuzzy name/symbol match → NEEDS_AI."""

import sqlite3
from difflib import SequenceMatcher
from enum import StrEnum

from pydantic import BaseModel

from portfolio_dash.data_ingestion.store import get_instrument, list_instruments
from portfolio_dash.shared.enums import Market
from portfolio_dash.shared.models.assets import Instrument


class ResolutionStatus(StrEnum):
    EXACT = "exact"
    FUZZY = "fuzzy"
    NEEDS_AI = "needs_ai"


class Resolution(BaseModel):
    status: ResolutionStatus
    instrument: Instrument | None = None
    candidates: list[Instrument] = []


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def resolve(
    conn: sqlite3.Connection,
    raw: str,
    *,
    market_hint: Market | None = None,
    threshold: float = 0.6,
) -> Resolution:
    """Resolve a raw user string to an Instrument.

    Returns:
        Resolution with status EXACT (symbol hit), FUZZY (best name/symbol match
        above threshold), or NEEDS_AI (no confident match found).
    """
    exact = get_instrument(conn, raw.strip())
    if exact is not None:
        return Resolution(status=ResolutionStatus.EXACT, instrument=exact)

    pool = [
        i
        for i in list_instruments(conn)
        if market_hint is None or i.market is market_hint
    ]
    scored = sorted(
        (
            (max(_ratio(raw, i.symbol), _ratio(raw, i.name or "")), i)
            for i in pool
        ),
        key=lambda t: t[0],
        reverse=True,
    )
    if scored and scored[0][0] >= threshold:
        cands = [i for s, i in scored if s >= threshold][:5]
        return Resolution(
            status=ResolutionStatus.FUZZY,
            instrument=scored[0][1],
            candidates=cands,
        )
    return Resolution(status=ResolutionStatus.NEEDS_AI)
