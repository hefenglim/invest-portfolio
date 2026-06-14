import sqlite3
from datetime import date, datetime
from decimal import Decimal

from portfolio_dash.pricing.defaults import DEFAULT_PROVIDER_ORDER, default_registry
from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.base import ProviderBase
from portfolio_dash.pricing.refresh import refresh_quotes
from portfolio_dash.pricing.refs import FxPair, InstrumentRef
from portfolio_dash.pricing.registry import Registry
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.pricing.store import get_fx, get_latest_price
from portfolio_dash.shared.enums import Currency, Market

_NOW = datetime(2026, 6, 8, 12, 0, 0)
_AAPL = InstrumentRef(symbol="AAPL", market=Market.US)
_PAIR = FxPair(base=Currency.USD, quote=Currency.TWD)


class FakeAll(ProviderBase):
    name = "fake"

    def supports(self, data_type: DataType, market: Market | None) -> bool:
        return True

    def fetch_quote_latest(self, instruments: list[InstrumentRef]) -> list[PriceRow]:
        return [PriceRow(instrument=r.symbol, market=r.market, as_of=date(2026, 6, 8),
                         close=Decimal("100"), source=self.name) for r in instruments]

    def fetch_fx(self, pairs: list[FxPair]) -> list[FxRow]:
        return [FxRow(base=p.base, quote=p.quote, as_of=date(2026, 6, 8),
                      rate=Decimal("31.5"), source=self.name) for p in pairs]


def _reg(provider: ProviderBase) -> Registry:
    return Registry(
        providers={provider.name: provider},
        order={(DataType.QUOTE_LATEST, Market.US): [provider.name],
               (DataType.FX, None): [provider.name]},
    )


def test_refresh_quotes_stores_and_summarizes(conn: sqlite3.Connection) -> None:
    summary = refresh_quotes(conn, _reg(FakeAll()), [_AAPL], [_PAIR], now=_NOW)
    assert summary.ok == {"AAPL": "fake", "USDTWD": "fake"} and summary.failed == []
    price = get_latest_price(conn, "AAPL", now=_NOW)
    fx = get_fx(conn, Currency.USD, Currency.TWD, now=_NOW)
    assert price is not None and price.value == Decimal("100")
    assert fx is not None and fx.rate == Decimal("31.5")


def test_refresh_quotes_all_fail_no_raise(conn: sqlite3.Connection) -> None:
    empty = Registry(providers={}, order={})
    summary = refresh_quotes(conn, empty, [_AAPL], [_PAIR], now=_NOW)
    assert set(summary.failed) == {"AAPL", "USDTWD"} and summary.ok == {}
    assert get_latest_price(conn, "AAPL", now=_NOW) is None


def test_default_order_and_registry() -> None:
    assert (DataType.QUOTE_LATEST, Market.TW) in DEFAULT_PROVIDER_ORDER
    # spec 20.8 appended the free twstock fallback to the TW chain tail.
    expected = ["twse", "tpex", "yfinance", "twstock"]
    assert DEFAULT_PROVIDER_ORDER[(DataType.QUOTE_LATEST, Market.TW)] == expected
    reg = default_registry()  # must instantiate real providers without error
    assert isinstance(reg, Registry)
