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
    def fetch_quote_named(
        self, instrument: InstrumentRef
    ) -> tuple[PriceRow | None, str | None]: ...


def _listing(provider: _QuoteProber, symbol: str) -> tuple[bool, str | None]:
    """(listed on this board, the exchange's short name when it gave one)."""
    ref = InstrumentRef(symbol=symbol, market=Market.TW, board="")
    try:
        row, name = provider.fetch_quote_named(ref)
    except Exception:  # noqa: BLE001 — network/HTTP error -> treat as "not found here"
        return False, None
    return row is not None, name


def probe_tw_board(
    symbol: str, *, twse: _QuoteProber | None = None, tpex: _QuoteProber | None = None,
    names: dict[str, str] | None = None,
) -> str | None:
    """Return ``"TWSE"`` / ``"TPEx"`` for a TW *symbol*, or ``None`` if neither lists it.

    ``names`` (owner 2026-09-30, item 8) is an optional sink: when given, the board that
    lists *symbol* also records the exchange's own short name (台積電 / 群聯) under
    ``names[symbol]`` — taken from the SAME response the probe needed anyway, so the
    registration door can keep it as an alias without a second request. Absent a name the
    sink is left untouched; the probe's answer never depends on it.
    """
    twse = twse if twse is not None else TwseProvider()
    tpex = tpex if tpex is not None else TpexProvider()
    for board, provider in (("TWSE", twse), ("TPEx", tpex)):
        listed, name = _listing(provider, symbol)
        if listed:
            if names is not None and name:
                names[symbol] = name
            return board
    return None
