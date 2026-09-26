"""DEF-077 (owner ruling ⑥ A, 2026-09-26): the FX triangle is judged on the LATEST COMMON
date of its three legs, and legs with different latest dates are labelled 「日期不同」 — not a
red light.

Measured by the verifier on 3be67db (J-01): USD/TWD had already been refreshed to a new day
while USD/MYR had not, so the check compared USD/MYR × MYR/TWD = 31.799144 against USD/TWD
31.829000 — two different days — and reported gap −0.0938%, ``ok: false``, with no data error
anywhere. Since the M1 ruling MYR/TWD is DERIVED as USD/TWD ÷ USD/MYR (``pricing/cross.py``),
so on any day all three exist the triangle closes to within the 6-dp cap by construction;
comparing latest rows across days measured the market's move between the days, not the data.

The four cases of the spec, plus the wire shape and the whole path through ``build_dashboard``
(where the resolver's direction choice — direct or inverted — must be the one the history is
read in):

(a) same date, consistent       → ok
(b) same date, inconsistent     → false (the guard still fires)
(c) different latest dates, consistent on the common date → ok + ``dates_differ``
(d) no common date in the lookback → cannot compare (``ok: null`` + a reason), never false
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal

from portfolio_dash.portfolio.dashboard import RateResolver, _fx_triangulation, build_dashboard
from portfolio_dash.portfolio.dashboard_models import FreshnessReport, FxTriangle
from portfolio_dash.pricing.results import FxRead, FxRow
from portfolio_dash.pricing.schema import create_tables
from portfolio_dash.pricing.store import upsert_fx
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.wire import to_wire
from tests.conftest import GOLDEN_NOW

_USD, _MYR, _TWD = Currency.USD, Currency.MYR, Currency.TWD
_D1 = date(2026, 9, 24)
_D2 = date(2026, 9, 25)
_VALUED = date(2026, 9, 26)

_Series = dict[tuple[Currency, Currency], dict[date, str]]
_History = Callable[[Currency, Currency, date, date], list[FxRead]]


def _read(rate: str, on: date) -> FxRead:
    return FxRead(rate=Decimal(rate), as_of=on, source="test", stale=False)


def _setup(series: _Series) -> tuple[dict[tuple[Currency, Currency], FxRead | None], _History]:
    """Latest read per pair + a history callable over the same stored series."""
    reads: dict[tuple[Currency, Currency], FxRead | None] = {}
    for pair, rows in series.items():
        last = max(rows)
        reads[pair] = _read(rows[last], last)

    def history(base: Currency, quote: Currency, start: date, end: date) -> list[FxRead]:
        rows = series.get((base, quote), {})
        return [_read(rows[d], d) for d in sorted(rows) if start <= d <= end]

    return reads, history


def _one(series: _Series, *, valued_on: date = _VALUED) -> FxTriangle:
    reads, history = _setup(series)
    tris = _fx_triangulation(reads, history=history, valued_on=valued_on)
    assert len(tris) == 1, tris
    return tris[0]


def test_a_same_date_consistent_triangle_is_ok() -> None:
    t = _one({(_USD, _TWD): {_D2: "32"}, (_USD, _MYR): {_D2: "4"}, (_MYR, _TWD): {_D2: "8"}})
    assert t.ok is True and t.dates_differ is False
    assert t.compared_on == _D2 and t.gap_pct == Decimal("0.0000")


def test_b_same_date_inconsistent_triangle_is_false() -> None:
    t = _one({(_USD, _TWD): {_D2: "32"}, (_USD, _MYR): {_D2: "4"}, (_MYR, _TWD): {_D2: "8.1"}})
    assert t.ok is False and t.dates_differ is False
    assert t.gap_pct == Decimal("1.2500")


def test_c_different_latest_dates_are_compared_on_the_common_date() -> None:
    """The verifier's J-01 shape: USD/TWD already on the new day, USD/MYR (and the derived
    MYR/TWD) still on the previous one. The previous day closes exactly; the latest rows
    across the two days do not (32.5 vs 4 × 8 = 32 → −1.5385%)."""
    t = _one({
        (_USD, _TWD): {_D1: "32", _D2: "32.5"},
        (_USD, _MYR): {_D1: "4"},
        (_MYR, _TWD): {_D1: "8"},
    })
    assert t.ok is True, f"judged on the common date {_D1}, the triangle closes: {t}"
    assert t.dates_differ is True
    assert t.compared_on == _D1
    assert t.implied == Decimal("32.000000") and t.direct == Decimal("32.000000")
    assert t.leg_dates == {"USD/MYR": _D1, "MYR/TWD": _D1, "USD/TWD": _D2}
    assert t.reason is None


def test_c_a_real_gap_on_the_common_date_is_still_reported() -> None:
    """Different dates do not excuse a genuine disagreement ON the common date."""
    t = _one({
        (_USD, _TWD): {_D1: "32", _D2: "32.5"},
        (_USD, _MYR): {_D1: "4"},
        (_MYR, _TWD): {_D1: "8.1"},
    })
    assert t.ok is False and t.dates_differ is True and t.compared_on == _D1


def test_d_no_common_date_in_the_lookback_cannot_be_compared() -> None:
    t = _one({
        (_USD, _TWD): {_D2: "32"},
        (_USD, _MYR): {_D1: "4"},
        (_MYR, _TWD): {_D1 - timedelta(days=1): "8"},
    })
    assert t.ok is None, "no common date is 「無法比較」, never a red light"
    assert t.dates_differ is True and t.compared_on is None
    assert t.implied is None and t.direct is None and t.gap_pct is None
    assert t.reason is not None and "無法比較" in t.reason


def test_a_row_after_the_valuation_day_is_not_the_comparison_day() -> None:
    """「不晚於估值日」: a leg row dated after the valuation day is not compared on."""
    t = _one({
        (_USD, _TWD): {_D1: "32", _D2: "40"},
        (_USD, _MYR): {_D1: "4", _D2: "4"},
        (_MYR, _TWD): {_D1: "8", _D2: "8"},
    }, valued_on=_D1)
    assert t.compared_on == _D1 and t.ok is True


def test_wire_carries_the_new_fields_as_strings_and_null() -> None:
    reads, history = _setup({
        (_USD, _TWD): {_D2: "32"}, (_USD, _MYR): {_D1: "4"}, (_MYR, _TWD): {_D1: "8"}})
    report = FreshnessReport(prices=[], fx=[], any_stale=False, missing_prices=[],
                             missing_fx=[],
                             fx_triangulation=_fx_triangulation(reads, history=history,
                                                                valued_on=_VALUED))
    [tri] = to_wire(report.model_dump())["fx_triangulation"]
    assert tri["ok"] is None and tri["gap_pct"] is None and tri["compared_on"] is None
    assert tri["dates_differ"] is True
    assert tri["leg_dates"] == {"USD/MYR": "2026-09-24", "MYR/TWD": "2026-09-24",
                                "USD/TWD": "2026-09-25"}


def test_through_the_dashboard_the_common_date_decides(golden_db: sqlite3.Connection) -> None:
    """The whole path: stored rows → resolver reads → history in the resolver's direction.

    The golden ledger reads all three pairs on 2026-06-09. Make that day close exactly
    (4.4 × 7 = 30.8) and move USD/TWD alone to 2026-06-10: before DEF-077 the check compared
    the 06-10 USD/TWD with the 06-09 legs and reported a false inconsistency.
    """
    upsert_fx(golden_db, [
        FxRow(base=_USD, quote=_TWD, as_of=date(2026, 6, 9), rate=Decimal("30.8"),
              source="test"),
        FxRow(base=_USD, quote=_TWD, as_of=date(2026, 6, 10), rate=Decimal("31.5"),
              source="test"),
    ], fetched_at=GOLDEN_NOW)
    golden_db.commit()
    data = build_dashboard(golden_db, now=GOLDEN_NOW, reporting=_TWD)
    tris = [t for t in data.freshness.fx_triangulation if t.pair == "USD/TWD"]
    assert tris, data.freshness.fx_triangulation
    t = tris[0]
    assert t.ok is True, t
    assert t.dates_differ is True and t.compared_on == date(2026, 6, 9)


def test_the_history_is_read_in_the_direction_the_rate_was_resolved() -> None:
    """A pair stored only as its inverse is inverted for the history exactly as for the read
    (``1 / rate``), so a leg answered from MYR/USD is compared as USD/MYR on the common day."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_tables(conn)
    upsert_fx(conn, [
        FxRow(base=_MYR, quote=_USD, as_of=_D1, rate=Decimal("0.25"), source="test"),
        FxRow(base=_USD, quote=_TWD, as_of=_D2, rate=Decimal("32"), source="test"),
    ], fetched_at=GOLDEN_NOW)
    resolver = RateResolver(conn, now=GOLDEN_NOW)
    assert resolver.rate(_USD, _MYR) == Decimal("4")
    [row] = resolver.history(_USD, _MYR, _D1, _D2)
    assert row.as_of == _D1 and row.rate == Decimal("4")
    assert [r.rate for r in resolver.history(_USD, _TWD, _D1, _D2)] == [Decimal("32")]
