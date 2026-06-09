"""Fuzzy symbol resolution: exact match → fuzzy name/symbol match → NEEDS_AI."""

import sqlite3
from collections.abc import Callable
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
    llm_resolver: Callable[[str], Instrument | None] | None = None,
) -> Resolution:
    """Resolve a raw user string to an Instrument.

    First attempts an exact symbol match, then a fuzzy name/symbol match.  If
    both fail and *llm_resolver* is provided, calls it as a last-resort fallback.

    Args:
        conn:          Active SQLite connection.
        raw:           Raw user-supplied symbol or name string.
        market_hint:   Optional market filter applied to the fuzzy-match pool.
        threshold:     Minimum fuzzy-match ratio to accept a candidate (0–1).
        llm_resolver:  Optional callable ``(raw_symbol) -> Instrument | None``
                       invoked only when exact and fuzzy both fail.  When it
                       returns an instrument, the resolution status is FUZZY
                       (confident enough to proceed with confirmation).

    Returns:
        Resolution with status EXACT (symbol hit), FUZZY (best name/symbol match
        above threshold or LLM-resolved), or NEEDS_AI (no confident match found).
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

    # --- LLM fallback (optional, injected by caller) ---
    if llm_resolver is not None:
        proposed = llm_resolver(raw)
        if proposed is not None:
            return Resolution(
                status=ResolutionStatus.FUZZY,
                instrument=proposed,
                candidates=[proposed],
            )

    return Resolution(status=ResolutionStatus.NEEDS_AI)
