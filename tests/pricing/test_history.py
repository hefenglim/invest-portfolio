import sqlite3
from datetime import date, datetime
from decimal import Decimal

from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.base import ProviderBase
from portfolio_dash.pricing.refresh import refresh_history
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.pricing.registry import Registry
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import get_price_history, upsert_prices
from portfolio_dash.shared.enums import Market

_NOW = datetime(2026, 6, 8, 12, 0, 0)
_AAPL = InstrumentRef(symbol="AAPL", market=Market.US)


def _series() -> list[PriceRow]:
    return [PriceRow(instrument="AAPL", market=Market.US, as_of=date(2026, 6, d),
                     close=Decimal(str(100 + d)), source="hist") for d in (4, 5, 6)]


class HistProvider(ProviderBase):
    name = "hist"

    def supports(self, data_type: DataType, market: Market | None) -> bool:
        return data_type is DataType.QUOTE_HISTORY

    def fetch_quote_history(self, instrument: InstrumentRef, start: date) -> list[PriceRow]:
        return _series()


def _reg() -> Registry:
    return Registry(providers={"hist": HistProvider()},
                    order={(DataType.QUOTE_HISTORY, Market.US): ["hist"]})


def test_store_history_roundtrip_ascending_and_idempotent(conn: sqlite3.Connection) -> None:
    upsert_prices(conn, _series(), fetched_at=_NOW)
    upsert_prices(conn, _series(), fetched_at=_NOW)  # idempotent
    out = get_price_history(conn, "AAPL", date(2026, 6, 4), date(2026, 6, 6))
    assert [r.as_of.day for r in out] == [4, 5, 6]  # ascending
    assert [r.value for r in out] == [Decimal("104"), Decimal("105"), Decimal("106")]


def test_get_price_history_range_filters(conn: sqlite3.Connection) -> None:
    upsert_prices(conn, _series(), fetched_at=_NOW)
    out = get_price_history(conn, "AAPL", date(2026, 6, 5), date(2026, 6, 5))
    assert [r.as_of.day for r in out] == [5]


def test_registry_fetch_quote_history_routes(conn: sqlite3.Connection) -> None:
    rows, sources, failed = _reg().fetch_quote_history([_AAPL], date(2026, 1, 1))
    assert len(rows) == 3 and sources == {"AAPL": "hist"} and failed == []


def test_refresh_history_stores_and_summarizes(conn: sqlite3.Connection) -> None:
    summary = refresh_history(conn, _reg(), [_AAPL], date(2026, 1, 1), now=_NOW)
    assert summary.ok == {"AAPL": "hist"} and summary.failed == []
    assert len(get_price_history(conn, "AAPL", date(2026, 6, 1), date(2026, 6, 30))) == 3
