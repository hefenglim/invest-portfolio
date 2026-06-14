"""US quote fallback via stockprices.dev (spec 20.8).

A key-less, latest-only US quote source (no history — it exposes only a current
price). Sits after yfinance in the US QUOTE_LATEST chain; it is flaky (400/429),
so any failure simply yields no row and the registry falls through. The price lives
under the capitalized ``"Price"`` JSON key (discovered at probe time). Parsed to
``Decimal`` (no float into the chain); HTTP I/O is isolated in ``_quote`` for
monkeypatching (the repo bans sockets in tests).
"""

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import requests

from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.base import ProviderBase
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.shared.enums import Market

_URL = "https://stockprices.dev/api/stocks"
_TIMEOUT_S = 15


class StockPricesDevProvider(ProviderBase):
    name = "stockprices_dev"

    def supports(self, data_type: DataType, market: Market | None) -> bool:
        # Latest-only: no history support (the endpoint exposes a single current price).
        return data_type is DataType.QUOTE_LATEST and market is Market.US

    def _quote(self, symbol: str) -> dict[str, Any]:
        """Fetch the stockprices.dev quote JSON for a symbol (isolated for monkeypatch)."""
        resp = requests.get(f"{_URL}/{symbol}", timeout=_TIMEOUT_S)
        resp.raise_for_status()
        payload: dict[str, Any] = resp.json()
        return payload

    def fetch_quote_latest(self, instruments: list[InstrumentRef]) -> list[PriceRow]:
        out: list[PriceRow] = []
        for ref in instruments:
            payload = self._quote(ref.symbol)
            raw = payload.get("Price")
            if raw is None:
                continue
            try:
                close = Decimal(str(raw))
            except (InvalidOperation, ValueError):
                continue
            if not close.is_finite():
                continue
            out.append(PriceRow(
                instrument=ref.symbol, market=Market.US, as_of=date.today(),
                close=close, source=self.name,
            ))
        return out
