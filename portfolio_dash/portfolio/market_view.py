"""Pure market-slice views over dashboard holdings (per_market scope, 2026-07-05 spec).

The per_market insight cards must cite numbers computed HERE (invariant #1: the LLM
never computes). Everything is :class:`~decimal.Decimal`; a holding without a market
value is skipped honestly (never guessed). ``llm_insight`` calls these the same way it
calls :mod:`portfolio.technicals` — a one-way read of the calculation core.

Market ↔ quote-currency note: this app's three markets map 1:1 onto their quote
currencies (TW→TWD, US→USD, MY→MYR — see ``rules/domain-ledger.md``). Ledger rows that
carry only a currency (realized rows, dividend events) are market-sliced through
:data:`MARKET_QUOTE_CCY`; holdings carry ``market`` directly and never need the map.
"""

from decimal import Decimal

from portfolio_dash.portfolio.dashboard_models import HoldingRow

_ZERO = Decimal("0")

# 1:1 in this app (domain-ledger.md). If a market ever lists in a second currency,
# ccy-carrying rows need a real instrument→market join instead of this map.
MARKET_QUOTE_CCY: dict[str, str] = {"TW": "TWD", "US": "USD", "MY": "MYR"}


def market_holdings(holdings: list[HoldingRow], market: str) -> list[HoldingRow]:
    """The holdings belonging to *market* (order preserved)."""
    return [h for h in holdings if h.market.value == market]


def market_allocation(holdings: list[HoldingRow], market: str) -> dict[str, Decimal]:
    """Sector weights WITHIN one market: sector value / market total value.

    Skips holdings without a market value (missing price) — the freshness variable
    names them; this function never fabricates. An empty/valueless market → ``{}``.
    """
    rows = market_holdings(holdings, market)
    by_sector: dict[str, Decimal] = {}
    total = _ZERO
    for h in rows:
        if h.market_value is None:
            continue
        by_sector[h.sector] = by_sector.get(h.sector, _ZERO) + h.market_value
        total += h.market_value
    if total == _ZERO:
        return {}
    return {sector: value / total for sector, value in by_sector.items()}
