"""Instrument resolution — EXACT-only for exchange codes, non-binding name hints otherwise.

WHY exact-only for codes (owner-signed R6-A, 2026-07-19): an earlier version fuzzy-matched
an unregistered symbol against REGISTERED instruments with ``difflib.SequenceMatcher`` at a
0.75 threshold.  Any two 4-digit exchange codes differing in a single digit score EXACTLY
``2*3/8 = 0.75`` — so unrelated companies coerced into one another.  The live bug: 2303 聯電
was rewritten to 2330 台積電, and 2883 開發金 to 2882 國泰金, with a 「視為」 confirmation the
user could easily wave through.  Edit distance has NO semantic meaning for exchange codes.

New rules:

* Exact symbol hit → ``EXACT`` with the instrument.
* Code-shaped input (:func:`~shared.symbol_format.looks_like_market_code`) that is NOT
  registered → ``NEEDS_AI`` with NO candidates.  It never coerces to a near neighbour; it
  routes to the register-first flow.
* Name-shaped input (聯電, Apple) → ``NEEDS_AI`` with up to five NON-BINDING suggestions
  drawn from NAME-only similarity (never symbol similarity).  ``instrument`` is ``None`` for
  every non-exact outcome; suggestions are hints the caller may show, never a binding.

The 「視為」 coercion class of message is gone entirely.
"""

import sqlite3
from difflib import SequenceMatcher
from enum import StrEnum

from pydantic import BaseModel

from portfolio_dash.data_ingestion.store import get_instrument, list_instruments
from portfolio_dash.shared.enums import Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.symbol_format import looks_like_market_code

# Name-similarity floor for NON-BINDING suggestions.  Deliberately looser than a binding
# match would ever need: a suggestion never rewrites the symbol, so generous recall is safe
# and useful — e.g. 聯電 vs 聯華電子 scores 2*2/6 ≈ 0.67, which SHOULD be offered.
_SUGGEST_THRESHOLD = 0.6

# How many name suggestions to surface at most (kept small — they are a hint, not a picker).
_MAX_SUGGESTIONS = 5


class ResolutionStatus(StrEnum):
    EXACT = "exact"
    NEEDS_AI = "needs_ai"


class Resolution(BaseModel):
    status: ResolutionStatus
    instrument: Instrument | None = None
    candidates: list[Instrument] = []


def _name_ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def resolve(
    conn: sqlite3.Connection,
    raw: str,
    *,
    market_hint: Market | None = None,
) -> Resolution:
    """Resolve a raw user string to an Instrument — exact-only for exchange codes.

    Args:
        conn:        Active SQLite connection.
        raw:         Raw user-supplied symbol or name string.
        market_hint: Optional market filter applied to the name-suggestion pool.

    Returns:
        A :class:`Resolution`.  ``EXACT`` sets ``instrument``.  ``NEEDS_AI`` always
        leaves ``instrument`` None; ``candidates`` holds name-only suggestions for
        name-shaped input and is EMPTY for code-shaped input (the 0.75 digit-code trap).
    """
    exact = get_instrument(conn, raw.strip())
    if exact is not None:
        return Resolution(status=ResolutionStatus.EXACT, instrument=exact)

    # Code-shaped input is EXACT-only: never suggest a near-miss code (the 0.75 trap).
    if looks_like_market_code(raw):
        return Resolution(status=ResolutionStatus.NEEDS_AI)

    # Name-shaped input: offer NON-BINDING suggestions from NAME similarity only (never
    # symbol similarity — that is exactly the coercion this redesign removes).
    pool = [
        i
        for i in list_instruments(conn)
        if market_hint is None or i.market is market_hint
    ]
    scored = sorted(
        ((_name_ratio(raw, i.name or ""), i) for i in pool),
        key=lambda t: t[0],
        reverse=True,
    )
    candidates = [i for s, i in scored if s >= _SUGGEST_THRESHOLD][:_MAX_SUGGESTIONS]
    return Resolution(status=ResolutionStatus.NEEDS_AI, candidates=candidates)


def suggestion_tail(candidates: list[Instrument]) -> str:
    """Return a zh-TW suffix listing NON-BINDING name suggestions, or ``""`` when none.

    Appended to an unregistered-symbol message so the user can spot a likely intended
    instrument (e.g. 「（相近名稱：2303 聯華電子）」) WITHOUT the resolver ever binding it —
    the row stays a hard register-first block.
    """
    if not candidates:
        return ""
    names = "、".join(f"{c.symbol} {c.name or ''}".strip() for c in candidates)
    return f"（相近名稱：{names}）"
