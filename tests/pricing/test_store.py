import sqlite3
from datetime import date, datetime
from decimal import Decimal

from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.pricing.store import get_fx, get_latest_price, upsert_fx, upsert_prices
from portfolio_dash.shared.enums import Currency, Market

_NOW = datetime(2026, 6, 8, 12, 0, 0)


def _price(close: str, d: date, source: str = "yfinance") -> PriceRow:
    return PriceRow(instrument="AAPL", market=Market.US, as_of=d,
                    close=Decimal(close), source=source)


def test_upsert_prices_idempotent(conn: sqlite3.Connection) -> None:
    upsert_prices(conn, [_price("100", date(2026, 6, 6))], fetched_at=_NOW)
    upsert_prices(conn, [_price("100", date(2026, 6, 6))], fetched_at=_NOW)  # no dup
    rows = list(conn.execute("SELECT close FROM prices WHERE instrument='AAPL'"))
    assert len(rows) == 1


def test_get_latest_price_returns_max_date(conn: sqlite3.Connection) -> None:
    upsert_prices(conn, [_price("100", date(2026, 6, 6)), _price("110", date(2026, 6, 8))],
                  fetched_at=_NOW)
    r = get_latest_price(conn, "AAPL", now=_NOW)
    assert r is not None and r.value == Decimal("110") and r.as_of == date(2026, 6, 8)
    assert r.stale is False


def test_get_latest_price_stale_when_old(conn: sqlite3.Connection) -> None:
    upsert_prices(conn, [_price("100", date(2026, 1, 1))], fetched_at=datetime(2026, 1, 1))
    r = get_latest_price(conn, "AAPL", now=_NOW, max_age_days=5)
    assert r is not None and r.stale is True


def test_get_latest_price_none_when_absent(conn: sqlite3.Connection) -> None:
    assert get_latest_price(conn, "NOPE", now=_NOW) is None


def test_upsert_and_get_fx(conn: sqlite3.Connection) -> None:
    upsert_fx(conn, [FxRow(base=Currency.USD, quote=Currency.TWD, as_of=date(2026, 6, 8),
                           rate=Decimal("31.5"), source="yfinance")], fetched_at=_NOW)
    r = get_fx(conn, Currency.USD, Currency.TWD, now=_NOW)
    assert r is not None and r.rate == Decimal("31.5") and r.stale is False
