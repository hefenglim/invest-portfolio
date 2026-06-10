"""Probe a TW instrument's board (TWSE vs TPEx) by trying each source's quote endpoint.

Used at instrument registration to resolve ``instruments.board`` once. Reuses the
TWSE/TPEx providers; both ignore the ``InstrumentRef.board`` field (each *is* a board),
so a probe ref with an empty board is fine. Injectable for tests (no live network).
"""

from typing import Protocol

from portfolio_dash.pricing.providers.tpex_provider import TpexProvider
from portfolio_dash.pricing.providers.twse_provider import TwseProvider
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.shared.enums import Market


class _QuoteProber(Protocol):
    def fetch_quote_latest(self, instruments: list[InstrumentRef]) -> list[PriceRow]: ...


def _has(provider: _QuoteProber, symbol: str) -> bool:
    ref = InstrumentRef(symbol=symbol, market=Market.TW, board="")
    try:
        return bool(provider.fetch_quote_latest([ref]))
    except Exception:  # noqa: BLE001 — network/HTTP error -> treat as "not found here"
        return False


def probe_tw_board(
    symbol: str, *, twse: _QuoteProber | None = None, tpex: _QuoteProber | None = None
) -> str | None:
    """Return ``"TWSE"`` / ``"TPEx"`` for a TW *symbol*, or ``None`` if neither lists it."""
    twse = twse if twse is not None else TwseProvider()
    tpex = tpex if tpex is not None else TpexProvider()
    if _has(twse, symbol):
        return "TWSE"
    if _has(tpex, symbol):
        return "TPEx"
    return None
