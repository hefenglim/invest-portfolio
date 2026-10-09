"""CR-14 (pre-merge code review, 2026-10-04): a derived cross-rate row is rewritten only when
its value changed.

``derive_cross_rates`` re-derived AND re-upserted MYR/TWD for every stored date on every FX
write, restamping ``fetched_at`` on rows whose value had not moved: each quote refresh (three
market jobs a day) and each backfill rewrote the whole history — ~1,800 rows for five years —
and the count grew with the history. Measured before the fix on the 30-day fixture below: a
one-day refresh changed 33 rows (2 legs + 31 derived) where 3 were new.

The fix keeps the derivation whole-history (the read is one indexed join) but writes only a
row that differs from the derivation of its legs, compared on the exact TEXT the write seam
would store and the source. That covers, with no per-caller plumbing: a day whose leg was
written or changed this run, a gap (legs on a day with no derived row), and a row some other
writer left on a day nobody re-fetched — a pre-M1 provider row (prod is on v0.1.28, which
fetched MYR/TWD directly for its whole history) or a seed script's fixed series. Scoping the
derivation to "the dates this run touched" would have left those last rows in place forever.
"""

import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from portfolio_dash.pricing.cross import derive_cross_rates
from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.base import ProviderBase
from portfolio_dash.pricing.refresh import refresh_fx_history, refresh_quotes
from portfolio_dash.pricing.refs import FxPair, InstrumentRef
from portfolio_dash.pricing.registry import Registry
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.pricing.store import _no_factor, upsert_fx
from portfolio_dash.scheduler.jobs import REPORTING_FX_PAIRS
from portfolio_dash.shared.enums import Currency, Market

_T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)    # when the history was written
_T1 = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)   # this run
_TODAY = date(2026, 9, 15)
_HISTORY = [date(2026, 8, 1) + timedelta(days=i) for i in range(30)]   # 08-01 .. 08-30
_Q6 = Decimal("0.000001")


def _usdtwd(day: date) -> Decimal:
    return Decimal("31.5") + Decimal(day.toordinal() % 17) / 100


def _usdmyr(day: date) -> Decimal:
    return Decimal("4.05") + Decimal(day.toordinal() % 11) / 1000


def _legs(day: date, *, twd: Decimal | None = None, myr: Decimal | None = None) -> list[FxRow]:
    return [
        FxRow(base=Currency.USD, quote=Currency.TWD, as_of=day,
              rate=twd if twd is not None else _usdtwd(day), source="p"),
        FxRow(base=Currency.USD, quote=Currency.MYR, as_of=day,
              rate=myr if myr is not None else _usdmyr(day), source="p"),
    ]


def _seed_history(conn: sqlite3.Connection, days: list[date] = _HISTORY) -> None:
    upsert_fx(conn, [r for d in days for r in _legs(d)], fetched_at=_T0)
    derive_cross_rates(conn, fetched_at=_T0)


def _derived(conn: sqlite3.Connection) -> dict[date, tuple[str, str, str]]:
    """MYR/TWD per day → (stored rate TEXT, source, fetched_at)."""
    return {date.fromisoformat(r[0]): (str(r[1]), str(r[2]), str(r[3])) for r in conn.execute(
        "SELECT as_of_date, rate, source, fetched_at FROM fx_rates "
        "WHERE base = 'MYR' AND quote = 'TWD'")}


def _stamped(conn: sqlite3.Connection, when: datetime) -> list[date]:
    return sorted(d for d, (_, _, at) in _derived(conn).items() if at == when.isoformat())


class _Fx(ProviderBase):
    """Answers the two USD legs from a fixed table: ``latest`` for a quote refresh, ``days``
    for a history backfill."""

    name = "fake"

    def __init__(self, *, latest: list[FxRow] | None = None,
                 days: list[FxRow] | None = None) -> None:
        self._latest = latest or []
        self._days = days or []

    def supports(self, data_type: DataType, market: Market | None) -> bool:
        return True

    def fetch_quote_latest(self, instruments: list[InstrumentRef]) -> list[PriceRow]:
        return []

    def fetch_fx(self, pairs: list[FxPair]) -> list[FxRow]:
        return [r for r in self._latest
                if any((r.base, r.quote) == (p.base, p.quote) for p in pairs)]

    def fetch_fx_history(self, pair: FxPair, start: date) -> list[FxRow]:
        return [r for r in self._days
                if (r.base, r.quote) == (pair.base, pair.quote) and r.as_of >= start]


def _reg(provider: _Fx) -> Registry:
    return Registry(
        providers={"fake": provider},
        order={(DataType.FX, None): ["fake"],
               **{(DataType.QUOTE_LATEST, m): ["fake"] for m in Market}},
    )


def _changes(conn: sqlite3.Connection, run: Callable[[], object]) -> int:
    before = conn.total_changes
    run()
    return conn.total_changes - before


def _refresh(conn: sqlite3.Connection, latest: list[FxRow]) -> dict[str, str]:
    return refresh_quotes(conn, _reg(_Fx(latest=latest)), [], REPORTING_FX_PAIRS, now=_T1,
                          factor_of=_no_factor).ok


# ------------------------------------------------------------------ the six behaviours


def test_a_one_day_refresh_writes_only_that_days_derived_row(conn: sqlite3.Connection) -> None:
    _seed_history(conn)
    ok: dict[str, str] = {}
    changed = _changes(conn, lambda: ok.update(_refresh(conn, _legs(_TODAY))))
    assert changed == 3                       # two legs + today's MYR/TWD (was 33)
    assert _stamped(conn, _T1) == [_TODAY]
    assert ok["MYRTWD"] == "derived:USD"      # still reported, as before


def test_untouched_rows_keep_their_fetched_at(conn: sqlite3.Connection) -> None:
    """A refresh that re-delivers the latest day unchanged (a weekend run) restamps the two
    legs it fetched and nothing else — and still reports the pair."""
    _seed_history(conn)
    before = _derived(conn)
    ok: dict[str, str] = {}
    changed = _changes(conn, lambda: ok.update(_refresh(conn, _legs(_HISTORY[-1]))))
    assert changed == 2                       # the legs only
    assert _derived(conn) == before           # every MYR/TWD row byte-identical, stamp too
    assert ok["MYRTWD"] == "derived:USD"


def test_an_n_day_backfill_derives_n_rows(conn: sqlite3.Connection) -> None:
    earlier = [date(2026, 7, 1) + timedelta(days=i) for i in range(10)]
    # On an empty table: N days of legs → N derived rows.
    summary = refresh_fx_history(conn, _reg(_Fx(days=[r for d in earlier for r in _legs(d)])),
                                 REPORTING_FX_PAIRS, earlier[0], now=_T1)
    assert summary.ok["MYRTWD"] == "derived:USD"
    assert _stamped(conn, _T1) == earlier
    # Beside an existing history: the 10 new days are written, the 30 old ones are not.
    conn.execute("DELETE FROM fx_rates")
    _seed_history(conn)
    changed = _changes(conn, lambda: refresh_fx_history(
        conn, _reg(_Fx(days=[r for d in earlier for r in _legs(d)])), REPORTING_FX_PAIRS,
        earlier[0], now=_T1))
    assert changed == 2 * 10 + 10
    assert _stamped(conn, _T1) == earlier
    assert len(_derived(conn)) == 40


def test_a_changed_historical_leg_re_derives_that_date(conn: sqlite3.Connection) -> None:
    """A backfill re-delivers the whole history with ONE revised day: that day's MYR/TWD is
    re-derived from the new leg; the other 29 keep their row and their stamp."""
    _seed_history(conn)
    revised = _HISTORY[9]
    new_twd = _usdtwd(revised) + Decimal("0.25")
    days = [r for d in _HISTORY
            for r in (_legs(d, twd=new_twd) if d == revised else _legs(d))]
    refresh_fx_history(conn, _reg(_Fx(days=days)), REPORTING_FX_PAIRS, _HISTORY[0], now=_T1)
    assert _stamped(conn, _T1) == [revised]
    rate, source, _ = _derived(conn)[revised]
    assert Decimal(rate) == (new_twd / _usdmyr(revised)).quantize(_Q6, rounding=ROUND_HALF_UP)
    assert source == "derived:USD"


def test_a_gap_is_filled(conn: sqlite3.Connection) -> None:
    """Legs written on a day by a path that did not derive (a seed, a hand repair) — the next
    derivation fills it, whatever day that run itself fetched."""
    _seed_history(conn)
    gap = date(2026, 7, 20)
    upsert_fx(conn, _legs(gap), fetched_at=_T0)          # legs only, no derived row
    assert gap not in _derived(conn)
    _refresh(conn, _legs(_TODAY))
    assert _stamped(conn, _T1) == [gap, _TODAY]
    assert _derived(conn)[gap][1] == "derived:USD"


def test_a_repeat_run_writes_nothing(conn: sqlite3.Connection) -> None:
    _seed_history(conn)
    first = derive_cross_rates(conn, fetched_at=_T0)
    before = _derived(conn)
    again: list[FxRow] = []
    assert _changes(conn, lambda: again.extend(derive_cross_rates(conn, fetched_at=_T1))) == 0
    assert _derived(conn) == before
    assert again == first                     # the pair's rows are still returned, unchanged
    assert [r.as_of for r in again] == _HISTORY


# ------------------------------------------------------------------ why the scope is whole-history


def test_a_foreign_row_on_a_day_nobody_refetched_is_still_replaced(
    conn: sqlite3.Connection,
) -> None:
    """A pre-M1 database (prod, v0.1.28) holds a FETCHED MYR/TWD for every historical day. The
    first one-day refresh after the upgrade must still re-express all of them — and a row with
    the right number under the wrong source is not "unchanged" either."""
    upsert_fx(conn, [r for d in _HISTORY for r in _legs(d)], fetched_at=_T0)
    upsert_fx(conn, [FxRow(base=Currency.MYR, quote=Currency.TWD, as_of=d,
                           rate=Decimal("7.803300"), source="yfinance") for d in _HISTORY[1:]]
              + [FxRow(base=Currency.MYR, quote=Currency.TWD, as_of=_HISTORY[0],
                       rate=_usdtwd(_HISTORY[0]) / _usdmyr(_HISTORY[0]), source="yfinance")],
              fetched_at=_T0)
    _refresh(conn, _legs(_TODAY))
    rows = _derived(conn)
    assert {src for _, src, _ in rows.values()} == {"derived:USD"}
    assert _stamped(conn, _T1) == [*_HISTORY, _TODAY]


def test_every_day_still_closes_the_triangle_at_the_6dp_cap(conn: sqlite3.Connection) -> None:
    _seed_history(conn)
    _refresh(conn, _legs(_TODAY, twd=Decimal("31.834999"), myr=Decimal("4.082500")))
    for day, (rate, source, _) in _derived(conn).items():
        twd = Decimal("31.834999") if day == _TODAY else _usdtwd(day)
        myr = Decimal("4.082500") if day == _TODAY else _usdmyr(day)
        assert Decimal(rate) == (twd / myr).quantize(_Q6, rounding=ROUND_HALF_UP), day
        assert Decimal(rate) == Decimal(rate).quantize(_Q6)        # at most 6 dp
        assert source == "derived:USD"
        assert abs((myr * Decimal(rate)).quantize(_Q6, rounding=ROUND_HALF_UP) - twd) \
            <= Decimal("0.000005")
