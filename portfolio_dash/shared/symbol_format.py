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


#: CJK symbols & punctuation, kana, CJK ideographs (+ Extension A + compatibility) and Hangul
#: syllables — the characters no exchange code in any market above can contain. Full-width
#: Latin / digits (``２３３０``) are deliberately NOT in the class: an IME in full-width mode
#: types a code in them, and that is a code, not a name. Mirrored verbatim as ``CJK_RE`` in
#: ``web/inst-quickadd.js`` (pinned by tests/contract/test_quickadd_cjk_skips_provider_lookup.py).
CJK_CLASS = r"\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff"
_CJK = re.compile(f"[{CJK_CLASS}]")


def contains_cjk(raw: str) -> bool:
    """Return True when *raw* contains a CJK character — so it can never be a code.

    Owner 2026-09-30 (R8 F-01): 台積電 typed into the quick-add code box used to wait 25–30 s
    on a provider quote lookup to learn 「查無報價」. A name is not a ticker; the lookup
    doors answer such input from the registry alone and leave identification to AI 辨識.
    """
    return _CJK.search(raw) is not None
