"""TW intraday quote fallback via the ``twstock`` library (spec 20.8).

Tail of the TW QUOTE_LATEST chain (after twse/tpex/yfinance): a free intraday
source. ``fetch_quote_latest`` returns a ``PriceRow`` per symbol with the latest
trade price parsed straight to ``Decimal`` (no float); an unsuccessful response is
skipped so the registry falls through. The network call is isolated in ``_realtime``
for monkeypatching (the repo bans sockets in tests).
"""

from datetime import date
from decimal import Decimal
from typing import Any

from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.base import ProviderBase
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.shared.enums import Market


class TwStockProvider(ProviderBase):
    name = "twstock"

    def supports(self, data_type: DataType, market: Market | None) -> bool:
        return data_type is DataType.QUOTE_LATEST and market is Market.TW

    def _realtime(self, code: str) -> dict[str, Any]:
        """Fetch the twstock realtime payload for a code (isolated for monkeypatch)."""
        import twstock

        result: dict[str, Any] = twstock.realtime.get(code)
        return result

    def _row(self, symbol: str, close: Decimal) -> PriceRow:
        return PriceRow(
            instrument=symbol, market=Market.TW, as_of=date.today(),
            close=close, source=self.name,
        )

    def fetch_quote_latest(self, instruments: list[InstrumentRef]) -> list[PriceRow]:
        out: list[PriceRow] = []
        for ref in instruments:
            payload = self._realtime(ref.symbol)
            if not payload.get("success"):
                continue
            price = (payload.get("realtime") or {}).get("latest_trade_price")
            if price in (None, "", "-"):
                continue
            out.append(self._row(ref.symbol, Decimal(str(price))))
        return out
