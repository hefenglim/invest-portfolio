"""MY 3-dp string quote fallback via KLSE Screener (spec 20.8).

A key-less MY (Bursa) source whose stock-view page serves the live price as a
**string** in ``<h2 id="price" data-value="2.260">2.260</h2>`` — preserving Bursa's
native tick precision (sub-RM1 0.005, ETF 0.001), unlike yfinance's float64 columns.
This provider parses that string straight to ``Decimal``, so 3 decimal places survive
intact (``rules/data-and-pricing.md``: do not truncate MY prices). Sits after
yfinance in the MY QUOTE_LATEST chain for tick-precision corroboration / hole-filling.
HTML I/O is isolated in ``_view_html`` for monkeypatching (the repo bans sockets in
tests).
"""

from datetime import date
from decimal import Decimal, InvalidOperation

import requests
from bs4 import BeautifulSoup

from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.base import ProviderBase
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.shared.enums import Market

_URL = "https://www.klsescreener.com/v2/stocks/view/{code}"
_TIMEOUT_S = 20
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; portfolio-dash)"}


def _parse_price_string(html: str) -> str | None:
    """Extract the canonical 3-dp price string from a klsescreener view page.

    Prefers the ``data-value`` attribute of ``#price`` (the page's own string form),
    falling back to the element's text; both carry full precision (e.g. ``"2.260"``).
    """
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one("#price")
    if node is None:
        return None
    value = node.get("data-value")
    if isinstance(value, str) and value.strip():
        return value.strip()
    text = node.get_text(strip=True)
    return text or None


class KlseScreenerProvider(ProviderBase):
    name = "klsescreener"

    def supports(self, data_type: DataType, market: Market | None) -> bool:
        return data_type is DataType.QUOTE_LATEST and market is Market.MY

    def _view_html(self, code: str) -> str:
        """Fetch the klsescreener stock-view HTML (isolated for monkeypatch)."""
        resp = requests.get(_URL.format(code=code), headers=_HEADERS, timeout=_TIMEOUT_S)
        resp.raise_for_status()
        return resp.text

    def fetch_quote_latest(self, instruments: list[InstrumentRef]) -> list[PriceRow]:
        out: list[PriceRow] = []
        for ref in instruments:
            price_str = _parse_price_string(self._view_html(ref.symbol))
            if not price_str:
                continue
            try:
                close = Decimal(price_str)  # parse the string directly -> 3-dp preserved
            except InvalidOperation:
                continue
            if not close.is_finite():
                continue
            out.append(PriceRow(
                instrument=ref.symbol, market=Market.MY, as_of=date.today(),
                close=close, source=self.name,
            ))
        return out
