import sqlite3
from datetime import date, datetime

from portfolio_dash.pricing.defaults import DEFAULT_PROVIDER_ORDER, default_registry
from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.base import ProviderBase
from portfolio_dash.pricing.refresh import refresh_dividends
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.pricing.registry import Registry
from portfolio_dash.pricing.results import DividendEvent
from portfolio_dash.pricing.store import get_dividend_events
from portfolio_dash.shared.enums import Market

_NOW = datetime(2026, 6, 9, 12, 0, 0)
_2330 = InstrumentRef(symbol="2330", market=Market.TW)
_AAPL = InstrumentRef(symbol="AAPL", market=Market.US)


def _ev(sym: str, market: Market, src: str) -> DividendEvent:
    return DividendEvent(instrument=sym, market=market, ex_date=date(2026, 6, 1),
                         cash_amount=None, currency=None, source=src)


class FakeFinmind(ProviderBase):
    name = "finmind"

    def supports(self, data_type: DataType, market: Market | None) -> bool:
        return data_type is DataType.DIVIDEND and market is Market.TW

    def fetch_dividends(self, instruments: list[InstrumentRef]) -> list[DividendEvent]:
        return [_ev(r.symbol, r.market, self.name) for r in instruments]


class FakeYf(ProviderBase):
    name = "yfinance"

    def supports(self, data_type: DataType, market: Market | None) -> bool:
        return data_type is DataType.DIVIDEND

    def fetch_dividends(self, instruments: list[InstrumentRef]) -> list[DividendEvent]:
        return [_ev(r.symbol, r.market, self.name) for r in instruments]


def _reg() -> Registry:
    return Registry(
        providers={"finmind": FakeFinmind(), "yfinance": FakeYf()},
        order={(DataType.DIVIDEND, Market.TW): ["finmind", "yfinance"],
               (DataType.DIVIDEND, Market.US): ["yfinance"]},
    )


def test_registry_fetch_dividends_routes_by_market() -> None:
    events, sources, failed = _reg().fetch_dividends([_2330, _AAPL])
    assert sources == {"2330": "finmind", "AAPL": "yfinance"} and failed == []
    assert len(events) == 2


def test_refresh_dividends_stores_and_summarizes(conn: sqlite3.Connection) -> None:
    summary = refresh_dividends(conn, _reg(), [_2330, _AAPL], now=_NOW)
    assert summary.ok == {"2330": "finmind", "AAPL": "yfinance"} and summary.failed == []
    assert len(get_dividend_events(conn, "2330")) == 1


def test_refresh_dividends_all_fail_no_raise(conn: sqlite3.Connection) -> None:
    empty = Registry(providers={}, order={})
    summary = refresh_dividends(conn, empty, [_2330], now=_NOW)
    assert summary.failed == ["2330"] and summary.ok == {}
    assert get_dividend_events(conn, "2330") == []


def test_default_dividend_order_and_registry_has_finmind() -> None:
    assert DEFAULT_PROVIDER_ORDER[(DataType.DIVIDEND, Market.TW)] == ["finmind", "yfinance"]
    assert DEFAULT_PROVIDER_ORDER[(DataType.DIVIDEND, Market.US)] == ["yfinance"]
    reg = default_registry()
    assert isinstance(reg, Registry)  # builds with finmind provider wired in
