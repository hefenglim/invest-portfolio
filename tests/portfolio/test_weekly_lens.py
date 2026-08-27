"""W8's second half as a READ-ONLY LENS (owner ruling 2026-08-28).

Every rule and indicator in this app reads DAILY closes. A weekly view is the one thing a
long-term reader asks for that the data cannot currently answer — 「日線轉弱，但週線還沒破」 is
a sentence the assistant has no way to form.

Scope is deliberately the small half of W8: resample at READ time, expose ONE per-symbol
variable, and touch nothing that persists. In particular `signal_history`'s primary key stays
``(symbol, as_of)``, `TechScore`, `alert_events` and the event-study backtest are untouched,
and there is no second `PARAMS_VERSION`. A full second timeframe needs a timeframe dimension
in that key; adding the lens without it is safe precisely because the lens stores nothing.

⚠ The trap this file exists to prevent is AI-D2's, one timeframe over: 「均線交叉」 once meant
20/60 in one place and 50/200 in another, and the same prompt cited both. So every weekly
figure is named in WEEKS and the payload states its own timeframe — a reader (human or model)
must never have to guess which one a number is in.
"""

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, cast

from portfolio_dash.portfolio import technicals

if TYPE_CHECKING:
    from portfolio_dash.llm_insight.variables import VarContext

D = Decimal


def _pt(y: int, m: int, d: int, close: str) -> dict[str, object]:
    return {"date": date(y, m, d), "close": D(close)}


def test_a_week_is_represented_by_its_LAST_close() -> None:
    """Mon-Fri of one ISO week collapses to Friday's close — the week's settled value."""
    pts = [_pt(2026, 6, 1, "10"), _pt(2026, 6, 2, "11"), _pt(2026, 6, 3, "12"),
           _pt(2026, 6, 4, "13"), _pt(2026, 6, 5, "14")]
    assert technicals.resample_weekly(pts) == [D("14")]


def test_weeks_are_split_on_the_ISO_boundary_not_every_five_rows() -> None:
    """The reason a positional 「every 5th close」 is wrong: holidays.

    This span has a FOUR-session week followed by a five-session week. Chunking by position
    would put Monday of week 2 into week 1 and drift further with every holiday in the year.
    """
    pts = [
        # ISO week 23: Tue-Fri only (Monday was a holiday)
        _pt(2026, 6, 2, "11"), _pt(2026, 6, 3, "12"), _pt(2026, 6, 4, "13"),
        _pt(2026, 6, 5, "14"),
        # ISO week 24: Mon-Fri
        _pt(2026, 6, 8, "20"), _pt(2026, 6, 9, "21"), _pt(2026, 6, 10, "22"),
        _pt(2026, 6, 11, "23"), _pt(2026, 6, 12, "24"),
    ]
    assert technicals.resample_weekly(pts) == [D("14"), D("24")]


def test_it_is_chronological_regardless_of_input_order() -> None:
    pts = [_pt(2026, 6, 12, "24"), _pt(2026, 6, 5, "14"), _pt(2026, 6, 8, "20")]
    assert technicals.resample_weekly(pts) == [D("14"), D("24")]


def test_an_empty_or_malformed_series_yields_nothing_rather_than_guessing() -> None:
    assert technicals.resample_weekly([]) == []
    assert technicals.resample_weekly([{"close": D("1")}]) == []          # no date
    assert technicals.resample_weekly([{"date": date(2026, 6, 1)}]) == []  # no close


def test_a_year_of_daily_closes_becomes_about_fifty_two_weeks() -> None:
    """Sanity on real scale: 5 sessions a week for 52 weeks is ~52 points, not ~260."""
    pts = []
    d = date(2026, 1, 5)                     # a Monday
    for w in range(52):
        for offset in range(5):
            pts.append({"date": date.fromordinal(d.toordinal() + w * 7 + offset),
                        "close": D(100 + w)})
    weekly = technicals.resample_weekly(pts)
    assert len(weekly) == 52
    assert weekly[0] == D("100") and weekly[-1] == D("151")


# --- the variable the lens exists to feed ---------------------------------------------


def _ctx_with(points: list[dict[str, object]]) -> "VarContext":
    """A stand-in carrying the ONE field the producer reads.

    A real ``VarContext`` needs a whole ``DashboardData``; the lens reads only
    ``price_points``, so building one would test the fixture rather than the lens. The cast
    states that narrowness instead of hiding it behind ``object``.
    """
    from unittest.mock import Mock
    ctx = Mock()
    ctx.price_points = points
    return cast("VarContext", ctx)


def test_the_variable_names_every_number_in_weeks_and_says_its_timeframe() -> None:
    """AI-D2's lesson applied before it can bite: a number whose timeframe is implicit is a
    number the next reader — human or model — will compare against the wrong thing."""
    from portfolio_dash.llm_insight.variables import _weekly_signals

    pts = []
    d = date(2026, 1, 5)
    for w in range(60):                       # 60 clean weeks, rising 100 → 159
        for offset in range(5):
            pts.append({"date": date.fromordinal(d.toordinal() + w * 7 + offset),
                        "close": D(100 + w)})
    out = _weekly_signals(_ctx_with(pts))

    assert out["timeframe"] == "weekly"
    assert out["weeks"] == 60
    assert out["last_close"] == D("159")
    # Every key carries its unit; nothing is named like a daily figure.
    for key in ("ma10w", "ma20w", "ma40w", "price_vs_ma10w", "return_13w", "return_52w"):
        assert key in out, key
    # Hand-computed: the last 10 weekly closes are 150…159, mean 154.5.
    assert out["ma10w"] == D("154.5")
    # 13 weeks back from 159 is 146; (159 - 146) / 146.
    assert out["return_13w"] == (D("159") - D("146")) / D("146")


def test_a_window_with_too_little_history_is_null_not_estimated() -> None:
    from portfolio_dash.llm_insight.variables import _weekly_signals

    pts = []
    d = date(2026, 1, 5)
    for w in range(12):                       # 12 weeks: enough for 10w, not for 20w/40w/13w
        pts.append({"date": date.fromordinal(d.toordinal() + w * 7 + 4), "close": D(100 + w)})
    out = _weekly_signals(_ctx_with(pts))
    assert out["weeks"] == 12
    assert out["ma10w"] is not None
    assert out["ma20w"] is None and out["ma40w"] is None
    assert out["return_13w"] is None and out["return_52w"] is None


def test_an_unusable_series_degrades_rather_than_returning_a_shape_full_of_nulls() -> None:
    from portfolio_dash.llm_insight.variables import _weekly_signals

    assert _weekly_signals(_ctx_with([])) == {"unavailable": True}
    one_week = [{"date": date(2026, 6, 1), "close": D("10")}]
    assert _weekly_signals(_ctx_with(one_week)) == {"unavailable": True}
