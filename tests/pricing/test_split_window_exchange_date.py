"""The write window's upper bound is the EXCHANGE-LOCAL date of the fetch (owner ruling
2026-10-20; the price-basis split edge, spec 2026-10-18 §20 row 9).

``upsert_prices`` multiplies a delivered close by the splits the provider had already folded
in: the window ``(as_of, through]``. ``through`` used to be ``fetched_at.date()`` — and every
fetch carries the APP clock (Asia/Taipei), so the date was Taipei's. The US quote job fires at
16:30 New York, which is 04:30 / 05:30 Taipei the NEXT calendar day: every scheduled fetch of a
split's last pre-split session had a Taipei date equal to the ex-date, the seam folded the split
into a close the provider had not restated yet, and the row was stored at ratio x as traded
until ``history_daily`` healed it about 20 hours later.

``through`` is now the date in the market's OWN timezone (``shared.enums.MARKET_TIMEZONE``),
computed by ONE helper, ``pricing.store.split_window_through``, which the write seam and the
reconcile both call. TW (Asia/Taipei) and MY (Asia/Kuala_Lumpur, UTC+8, no DST) share
Taipei's date, so their stored rows do not move at all. A naive ``fetched_at`` keeps its own
``.date()``, exactly as before.

Every load-bearing assertion is on the stored TEXT: ``Decimal("400") == Decimal("400.0")``.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from decimal import Decimal
from typing import Self
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.api.instrument_service import reconcile_price_basis
from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.store import insert_corporate_action
from portfolio_dash.pricing.reconcile import reconcile_prices
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.pricing.store import (
    SplitFactorFn,
    _cap_dp,
    split_window_through,
    upsert_prices,
)
from portfolio_dash.scheduler.jobs import split_factor_fn
from portfolio_dash.shared.corporate_actions import (
    ActionIndex,
    CorporateAction,
    CorporateActionKind,
    split_factor,
)
from portfolio_dash.shared.enums import MARKET_TIMEZONE, Market
from portfolio_dash.shared.money import to_db

TAIPEI = ZoneInfo("Asia/Taipei")
NEW_YORK = ZoneInfo("America/New_York")
_RATIO = Decimal("20")
_AS_TRADED = Decimal("400")
_RESTATED = _AS_TRADED / _RATIO  # what the provider serves for a pre-split date once restated


def _split(symbol: str, ex_date: date, *, account: str = "schwab") -> CorporateAction:
    return CorporateAction(account_id=account, date=ex_date, kind=CorporateActionKind.SPLIT,
                           from_symbol=symbol, to_symbol=symbol, ratio_to=_RATIO,
                           ratio_from=Decimal("1"))


def _factor_of(*actions: CorporateAction) -> SplitFactorFn:
    """The REAL algebra (``ActionIndex`` + ``split_factor``), as ``split_factor_fn`` binds it."""
    index = ActionIndex.build(actions)

    def factor_of(symbol: str, *, after: date, through: date) -> Decimal:
        return split_factor(index, symbol, after=after, through=through)

    return factor_of


def _ny(day: date, hour: int, minute: int = 0) -> datetime:
    """A New York wall-clock instant, carried in the APP clock's zone (Asia/Taipei) — the
    form every production fetch hands the write seam (``shared.clock.app_now``)."""
    return datetime.combine(day, time(hour, minute), tzinfo=NEW_YORK).astimezone(TAIPEI)


def _write(conn: sqlite3.Connection, *, symbol: str, market: Market, as_of: date,
           close: Decimal, fetched_at: datetime, factor_of: SplitFactorFn) -> tuple[str, str]:
    """One provider row through the real write seam; returns the stored (close, split_basis)."""
    upsert_prices(conn, [PriceRow(instrument=symbol, market=market, as_of=as_of, close=close,
                                  source="test")],
                  fetched_at=fetched_at, factor_of=factor_of)
    row = conn.execute("SELECT close, split_basis FROM prices WHERE instrument=? AND "
                       "as_of_date=?", (symbol, as_of.isoformat())).fetchone()
    assert row is not None
    return str(row["close"]), str(row["split_basis"])


# --- the one owner of the exchange timezone ------------------------------------------------


def test_every_market_has_exactly_one_exchange_timezone() -> None:
    assert MARKET_TIMEZONE == {
        Market.TW: "Asia/Taipei",
        Market.US: "America/New_York",
        Market.MY: "Asia/Kuala_Lumpur",
    }
    for key in MARKET_TIMEZONE.values():
        ZoneInfo(key)  # every value is a real IANA key on this platform (tzdata)


# --- the helper ----------------------------------------------------------------------------


def test_the_scheduled_us_fetch_reads_the_new_york_date() -> None:
    """``quotes_us`` fires at 16:30 New York; the job's clock reads Tuesday 04:30 in Taipei,
    and the window closes on MONDAY — the session it fetched."""
    fired = _ny(date(2026, 6, 15), 16, 30)
    assert (fired.date(), fired.hour, fired.minute) == (date(2026, 6, 16), 4, 30)  # premise
    assert split_window_through(fired, Market.US) == date(2026, 6, 15)


@pytest.mark.parametrize(("taipei", "new_york_date"), [
    # Spring 2026: DST starts Sunday 03-08 02:00 — New York midnight moves from 13:00 Taipei
    # (EST, UTC-5) to 12:00 Taipei (EDT, UTC-4).
    (datetime(2026, 3, 7, 12, 59), date(2026, 3, 6)),     # Fri 23:59 EST
    (datetime(2026, 3, 7, 13, 0), date(2026, 3, 7)),      # Sat 00:00 EST
    (datetime(2026, 3, 9, 11, 59), date(2026, 3, 8)),     # Sun 23:59 EDT
    (datetime(2026, 3, 9, 12, 30), date(2026, 3, 9)),     # Mon 00:30 EDT (fixed EST: 03-08)
    # Autumn 2026: DST ends Sunday 11-01 02:00 — midnight moves from 12:00 back to 13:00 Taipei.
    (datetime(2026, 10, 31, 11, 59), date(2026, 10, 30)),  # Fri 23:59 EDT
    (datetime(2026, 10, 31, 12, 0), date(2026, 10, 31)),   # Sat 00:00 EDT
    (datetime(2026, 11, 2, 12, 30), date(2026, 11, 1)),    # Sun 23:30 EST (fixed EDT: 11-02)
    (datetime(2026, 11, 2, 13, 0), date(2026, 11, 2)),     # Mon 00:00 EST
])
def test_the_us_date_follows_new_york_across_both_dst_switches(
    taipei: datetime, new_york_date: date,
) -> None:
    assert split_window_through(taipei.replace(tzinfo=TAIPEI), Market.US) == new_york_date


@pytest.mark.parametrize("market", [Market.TW, Market.MY])
def test_a_tw_or_my_fetch_keeps_the_taipei_date_at_every_minute_of_the_day(
    market: Market,
) -> None:
    """Asia/Taipei and Asia/Kuala_Lumpur are both UTC+8 with no DST, so for every fetch the
    app clock stamps, the window's bound is exactly what ``fetched_at.date()`` gave."""
    start = datetime(2026, 6, 16, tzinfo=TAIPEI)
    for minutes in range(0, 24 * 60, 7):
        at = start + timedelta(minutes=minutes)
        assert split_window_through(at, market) == at.date()
    last = datetime(2026, 6, 16, 23, 59, 59, 999999, tzinfo=TAIPEI)
    assert split_window_through(last, market) == date(2026, 6, 16)


class _NoConversion(datetime):
    """A naive stamp that fails the test if anything converts it — ``astimezone`` on a naive
    datetime reads the HOST's zone, so a date assertion alone would pass on a host whose zone
    happens to put the instant on the same New York date."""

    def astimezone(self, tz: tzinfo | None = None) -> Self:
        raise AssertionError("a naive fetched_at must not be converted")


@pytest.mark.parametrize("market", list(Market))
@pytest.mark.parametrize(("hour", "minute"), [(0, 30), (5, 30), (23, 30)])
def test_a_naive_fetched_at_keeps_its_own_date_unconverted(
    market: Market, hour: int, minute: int,
) -> None:
    """No offset → no conversion: a naive stamp behaves exactly as before the ruling (the test
    clocks and the stress harness write naive stamps)."""
    naive = _NoConversion(2026, 6, 16, hour, minute)
    assert split_window_through(naive, market) == date(2026, 6, 16)


@pytest.mark.parametrize(("market", "bound"), [
    (Market.TW, date(2026, 6, 16)),   # 02:00 in Taipei
    (Market.MY, date(2026, 6, 16)),   # 02:00 in Kuala Lumpur
    (Market.US, date(2026, 6, 15)),   # 14:00 in New York
])
def test_an_aware_stamp_in_another_zone_is_converted_for_every_market(
    market: Market, bound: date,
) -> None:
    """TW and MY keep the old bound only for +08:00 (and naive) stamps. A UTC stamp — the
    scheduler's clock before the single-clock decision Q6 (2026-07-07) — of 18:00 on 06-15 is
    06-15 under ``fetched_at.date()`` and 06-16 on the TW / MY calendar: the rule as ruled."""
    at = datetime(2026, 6, 15, 18, 0, tzinfo=UTC)
    assert at.date() == date(2026, 6, 15)  # the pre-ruling bound
    assert split_window_through(at, market) == bound


# --- the write seam ------------------------------------------------------------------------

_EX = date(2026, 6, 16)          # Tuesday: the split takes effect at the New York open
_LAST = date(2026, 6, 15)        # Monday, the last pre-split session


@pytest.fixture
def prices() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    create_pricing_tables(c)
    yield c
    c.close()


def test_the_scheduled_fetch_of_the_last_pre_split_session_is_stored_as_traded(
    prices: sqlite3.Connection,
) -> None:
    """The defect itself: Monday 16:30 New York delivers Monday's close as traded."""
    got = _write(prices, symbol="AAA", market=Market.US, as_of=_LAST, close=_AS_TRADED,
                 fetched_at=_ny(_LAST, 16, 30), factor_of=_factor_of(_split("AAA", _EX)))
    assert got == ("400", "1")


@pytest.mark.parametrize("fetched_at", [
    _ny(_EX, 9, 31),   # just after the New York open on the ex-date
    _ny(_EX, 13, 0),   # mid-session
    _ny(_EX, 16, 30),  # the scheduled post-close fetch of the ex-date (Wed 04:30 Taipei)
], ids=["open", "mid-session", "post-close"])
def test_a_fetch_during_or_after_the_ex_date_session_multiplies_the_restated_close_back(
    prices: sqlite3.Connection, fetched_at: datetime,
) -> None:
    got = _write(prices, symbol="AAA", market=Market.US, as_of=_LAST, close=_RESTATED,
                 fetched_at=fetched_at, factor_of=_factor_of(_split("AAA", _EX)))
    assert got == ("400", "20")


@pytest.mark.parametrize(("ex_date", "last_session"), [
    (date(2026, 3, 4), date(2026, 3, 3)),     # before the spring switch (EST)
    (date(2026, 3, 10), date(2026, 3, 9)),    # after the spring switch (EDT)
    (date(2026, 10, 28), date(2026, 10, 27)),  # before the autumn switch (EDT)
    (date(2026, 11, 3), date(2026, 11, 2)),   # after the autumn switch (EST)
], ids=["spring-EST", "spring-EDT", "autumn-EDT", "autumn-EST"])
def test_both_scheduled_fetches_store_the_row_as_traded_on_either_side_of_dst(
    prices: sqlite3.Connection, ex_date: date, last_session: date,
) -> None:
    """The last pre-split session fetched at its own 16:30 (as traded), then re-fetched at the
    ex-date's 16:30 (restated) — both store 400. Each pre-ruling fetch of the first kind had a
    Taipei date equal to the ex-date, EST (05:30) or EDT (04:30) alike."""
    factor_of = _factor_of(_split("AAA", ex_date))
    first = _ny(last_session, 16, 30)
    assert first.date() == ex_date  # premise: the Taipei date the old window read
    assert _write(prices, symbol="AAA", market=Market.US, as_of=last_session, close=_AS_TRADED,
                  fetched_at=first, factor_of=factor_of) == ("400", "1")
    assert _write(prices, symbol="AAA", market=Market.US, as_of=last_session, close=_RESTATED,
                  fetched_at=_ny(ex_date, 16, 30), factor_of=factor_of) == ("400", "20")


@pytest.mark.parametrize(("taipei", "basis"), [
    (datetime(2026, 3, 9, 11, 59), "1"),   # New York Sun 03-08 23:59 EDT: before the ex-date
    (datetime(2026, 3, 9, 12, 0), "20"),   # New York Mon 03-09 00:00 EDT: the ex-date has begun
])
def test_the_window_turns_at_new_york_midnight_after_the_spring_switch(
    prices: sqlite3.Connection, taipei: datetime, basis: str,
) -> None:
    """A split effective Monday 03-09 (the first session after the switch), Friday's row: the
    bound moves at New York's midnight — 12:00 Taipei that night, not 13:00."""
    _, stored = _write(prices, symbol="AAA", market=Market.US, as_of=date(2026, 3, 6),
                       close=_AS_TRADED, fetched_at=taipei.replace(tzinfo=TAIPEI),
                       factor_of=_factor_of(_split("AAA", date(2026, 3, 9))))
    assert stored == basis


@pytest.mark.parametrize("market", [Market.TW, Market.MY])
@pytest.mark.parametrize("taipei", [
    datetime(2026, 6, 15, 23, 59),  # the last pre-split evening
    datetime(2026, 6, 16, 0, 0),    # the ex-date's first minute
    datetime(2026, 6, 16, 4, 30),
    datetime(2026, 6, 16, 14, 0),   # after the TW close
    datetime(2026, 6, 16, 23, 59),
])
def test_a_tw_or_my_row_stores_what_the_taipei_date_rule_stored(
    prices: sqlite3.Connection, market: Market, taipei: datetime,
) -> None:
    """Unchanged by the ruling: the stored row equals the pre-ruling rule's
    (``through = fetched_at.date()``), evaluated independently here."""
    at = taipei.replace(tzinfo=TAIPEI)
    factor_of = _factor_of(_split("TTT", _EX, account="tw_broker"))
    old_basis = factor_of("TTT", after=_LAST, through=at.date())
    got = _write(prices, symbol="TTT", market=market, as_of=_LAST, close=Decimal("12.345"),
                 fetched_at=at, factor_of=factor_of)
    expected = ("12.345", "1") if old_basis == 1 else (
        to_db(_cap_dp(Decimal("12.345") * old_basis, 4)), to_db(old_basis))
    assert got == expected


def test_a_naive_fetched_at_on_the_ex_date_still_folds_the_split(
    prices: sqlite3.Connection,
) -> None:
    """A naive stamp keeps the pre-ruling bound (its own date), on purpose: nothing says which
    zone it was written in, so it is not converted (00:30 on 06-16 read in any zone east of
    UTC-3:30 would be 06-15 in New York)."""
    got = _write(prices, symbol="AAA", market=Market.US, as_of=_LAST, close=_RESTATED,
                 fetched_at=_NoConversion(2026, 6, 16, 0, 30),
                 factor_of=_factor_of(_split("AAA", _EX)))
    assert got == ("400", "20")


def test_a_tw_row_with_a_utc_stamp_folds_on_the_taipei_date(
    prices: sqlite3.Connection,
) -> None:
    """A TW row stamped 18:00 UTC on 06-15 (02:00 on the ex-date in Taipei) now reads the split
    as folded; ``fetched_at.date()`` read 06-15 and did not. TW and MY move only for such
    non-+08:00 stamps."""
    got = _write(prices, symbol="TTT", market=Market.TW, as_of=_LAST, close=Decimal("12.5"),
                 fetched_at=datetime(2026, 6, 15, 18, 0, tzinfo=UTC),
                 factor_of=_factor_of(_split("TTT", _EX, account="tw_broker")))
    assert got == ("250.0", "20")


def test_one_batch_of_several_markets_takes_each_markets_own_bound(
    prices: sqlite3.Connection,
) -> None:
    """``refresh_history(benchmark_refs())`` writes 0050 (TW), ^GSPC (US) and ^KLSE (MY) in ONE
    ``upsert_prices`` call. At 04:30 Taipei on the ex-date the TW and MY bounds are 06-16 and
    the US bound is 06-15, so one bound for the whole batch (either one) gets a row wrong."""
    actions = [_split("TTT", _EX, account="tw_broker"), _split("AAA", _EX),
               _split("MMM", _EX, account="moomoo_my")]
    rows = [PriceRow(instrument=s, market=m, as_of=_LAST, close=c, source="test")
            for s, m, c in (("TTT", Market.TW, Decimal("12.5")), ("AAA", Market.US, _AS_TRADED),
                            ("MMM", Market.MY, Decimal("0.125")))]
    upsert_prices(prices, rows, fetched_at=_ny(_LAST, 16, 30), factor_of=_factor_of(*actions))
    stored = {r["instrument"]: (r["close"], r["split_basis"]) for r in prices.execute(
        "SELECT instrument, close, split_basis FROM prices")}
    assert stored == {"TTT": ("250.0", "20"), "AAA": ("400", "1"), "MMM": ("2.500", "20")}


_MIXED = ["1.5", "1.50", "305.3650", "0.005", "600", "0.4250", "0.14166666865348816"]


@pytest.mark.parametrize("market", list(Market))
def test_the_identity_stores_byte_identical_text_with_an_aware_stamp(
    prices: sqlite3.Connection, market: Market,
) -> None:
    """No split in any window: the aware stamp changes nothing in the stored bytes."""
    rows = [PriceRow(instrument="ZZZ", market=market, as_of=date(2026, 6, i + 1),
                     close=Decimal(v), source="test") for i, v in enumerate(_MIXED)]
    upsert_prices(prices, rows, fetched_at=_ny(_LAST, 16, 30),
                  factor_of=_factor_of(_split("AAA", _EX)))
    stored = [(r["close"], r["split_basis"]) for r in prices.execute(
        "SELECT close, split_basis FROM prices WHERE instrument='ZZZ' ORDER BY as_of_date")]
    assert stored == [(str(_cap_dp(Decimal(v), 4)), "1") for v in _MIXED]
    assert [c for c, _ in stored] == ["1.5", "1.50", "305.3650", "0.005", "600", "0.4250",
                                      "0.1417"]


# --- the reconcile uses the SAME window ----------------------------------------------------


def _ledger_db() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    create_pricing_tables(c)
    return c


#: (symbol, market, as_of, delivered close, fetched_at) — each fetch shape the window must read.
_FETCHES: list[tuple[str, Market, date, Decimal, datetime]] = [
    # US: the scheduled fetch of the last pre-split session (Tue 04:30 Taipei) — as traded.
    ("AAA", Market.US, _LAST, _AS_TRADED, _ny(_LAST, 16, 30)),
    # US: an owner refresh at Tue 08:00 Taipei = Mon 20:00 New York — as traded.
    ("AAA", Market.US, date(2026, 6, 12), Decimal("390"), datetime(2026, 6, 16, 8, 0,
                                                                   tzinfo=TAIPEI)),
    # US: a fetch during the ex-date session — restated.
    ("AAA", Market.US, date(2026, 6, 11), Decimal("19"), _ny(_EX, 11, 0)),
    # TW: a Taipei-dated fetch on the ex-date — restated (the TW rule, unchanged).
    ("TTT", Market.TW, _LAST, Decimal("12.345"), datetime(2026, 6, 16, 14, 0, tzinfo=TAIPEI)),
    # TW: the evening before — as traded.
    ("TTT", Market.TW, date(2026, 6, 12), Decimal("246.9"), datetime(2026, 6, 15, 23, 0,
                                                                     tzinfo=TAIPEI)),
]


def _insert_splits(conn: sqlite3.Connection) -> None:
    for symbol, account in (("AAA", "schwab"), ("TTT", "tw_broker")):
        insert_corporate_action(
            conn, account_id=account, action_date=_EX, kind=CorporateActionKind.SPLIT,
            from_symbol=symbol, to_symbol=symbol, ratio_to=_RATIO, ratio_from=Decimal("1"),
        )


def _snapshot(conn: sqlite3.Connection) -> list[tuple[str, ...]]:
    return [tuple(str(v) for v in r) for r in conn.execute(
        "SELECT instrument, as_of_date, close, close_raw, split_basis, fetched_at FROM prices "
        "ORDER BY instrument, as_of_date")]


def _write_all(conn: sqlite3.Connection, factor_of: SplitFactorFn) -> None:
    for symbol, market, as_of, close, at in _FETCHES:
        upsert_prices(conn, [PriceRow(instrument=symbol, market=market, as_of=as_of,
                                      close=close, source="test")],
                      fetched_at=at, factor_of=factor_of)


def test_a_split_entered_after_the_fetches_reconciles_to_the_bytes_the_write_seam_stores() -> None:
    """Rows written with no SPLIT in the ledger, then the SPLIT entered and reconciled through
    the app's entry point — byte-identical to the same fetches written with the SPLIT already
    there. One window, two operations."""
    late = _ledger_db()
    _write_all(late, split_factor_fn(late))  # empty ledger: every basis is the identity
    _insert_splits(late)
    assert reconcile_price_basis(late, ["AAA", "TTT"]) == 2

    first = _ledger_db()
    _insert_splits(first)
    _write_all(first, split_factor_fn(first))

    assert _snapshot(late) == _snapshot(first)
    by_key = {(r[0], r[1]): (r[2], r[4]) for r in _snapshot(first)}
    assert by_key == {
        ("AAA", "2026-06-11"): ("380", "20"),
        ("AAA", "2026-06-12"): ("390", "1"),
        ("AAA", "2026-06-15"): ("400", "1"),
        ("TTT", "2026-06-12"): ("246.9", "1"),
        ("TTT", "2026-06-15"): ("246.900", "20"),
    }
    late.close()
    first.close()


def test_the_reconcile_keeps_a_naive_stamped_row_on_its_own_date(
    prices: sqlite3.Connection,
) -> None:
    """00:30 on 06-16, stored without an offset: converted in any host zone east of UTC-3:30
    it would be 06-15 in New York and fold nothing (the helper's no-conversion test pins the
    rule on every host)."""
    upsert_prices(prices, [PriceRow(instrument="AAA", market=Market.US, as_of=_LAST,
                                    close=_RESTATED, source="test")],
                  fetched_at=datetime(2026, 6, 16, 0, 30))
    assert reconcile_prices(prices, ["AAA"], factor_of=_factor_of(_split("AAA", _EX))) == 1
    row = prices.execute("SELECT close, split_basis, fetched_at FROM prices").fetchone()
    assert (row["close"], row["split_basis"], row["fetched_at"]) == (
        "400", "20", "2026-06-16T00:30:00")


def test_the_reconcile_reads_a_utc_stamped_tw_row_on_the_taipei_date(
    prices: sqlite3.Connection,
) -> None:
    """The stored ISO offset is honoured on the way back: ``+00:00`` 18:00 on 06-15 is the
    TW ex-date, exactly as the write seam reads it."""
    upsert_prices(prices, [PriceRow(instrument="TTT", market=Market.TW, as_of=_LAST,
                                    close=Decimal("12.5"), source="test")],
                  fetched_at=datetime(2026, 6, 15, 18, 0, tzinfo=UTC))
    factor_of = _factor_of(_split("TTT", _EX, account="tw_broker"))
    assert reconcile_prices(prices, ["TTT"], factor_of=factor_of) == 1
    assert tuple(prices.execute("SELECT close, split_basis, fetched_at FROM prices")
                 .fetchone()) == ("250.0", "20", "2026-06-15T18:00:00+00:00")


def test_the_reconcile_moves_a_row_the_taipei_date_rule_had_folded(
    prices: sqlite3.Connection,
) -> None:
    """A row stored under the pre-ruling window (the defect's bytes) is restated to as traded
    by the next reconcile — the repair path ``scripts/check_split_window.py --apply`` uses."""
    fired = _ny(_LAST, 16, 30)

    def old_window(symbol: str, *, after: date, through: date) -> Decimal:
        return _factor_of(_split("AAA", _EX))(symbol, after=after, through=fired.date())

    _write(prices, symbol="AAA", market=Market.US, as_of=_LAST, close=_AS_TRADED,
           fetched_at=fired, factor_of=old_window)
    assert tuple(prices.execute("SELECT close, split_basis FROM prices").fetchone()) == (
        "8000", "20")
    assert reconcile_prices(prices, ["AAA"], factor_of=_factor_of(_split("AAA", _EX))) == 1
    assert tuple(prices.execute("SELECT close, split_basis FROM prices").fetchone()) == (
        "400", "1")


def test_the_reconcile_leaves_a_row_with_an_unknown_market_untouched(
    prices: sqlite3.Connection, caplog: pytest.LogCaptureFixture,
) -> None:
    """``market`` is NOT NULL and every writer stores a ``Market`` value; a hand-edited one
    degrades to "left untouched" (the reconcile's existing posture), never a raise that would
    stop every OTHER row's restatement."""
    for symbol in ("AAA", "BBB"):
        upsert_prices(prices, [PriceRow(instrument=symbol, market=Market.US, as_of=_LAST,
                                        close=_RESTATED, source="test")],
                      fetched_at=_ny(_EX, 16, 30))
    prices.execute("UPDATE prices SET market='XX' WHERE instrument='BBB'")
    prices.commit()
    factor_of = _factor_of(_split("AAA", _EX), _split("BBB", _EX))
    with caplog.at_level(logging.WARNING, logger="portfolio_dash.pricing.reconcile"):
        assert reconcile_prices(prices, ["AAA", "BBB"], factor_of=factor_of) == 1
    stored = dict(prices.execute("SELECT instrument, close FROM prices").fetchall())
    assert stored == {"AAA": "400", "BBB": "20"}
    skipped = [r.getMessage() for r in caplog.records if "BBB" in r.getMessage()]
    assert skipped == ["price-basis reconcile skipped BBB 2026-06-15: unknown market 'XX'"]
