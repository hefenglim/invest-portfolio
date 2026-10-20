"""Light reads and the per-build FX cache (PERF spec §8.5).

* ``get_close_series`` / ``get_fx_series`` start at the LAST row on or before their start —
  so a first day on a weekend, a holiday or during a suspension still carries the previous
  value — and equal ``get_price_history`` / ``get_fx_history`` on the rows they share;
* ``get_price_on_or_before`` is ``get_latest_price`` as at a past day;
* ``FxSeriesCache`` answers ``get_fx`` / ``get_fx_on`` / ``get_fx_history`` row for row from
  ONE query per directed pair.
"""

import sqlite3
from bisect import bisect_right
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.pricing.store import (
    FxSeriesCache,
    get_close_series,
    get_fx,
    get_fx_history,
    get_fx_on,
    get_fx_series,
    get_latest_price,
    get_price_history,
    get_price_on_or_before,
    upsert_fx,
    upsert_prices,
)
from portfolio_dash.shared.enums import Currency, Market

USD, TWD = Currency.USD, Currency.TWD
_FETCHED = datetime(2026, 6, 30, 12, 0)

# Fri 2026-01-02 … : weekdays only, a holiday on Mon 2026-01-19 (no row), and a suspension
# from Wed 2026-02-04 to Fri 2026-02-27 (no rows) — the three shapes a bounded load must
# carry a value across.
_HOLIDAY = date(2026, 1, 19)
_SUSPENDED = (date(2026, 2, 4), date(2026, 2, 27))


def _trading_days() -> list[date]:
    out: list[date] = []
    day = date(2026, 1, 2)
    while day <= date(2026, 3, 31):
        if (day.weekday() < 5 and day != _HOLIDAY
                and not _SUSPENDED[0] <= day <= _SUSPENDED[1]):
            out.append(day)
        day += timedelta(days=1)
    return out


@pytest.fixture
def seeded(conn: sqlite3.Connection) -> sqlite3.Connection:
    days = _trading_days()
    upsert_prices(conn, [PriceRow(instrument="AAA", market=Market.US, as_of=d,
                                  close=Decimal(100 + n) / Decimal(4), source="test")
                         for n, d in enumerate(days)], fetched_at=_FETCHED)
    upsert_fx(conn, [FxRow(base=USD, quote=TWD, as_of=d, rate=Decimal("31.5") + Decimal(n)
                           / Decimal(1000), source="test") for n, d in enumerate(days)],
              fetched_at=_FETCHED)
    return conn


def _latest_on_or_before(series: list[tuple[date, Decimal]], on: date
                         ) -> tuple[date, Decimal] | None:
    i = bisect_right([d for d, _v in series], on)
    return series[i - 1] if i else None


_STARTS = [
    date(2026, 1, 3),    # a Saturday
    date(2026, 1, 19),   # the holiday
    date(2026, 2, 10),   # inside the suspension
    date(2026, 1, 2),    # the first row's own day
    date(2025, 12, 1),   # before every row
    date(2026, 3, 31),   # the last row's day
]


@pytest.mark.parametrize("start", _STARTS)
def test_a_close_series_carries_the_value_into_its_first_day(
    seeded: sqlite3.Connection, start: date,
) -> None:
    end = date(2026, 3, 31)
    full = [(p.as_of, p.value) for p in get_price_history(seeded, "AAA", date(1900, 1, 1), end)]
    bounded = get_close_series(seeded, "AAA", start, end)
    # every day the bounded reader can be asked reads the same entry as the full series
    day = start
    while day <= end:
        assert _latest_on_or_before(bounded, day) == _latest_on_or_before(full, day), day
        day += timedelta(days=1)
    # and it is a suffix of the full series starting at the last row on or before `start`
    lead = _latest_on_or_before(full, start)
    first = full.index(lead) if lead is not None else 0
    assert bounded == full[first:]
    assert [str(v) for _d, v in bounded] == [str(v) for _d, v in full[first:]]


@pytest.mark.parametrize("start", _STARTS)
def test_an_fx_series_carries_the_rate_into_its_first_day(
    seeded: sqlite3.Connection, start: date,
) -> None:
    end = date(2026, 3, 31)
    full = [(r.as_of, r.rate) for r in get_fx_history(seeded, USD, TWD, date(1900, 1, 1), end)]
    bounded = get_fx_series(seeded, USD, TWD, start, end)
    lead = _latest_on_or_before(full, start)
    assert bounded == full[full.index(lead) if lead is not None else 0:]


def test_series_stop_at_their_end_and_are_empty_without_rows(seeded: sqlite3.Connection
                                                             ) -> None:
    got = get_close_series(seeded, "AAA", date(2026, 1, 3), date(2026, 1, 9))
    assert [d for d, _v in got] == [date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6),
                                    date(2026, 1, 7), date(2026, 1, 8), date(2026, 1, 9)]
    assert get_close_series(seeded, "NOPE", date(2026, 1, 1), date(2026, 3, 1)) == []
    assert get_fx_series(seeded, TWD, USD, date(2026, 1, 1), date(2026, 3, 1)) == []


@pytest.mark.parametrize("on", [date(2026, 1, 3), _HOLIDAY, date(2026, 2, 27),
                                date(2026, 3, 31), date(2025, 12, 31)])
def test_a_price_on_or_before_is_the_latest_price_of_a_world_cut_at_that_day(
    seeded: sqlite3.Connection, on: date,
) -> None:
    got = get_price_on_or_before(seeded, "AAA", on=on)
    seeded.execute("DELETE FROM prices WHERE as_of_date > ?", (on.isoformat(),))
    expected = get_latest_price(seeded, "AAA", now=datetime(on.year, on.month, on.day, 23, 59))
    assert got == expected
    if got is not None:
        assert got.stale == ((on - got.as_of).days > 4)


def test_the_fx_cache_answers_like_the_three_reads(seeded: sqlite3.Connection) -> None:
    cache = FxSeriesCache(seeded)
    now = datetime(2026, 4, 3, 9, 0)
    for base, quote in ((USD, TWD), (TWD, USD)):
        assert cache.latest(base, quote, now=now) == get_fx(seeded, base, quote, now=now)
        day = date(2025, 12, 30)
        while day <= date(2026, 4, 2):
            assert (cache.on_or_before(base, quote, day)
                    == get_fx_on(seeded, base, quote, on=day)), day
            day += timedelta(days=1)
        for start, end in ((date(1900, 1, 1), date(2026, 6, 11)),
                           (date(2026, 1, 3), date(2026, 1, 19)),
                           (date(2026, 2, 5), date(2026, 2, 26)),
                           (date(2026, 3, 31), date(2026, 3, 31))):
            assert cache.history(base, quote, start, end) == get_fx_history(
                seeded, base, quote, start, end)


def test_the_fx_cache_reads_each_directed_pair_once(seeded: sqlite3.Connection) -> None:
    queries: list[str] = []
    seeded.set_trace_callback(queries.append)
    try:
        cache = FxSeriesCache(seeded)
        for _ in range(3):
            cache.latest(USD, TWD, now=_FETCHED)
            cache.on_or_before(USD, TWD, date(2026, 2, 1))
            cache.history(USD, TWD, date(2026, 1, 1), date(2026, 3, 1))
            cache.on_or_before(TWD, USD, date(2026, 2, 1))
    finally:
        seeded.set_trace_callback(None)
    assert len([q for q in queries if "fx_rates" in q]) == 2
