"""Best-effort instrument NAME lookup at registration time.

Names are descriptive metadata (never money): TW resolves offline via the
``twstock`` static code table; US/MY ask yfinance for shortName/longName. Every
path degrades to ``None`` — a failed lookup never blocks registration (the user
can edit the name later). Network/library calls are isolated in module-level
seams (``_tw_name`` / ``_yf_name``) so tests monkeypatch them (socket ban).
"""

import logging

from portfolio_dash.shared.enums import Market

logger = logging.getLogger(__name__)

# yfinance symbol suffix per market (same convention as the yfinance provider).
_YF_SUFFIX = {Market.US: "", Market.TW: ".TW", Market.MY: ".KL"}


def _tw_name(symbol: str) -> str | None:
    """TW name from twstock's bundled code table (offline, instant)."""
    import twstock

    info = twstock.codes.get(symbol)
    name = getattr(info, "name", None) if info is not None else None
    return str(name) if name else None


def _yf_name(yf_symbol: str) -> str | None:
    """US/MY name from yfinance ticker info (network; one-time at registration)."""
    import yfinance

    info = yfinance.Ticker(yf_symbol).get_info()
    if not isinstance(info, dict):
        return None
    name = info.get("shortName") or info.get("longName")
    return str(name) if name else None


def lookup_name(symbol: str, market: Market, *, board: str | None = None) -> str | None:
    """The instrument's display name, or None when no source can supply one.

    Each source degrades INDEPENDENTLY (a broken/missing twstock must still fall
    through to yfinance — that nesting gap left names empty on the live site,
    2026-07-02).
    """
    if market is Market.TW:
        try:
            name = _tw_name(symbol)
            if name:
                return name
        except Exception:  # noqa: BLE001 — static-table miss/import error -> yfinance
            logger.info("twstock name lookup failed for %s", symbol, exc_info=True)
        # TPEx/edge codes missing from the static table: fall through to yfinance.
        suffix = ".TWO" if board == "TPEx" else ".TW"
        yf_symbol = f"{symbol}{suffix}"
    else:
        yf_symbol = f"{symbol}{_YF_SUFFIX[market]}"
    try:
        return _yf_name(yf_symbol)
    except Exception:  # noqa: BLE001 — name lookup is best-effort by contract
        logger.info("yfinance name lookup failed for %s", yf_symbol, exc_info=True)
        return None
