"""Tests for the spec-20.8 free quote-fallback providers.

twstock (TW intraday), stockprices.dev (US latest-only), klsescreener +
malaysiastock (MY 3-dp string). Each: ``supports`` is True only for its market's
QUOTE_LATEST and False elsewhere; ``fetch_quote_latest`` over a monkeypatched
HTTP/lib seam yields a ``PriceRow`` with a ``Decimal`` close (MY sources preserve
3-dp from the string) and ``source == name``. Also asserts the default registry
order wires the new fallbacks. No network is touched.
"""

from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.klsescreener_provider import KlseScreenerProvider
from portfolio_dash.pricing.providers.malaysiastock_provider import MalaysiaStockProvider
from portfolio_dash.pricing.providers.stockprices_dev_provider import StockPricesDevProvider
from portfolio_dash.pricing.providers.twstock_provider import TwStockProvider
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.shared.enums import Market

# --- A minimal klsescreener DOM (real shape: <h2 id="price" data-value="0.555">) ---
_KLSE_HTML = (
    '<html><body><h2 id="price" data-value="0.555">0.555</h2>'
    '<h2 id="price-fixed" data-value="0.555">0.555</h2></body></html>'
)
_MALAYSIASTOCK_HTML = (
    '<html><body><span id="SharePrice" class="price">0.075</span></body></html>'
)


def _ref(symbol: str, market: Market) -> InstrumentRef:
    return InstrumentRef(symbol=symbol, market=market)


# --- supports() gating --------------------------------------------------------


def test_twstock_supports_tw_only() -> None:
    p = TwStockProvider()
    assert p.supports(DataType.QUOTE_LATEST, Market.TW)
    assert not p.supports(DataType.QUOTE_LATEST, Market.US)
    assert not p.supports(DataType.QUOTE_HISTORY, Market.TW)
    assert not p.supports(DataType.FX, None)


def test_stockprices_dev_supports_us_latest_only() -> None:
    p = StockPricesDevProvider()
    assert p.supports(DataType.QUOTE_LATEST, Market.US)
    assert not p.supports(DataType.QUOTE_LATEST, Market.TW)
    assert not p.supports(DataType.QUOTE_HISTORY, Market.US)  # latest-only
    assert not p.supports(DataType.DIVIDEND, Market.US)


def test_klsescreener_supports_my_only() -> None:
    p = KlseScreenerProvider()
    assert p.supports(DataType.QUOTE_LATEST, Market.MY)
    assert not p.supports(DataType.QUOTE_LATEST, Market.US)
    assert not p.supports(DataType.QUOTE_HISTORY, Market.MY)


def test_malaysiastock_supports_my_only() -> None:
    p = MalaysiaStockProvider()
    assert p.supports(DataType.QUOTE_LATEST, Market.MY)
    assert not p.supports(DataType.QUOTE_LATEST, Market.TW)


# --- fetch_quote_latest (monkeypatched seams) ---------------------------------


def test_twstock_fetch_quote(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"success": True, "realtime": {"latest_trade_price": "2295.0000"}}
    p = TwStockProvider()
    monkeypatch.setattr(p, "_realtime", lambda code: payload)
    rows = p.fetch_quote_latest([_ref("2330", Market.TW)])
    assert len(rows) == 1
    r = rows[0]
    assert r.instrument == "2330" and r.market is Market.TW
    assert r.close == Decimal("2295.0000") and r.source == "twstock"


def test_twstock_fetch_skips_unsuccessful(monkeypatch: pytest.MonkeyPatch) -> None:
    p = TwStockProvider()
    monkeypatch.setattr(p, "_realtime", lambda code: {"success": False})
    assert p.fetch_quote_latest([_ref("2330", Market.TW)]) == []


def test_stockprices_dev_fetch_quote(monkeypatch: pytest.MonkeyPatch) -> None:
    p = StockPricesDevProvider()
    monkeypatch.setattr(
        p, "_quote", lambda symbol: {"Ticker": symbol, "Price": 307.34}
    )
    rows = p.fetch_quote_latest([_ref("AAPL", Market.US)])
    assert len(rows) == 1
    r = rows[0]
    assert r.instrument == "AAPL" and r.market is Market.US
    assert r.close == Decimal("307.34") and r.source == "stockprices_dev"


def test_stockprices_dev_skips_missing_price(monkeypatch: pytest.MonkeyPatch) -> None:
    p = StockPricesDevProvider()
    monkeypatch.setattr(p, "_quote", lambda symbol: {"Ticker": symbol})
    assert p.fetch_quote_latest([_ref("AAPL", Market.US)]) == []


def test_klsescreener_fetch_preserves_3dp(monkeypatch: pytest.MonkeyPatch) -> None:
    p = KlseScreenerProvider()
    monkeypatch.setattr(p, "_view_html", lambda code: _KLSE_HTML)
    rows = p.fetch_quote_latest([_ref("5212", Market.MY)])
    assert len(rows) == 1
    r = rows[0]
    assert r.instrument == "5212" and r.market is Market.MY
    # 3-dp string parsed directly to Decimal (no float collapse).
    assert r.close == Decimal("0.555") and r.source == "klsescreener"


def test_klsescreener_skips_when_no_node(monkeypatch: pytest.MonkeyPatch) -> None:
    p = KlseScreenerProvider()
    monkeypatch.setattr(p, "_view_html", lambda code: "<html><body></body></html>")
    assert p.fetch_quote_latest([_ref("5212", Market.MY)]) == []


def test_malaysiastock_fetch_preserves_3dp(monkeypatch: pytest.MonkeyPatch) -> None:
    p = MalaysiaStockProvider()
    monkeypatch.setattr(p, "_page_html", lambda code: _MALAYSIASTOCK_HTML)
    rows = p.fetch_quote_latest([_ref("3182", Market.MY)])
    assert len(rows) == 1
    r = rows[0]
    assert r.close == Decimal("0.075") and r.source == "malaysiastock"
    assert r.market is Market.MY


def test_providers_use_today_as_of() -> None:
    p = TwStockProvider()
    # as_of defaults to today's date for the latest-quote providers.
    row = p._row("2330", Decimal("1"))
    assert row.as_of == date.today()


# --- default registry order ---------------------------------------------------


def test_default_order_includes_new_fallbacks() -> None:
    from portfolio_dash.pricing.defaults import DEFAULT_PROVIDER_ORDER

    tw = DEFAULT_PROVIDER_ORDER[(DataType.QUOTE_LATEST, Market.TW)]
    us = DEFAULT_PROVIDER_ORDER[(DataType.QUOTE_LATEST, Market.US)]
    my = DEFAULT_PROVIDER_ORDER[(DataType.QUOTE_LATEST, Market.MY)]
    assert tw == ["twse", "tpex", "yfinance", "twstock"]
    assert us == ["yfinance", "stockprices_dev"]
    assert my == ["yfinance", "klsescreener", "malaysiastock"]


def test_default_registry_wires_new_providers() -> None:
    from portfolio_dash.pricing.defaults import default_registry

    reg = default_registry()
    names = set(reg._providers)
    assert {"twstock", "stockprices_dev", "klsescreener", "malaysiastock"} <= names


def test_new_providers_in_registry_chain() -> None:
    from portfolio_dash.pricing.defaults import default_registry

    reg = default_registry()
    chain = [p.name for p in reg._chain(DataType.QUOTE_LATEST, Market.MY)]
    assert chain[-2:] == ["klsescreener", "malaysiastock"]
