"""Per-market instrument-code SHAPE patterns — the single source of truth.

Pure module: stdlib :mod:`re` + :class:`shared.enums.Market` only (``shared/`` imports
nothing internal beyond ``shared``).  It defines "what a local exchange code looks like"
exactly ONCE so the several places that need that judgement cannot drift apart:

* :mod:`portfolio_dash.data_ingestion.agents` — the post-parse soft symbol-format
  WARNING (FU-D41) that flags e.g. a US ticker booked on a TW account.
* :mod:`portfolio_dash.data_ingestion.resolve` — the exact-vs-code gating that decides
  whether an unregistered input routes straight to the register-first flow (code shape)
  or earns non-binding NAME suggestions (name shape).
* the (next-wave) AI instrument-resolve gate, which will consume the same patterns.

These are SHAPE checks only.  A syntactically valid code is NOT a registered instrument;
the provider lookup at registration remains the authority.  The formats below are
owner-signed (R6-A, 2026-07-19): TW ``2330`` / ``00878B``, US ``AAPL`` / ``BRK.B``,
MY ``5225``.
"""

import re
from collections.abc import Mapping

from portfolio_dash.shared.enums import Market

MARKET_CODE_PATTERNS: Mapping[Market, re.Pattern[str]] = {
    Market.TW: re.compile(r"^\d{4,6}[A-Z]{0,2}$"),
    Market.US: re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$"),
    Market.MY: re.compile(r"^\d{4}$"),
}


def _normalize(raw: str) -> str:
    """Canonical form every pattern check runs against: trim + uppercase."""
    return raw.strip().upper()


def matches_market_format(symbol: str, market: Market) -> bool:
    """Return True when *symbol* has the code SHAPE of *market* (after strip+upper)."""
    return MARKET_CODE_PATTERNS[market].match(_normalize(symbol)) is not None


def looks_like_market_code(raw: str) -> bool:
    """Return True when *raw* matches ANY market's code shape (after strip+upper).

    Distinguishes code-shaped input — which resolves EXACT-only, because one-digit
    edit distance between exchange codes has no semantic meaning (2303 vs 2330 score
    exactly 0.75) — from name-shaped input, which may earn non-binding name suggestions.
    """
    norm = _normalize(raw)
    return any(pattern.match(norm) is not None for pattern in MARKET_CODE_PATTERNS.values())
