"""MY 3-dp string quote fallback via Malaysiastock.biz (spec 20.8).

A secondary, key-less MY (Bursa) string source (redundancy against klsescreener's
single-source risk). Its quote page displays the price as a 3-dp string, preserving
Bursa tick precision; this provider parses that string straight to ``Decimal`` (no
float). Sits last in the MY QUOTE_LATEST chain. HTML I/O is isolated in ``_page_html``
for monkeypatching (the repo bans sockets in tests).
"""

import re
from datetime import date
from decimal import Decimal, InvalidOperation

import requests
from bs4 import BeautifulSoup

from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.base import ProviderBase
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.shared.enums import Market

_URL = "https://www.malaysiastock.biz/Corporate-Infomation.aspx?securityCode={code}"
_TIMEOUT_S = 20
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; portfolio-dash)"}
_PRICE_RE = re.compile(r"^\d+(?:\.\d+)?$")


def _parse_price_string(html: str) -> str | None:
    """Extract the price string from a Malaysiastock.biz page.

    The share price node carries ``id="SharePrice"`` whose text is a decimal string
    (e.g. ``"0.075"``). Returns the trimmed string only if it looks numeric, so a
    layout change yields None (degrade) rather than a bogus parse.
    """
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one("#SharePrice")
    if node is None:
        return None
    text = node.get_text(strip=True)
    return text if _PRICE_RE.match(text) else None


class MalaysiaStockProvider(ProviderBase):
    name = "malaysiastock"

    def supports(self, data_type: DataType, market: Market | None) -> bool:
        return data_type is DataType.QUOTE_LATEST and market is Market.MY

    def _page_html(self, code: str) -> str:
        """Fetch the Malaysiastock.biz quote-page HTML (isolated for monkeypatch)."""
        resp = requests.get(_URL.format(code=code), headers=_HEADERS, timeout=_TIMEOUT_S)
        resp.raise_for_status()
        return resp.text

    def fetch_quote_latest(self, instruments: list[InstrumentRef]) -> list[PriceRow]:
        out: list[PriceRow] = []
        for ref in instruments:
            price_str = _parse_price_string(self._page_html(ref.symbol))
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
