import sqlite3
from datetime import date, datetime
from decimal import Decimal

from portfolio_dash.pricing.results import DividendEvent, FxRow, PriceRow
from portfolio_dash.pricing.store import (
    get_dividend_events,
    get_fx,
    get_fx_history,
    get_fx_on,
    get_latest_price,
    get_price_history,
    upsert_dividend_events,
    upsert_fx,
    upsert_prices,
)
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


def test_upsert_and_get_dividend_events_idempotent(conn: sqlite3.Connection) -> None:
    e1 = DividendEvent(instrument="2330", market=Market.TW, ex_date=date(2026, 6, 1),
                       pay_date=date(2026, 7, 1), cash_amount=Decimal("13.5"),
                       currency=Currency.TWD, source="finmind")
    e2 = DividendEvent(instrument="2330", market=Market.TW, ex_date=date(2025, 6, 1),
                       cash_amount=Decimal("11"), currency=Currency.TWD, source="finmind")
    upsert_dividend_events(conn, [e1, e2], fetched_at=_NOW)
    upsert_dividend_events(conn, [e1, e2], fetched_at=_NOW)  # idempotent
    out = get_dividend_events(conn, "2330")
    assert [e.ex_date for e in out] == [date(2025, 6, 1), date(2026, 6, 1)]  # ascending
    assert out[1].cash_amount == Decimal("13.5") and out[1].pay_date == date(2026, 7, 1)
    assert out[0].pay_date is None and out[1].currency == Currency.TWD


def _fx(rate: str, d: date) -> FxRow:
    return FxRow(base=Currency.USD, quote=Currency.TWD, as_of=d,
                 rate=Decimal(rate), source="test")


def test_get_fx_on_exact_and_carry_forward(conn: sqlite3.Connection) -> None:
    upsert_fx(conn, [_fx("32.1", date(2026, 6, 1)), _fx("32.5", date(2026, 6, 5))],
              fetched_at=_NOW)
    exact = get_fx_on(conn, Currency.USD, Currency.TWD, on=date(2026, 6, 5))
    assert exact is not None and exact.rate == Decimal("32.5")
    carry = get_fx_on(conn, Currency.USD, Currency.TWD, on=date(2026, 6, 3))
    assert carry is not None and carry.rate == Decimal("32.1")
    assert carry.as_of == date(2026, 6, 1) and carry.stale is False


def test_get_fx_on_none_before_first_rate(conn: sqlite3.Connection) -> None:
    upsert_fx(conn, [_fx("32.1", date(2026, 6, 1))], fetched_at=_NOW)
    assert get_fx_on(conn, Currency.USD, Currency.TWD, on=date(2026, 5, 31)) is None
    assert get_fx_on(conn, Currency.MYR, Currency.TWD, on=date(2026, 6, 5)) is None


def test_get_fx_history_bounds_and_order(conn: sqlite3.Connection) -> None:
    upsert_fx(conn, [_fx("32.1", date(2026, 6, 1)), _fx("32.3", date(2026, 6, 3)),
                     _fx("32.5", date(2026, 6, 5))], fetched_at=_NOW)
    rows = get_fx_history(conn, Currency.USD, Currency.TWD,
                          date(2026, 6, 1), date(2026, 6, 3))
    assert [r.rate for r in rows] == [Decimal("32.1"), Decimal("32.3")]
    assert [r.as_of for r in rows] == [date(2026, 6, 1), date(2026, 6, 3)]
    assert all(r.stale is False for r in rows)


def test_price_float_noise_capped_at_4dp(conn: sqlite3.Connection) -> None:
    """Float-noise cap (2026-07-03, human sign-off): a float-tail close stores at
    4 dp max, rounded half-up; clean values stay byte-identical (cap, never pad)."""
    upsert_prices(conn, [_price("305.364990234375", date(2026, 6, 6))], fetched_at=_NOW)
    r = get_latest_price(conn, "AAPL", now=_NOW)
    assert r is not None and r.value == Decimal("305.3650")
    # a clean 2-dp / integer value is NOT padded to 4 dp
    upsert_prices(conn, [_price("600", date(2026, 6, 7))], fetched_at=_NOW)
    row = conn.execute(
        "SELECT close FROM prices WHERE instrument='AAPL' AND as_of_date='2026-06-07'"
    ).fetchone()
    assert row["close"] == "600"


def test_fx_rate_capped_at_6dp(conn: sqlite3.Connection) -> None:
    upsert_fx(conn, [FxRow(base=Currency.USD, quote=Currency.TWD, as_of=date(2026, 6, 6),
                           rate=Decimal("32.55500012340001"), source="yfinance")],
              fetched_at=_NOW)
    r = get_fx(conn, Currency.USD, Currency.TWD, now=_NOW)
    assert r is not None and r.rate == Decimal("32.555000")


def _price_v(close: str, d: date, volume: Decimal | None) -> PriceRow:
    return PriceRow(instrument="AAPL", market=Market.US, as_of=d,
                    close=Decimal(close), volume=volume, source="yfinance")


def test_get_price_history_roundtrips_volume(conn: sqlite3.Connection) -> None:
    """Volume persists as a canonical integer string and reads back through
    ``get_price_history`` (integer Decimal, None passthrough)."""
    upsert_prices(conn, [
        _price_v("100", date(2026, 6, 4), Decimal("27997826")),  # integer volume
        _price_v("101", date(2026, 6, 5), Decimal("0")),         # a real no-trade session
        _price_v("102", date(2026, 6, 6), None),                 # gap: no volume stored
    ], fetched_at=_NOW)
    out = get_price_history(conn, "AAPL", date(2026, 6, 4), date(2026, 6, 6))
    assert [r.volume for r in out] == [Decimal("27997826"), Decimal("0"), None]
    # stored canonically as an integer TEXT string (not "0E-4" etc.)
    stored = {row["as_of_date"]: row["volume"] for row in
              conn.execute("SELECT as_of_date, volume FROM prices WHERE instrument='AAPL'")}
    assert stored["2026-06-04"] == "27997826" and stored["2026-06-05"] == "0"
    assert stored["2026-06-06"] is None


def test_upsert_volume_idempotent_and_updates(conn: sqlite3.Connection) -> None:
    """Re-upsert never duplicates and overwrites a previously-empty volume (deep backfill)."""
    upsert_prices(conn, [_price_v("100", date(2026, 6, 4), None)], fetched_at=_NOW)
    upsert_prices(conn, [_price_v("100", date(2026, 6, 4), Decimal("500"))], fetched_at=_NOW)
    rows = list(conn.execute("SELECT volume FROM prices WHERE instrument='AAPL'"))
    assert len(rows) == 1 and rows[0]["volume"] == "500"  # backfilled onto the existing row
