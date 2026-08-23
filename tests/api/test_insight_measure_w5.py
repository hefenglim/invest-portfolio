"""W5 (AI-D22..D26) — the ``relative`` / ``volatility`` measurement seam.

The scorers were always written and tested (``tests/llm_insight/test_scoring.py``); what
the two v1 stubs never did is FEED them. This file pins the new measurement arms:

- ``_window_return`` / ``_vol_change_pct`` — the pure helpers, hand-checked;
- ``_benchmark_for_symbol`` — the fixed market map (AI-D22), MY/unknown → honest None;
- ``_measure_actual``'s relative arm — symbol leg vs the benchmark leg, both local-ccy
  (AI-D23), plus every degrade path landing as ``benchmark_return_pct=None``
  (pending_data, never a forced miss);
- ``_measure_actual``'s volatility arm — two fixed 30-day windows (AI-D24), and the proof
  that a SPLIT inside the window is re-expressed away rather than read as a vol spike.
"""

import sqlite3
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from portfolio_dash.api import insight_service
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.holdings import load_action_index
from portfolio_dash.data_ingestion.store import (
    insert_corporate_action,
    upsert_instrument,
)
from portfolio_dash.llm_insight import evaluations_store as es
from portfolio_dash.llm_insight.cards import Prediction
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from tests.conftest import init_golden_base

NOW = datetime(2026, 6, 30, 12, 0)
USD = Currency.USD
MYR = Currency.MYR
TWD = Currency.TWD


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    init_golden_base(c)
    seed_accounts(c)
    yield c
    c.close()


def _due(
    symbol: str, *, created: date, due: date, price_at_create: str | None = None
) -> es.DueInsight:
    return es.DueInsight(
        insight_id=1, insight_type_id=1, symbol=symbol, calibration_version=None,
        is_shadow=False, confidence=60,
        prediction='{"metric":"relative","direction":"up","horizon_days":30}',
        due_at=datetime.combine(due, datetime.min.time()).isoformat(),
        created_at=datetime.combine(created, datetime.min.time()).isoformat(),
        price_at_create=price_at_create,
    )


def _pred(metric: str, direction: str = "up") -> Prediction:
    return Prediction(
        metric=metric,  # type: ignore[arg-type]
        direction=direction,  # type: ignore[arg-type]
        horizon_days=30,
    )


def _register(conn: sqlite3.Connection, symbol: str, market: Market, ccy: Currency) -> None:
    upsert_instrument(conn, Instrument(symbol=symbol, market=market, quote_ccy=ccy,
                                       sector="Tech", name=symbol))


def _plant(
    conn: sqlite3.Connection, symbol: str, market: Market,
    points: list[tuple[date, Decimal]],
) -> None:
    upsert_prices(
        conn,
        [PriceRow(instrument=symbol, market=market, as_of=d, close=c, source="test")
         for d, c in points],
        fetched_at=NOW,
    )


def _daily_closes(
    start: date, days: int, first: Decimal, last: Decimal
) -> list[tuple[date, Decimal]]:
    """A straight-line ascending daily series from ``first`` to ``last`` (inclusive)."""
    if days < 2:
        return [(start, first)]
    step = (last - first) / Decimal(days - 1)
    return [(start + timedelta(days=i), first + step * i) for i in range(days)]


def _alternating(start: date, days: int, base: Decimal, pct: Decimal) -> list[tuple[date, Decimal]]:
    """Daily closes oscillating ±pct around an anchor — a deterministic vol regime."""
    out: list[tuple[date, Decimal]] = []
    for i in range(days):
        factor = (1 + pct) if i % 2 == 0 else (1 / (1 + pct))
        prev = out[-1][1] if out else base
        out.append((start + timedelta(days=i), prev * factor if i else base))
    return out


# --- _window_return (pure) -----------------------------------------------------


def test_window_return_uses_the_tolerant_legs() -> None:
    closes = [
        (date(2026, 6, 1), Decimal("100")),
        (date(2026, 6, 3), Decimal("101")),
        (date(2026, 6, 18), Decimal("108")),
        (date(2026, 6, 20), Decimal("110")),
    ]
    # created 6/2 has no close → first on/after = 6/3 @101; due 6/19 → last on/before = 6/18 @108.
    r = insight_service._window_return(closes, date(2026, 6, 2), date(2026, 6, 19))
    assert r == (Decimal("108") - Decimal("101")) / Decimal("101")


def test_window_return_degrades_on_missing_or_zero_legs() -> None:
    assert insight_service._window_return([], date(2026, 6, 1), date(2026, 6, 20)) is None
    # The only close sits beyond the +14d start tolerance.
    assert insight_service._window_return(
        [(date(2026, 7, 1), Decimal("100"))], date(2026, 6, 1), date(2026, 7, 1)
    ) is None
    # A zero start leg can never be a denominator.
    assert insight_service._window_return(
        [(date(2026, 6, 1), Decimal("0")), (date(2026, 6, 20), Decimal("1"))],
        date(2026, 6, 1), date(2026, 6, 20),
    ) is None


# --- _vol_change_pct (pure) ----------------------------------------------------


def test_vol_change_pct_measures_a_regime_shift() -> None:
    created, due = date(2026, 6, 1), date(2026, 6, 20)
    calm = _alternating(created - timedelta(days=40), 41, Decimal("100"), Decimal("0.001"))
    wild = _alternating(created + timedelta(days=1), 19, calm[-1][1], Decimal("0.05"))
    change = insight_service._vol_change_pct(calm + wild, created, due)
    assert change is not None
    assert change > Decimal("1")  # vol roughly 50× — the direction is the point


def test_vol_change_pct_degrades_on_a_short_or_flat_baseline() -> None:
    created, due = date(2026, 6, 1), date(2026, 6, 20)
    # Fewer than 31 closes on the create side → no baseline, honest None.
    short = _daily_closes(created - timedelta(days=10), 11, Decimal("100"), Decimal("100"))
    assert insight_service._vol_change_pct(short, created, due) is None
    # A perfectly constant baseline has vol exactly 0 — never a denominator.
    flat = [(created - timedelta(days=40 - i), Decimal("100")) for i in range(41)]
    flat += _alternating(created + timedelta(days=1), 19, Decimal("100"), Decimal("0.05"))
    assert insight_service._vol_change_pct(flat, created, due) is None


# --- _benchmark_for_symbol (AI-D22) --------------------------------------------


def test_benchmark_lookup_maps_us_and_tw_and_refuses_my(conn: sqlite3.Connection) -> None:
    _register(conn, "AAA", Market.US, USD)
    _register(conn, "2330", Market.TW, TWD)
    _register(conn, "1155", Market.MY, MYR)
    assert insight_service._benchmark_for_symbol(conn, "AAA").key == "sp500"  # type: ignore[union-attr]
    assert insight_service._benchmark_for_symbol(conn, "2330").key == "0050"  # type: ignore[union-attr]
    # MY has no wired benchmark — and an unregistered symbol has no market at all. Both
    # are the same honest None (AI-D22: no proxy, no guessed index).
    assert insight_service._benchmark_for_symbol(conn, "1155") is None
    assert insight_service._benchmark_for_symbol(conn, "NOPE") is None


# --- _measure_actual: the relative arm -----------------------------------------

CREATED = date(2026, 6, 1)
DUE = date(2026, 6, 20)


def test_relative_arm_measures_the_benchmark_leg(conn: sqlite3.Connection) -> None:
    _register(conn, "AAA", Market.US, USD)
    _plant(conn, "AAA", Market.US, _daily_closes(CREATED, 20, Decimal("100"), Decimal("110")))
    _plant(conn, "^GSPC", Market.US,
           _daily_closes(CREATED, 20, Decimal("5000"), Decimal("5100")))
    actual = insight_service._measure_actual(
        conn, _due("AAA", created=CREATED, due=DUE, price_at_create="100"),
        _pred("relative"), actions=load_action_index(conn), now=NOW, reporting=TWD,
    )
    assert actual is not None
    assert actual.symbol_return_pct == Decimal("0.1")
    assert actual.benchmark_return_pct == Decimal("0.02")


def test_relative_arm_tw_uses_0050(conn: sqlite3.Connection) -> None:
    _register(conn, "2330", Market.TW, TWD)
    _plant(conn, "2330", Market.TW,
           _daily_closes(CREATED, 20, Decimal("1000"), Decimal("1050")))
    _plant(conn, "0050", Market.TW, _daily_closes(CREATED, 20, Decimal("200"), Decimal("202")))
    actual = insight_service._measure_actual(
        conn, _due("2330", created=CREATED, due=DUE, price_at_create="1000"),
        _pred("relative"), actions=load_action_index(conn), now=NOW, reporting=TWD,
    )
    assert actual is not None
    assert actual.symbol_return_pct == Decimal("0.05")
    assert actual.benchmark_return_pct == Decimal("0.01")


def test_relative_arm_my_symbol_defers_honestly(conn: sqlite3.Connection) -> None:
    _register(conn, "1155", Market.MY, MYR)
    _plant(conn, "1155", Market.MY, _daily_closes(CREATED, 20, Decimal("10"), Decimal("11")))
    actual = insight_service._measure_actual(
        conn, _due("1155", created=CREATED, due=DUE, price_at_create="10"),
        _pred("relative"), actions=load_action_index(conn), now=NOW, reporting=TWD,
    )
    assert actual is not None
    assert actual.symbol_return_pct == Decimal("0.1")
    assert actual.benchmark_return_pct is None  # → pending_data, never a forced miss


def test_relative_arm_without_benchmark_prices_defers(conn: sqlite3.Connection) -> None:
    _register(conn, "AAA", Market.US, USD)
    _plant(conn, "AAA", Market.US, _daily_closes(CREATED, 20, Decimal("100"), Decimal("110")))
    actual = insight_service._measure_actual(
        conn, _due("AAA", created=CREATED, due=DUE, price_at_create="100"),
        _pred("relative"), actions=load_action_index(conn), now=NOW, reporting=TWD,
    )
    assert actual is not None
    assert actual.benchmark_return_pct is None  # series absent → pending_data


def test_a_held_benchmarks_own_split_is_not_a_benchmark_crash(
    conn: sqlite3.Connection,
) -> None:
    """The benchmark key "0050" may ALSO be a held instrument with a recorded SPLIT
    (benchmarks.py's collision note). Raw, a mid-window 2-for-1 reads as a −50% benchmark
    return — a fabricated +50% excess for every TW relative card maturing that week. The
    leg rides the same ``series_in`` seam as the performance router's benchmark read."""
    _register(conn, "2330", Market.TW, TWD)
    _register(conn, "0050", Market.TW, TWD)
    insert_corporate_action(
        conn, account_id="schwab", action_date=CREATED + timedelta(days=5),
        kind=CorporateActionKind.SPLIT, from_symbol="0050", to_symbol="0050",
        ratio_to=Decimal("2"), ratio_from=Decimal("1"),
    )
    _plant(conn, "2330", Market.TW,
           _daily_closes(CREATED, 20, Decimal("1000"), Decimal("1000")))
    bench = ([(CREATED + timedelta(days=i), Decimal("200")) for i in range(5)]
             + [(CREATED + timedelta(days=i), Decimal("100")) for i in range(5, 20)])
    _plant(conn, "0050", Market.TW, bench)
    actual = insight_service._measure_actual(
        conn, _due("2330", created=CREATED, due=DUE, price_at_create="1000"),
        _pred("relative"), actions=load_action_index(conn), now=NOW, reporting=TWD,
    )
    assert actual is not None
    assert actual.symbol_return_pct == Decimal("0")
    assert actual.benchmark_return_pct == Decimal("0")  # re-expressed: 200→100 pre-split


# --- _measure_actual: the volatility arm ----------------------------------------


def _vol_fixture(conn: sqlite3.Connection, symbol: str = "AAA") -> None:
    """Calm-then-wild close series long enough for both 30-day windows."""
    _register(conn, symbol, Market.US, USD)
    calm = _alternating(CREATED - timedelta(days=40), 41, Decimal("100"), Decimal("0.001"))
    wild = _alternating(CREATED + timedelta(days=1), 19, calm[-1][1], Decimal("0.05"))
    _plant(conn, symbol, Market.US, calm + wild)


def test_volatility_arm_measures_the_regime_shift(conn: sqlite3.Connection) -> None:
    _vol_fixture(conn)
    actual = insight_service._measure_actual(
        conn, _due("AAA", created=CREATED, due=DUE), _pred("volatility"),
        actions=load_action_index(conn), now=NOW, reporting=TWD,
    )
    assert actual is not None
    assert actual.vol_change_pct is not None
    assert actual.vol_change_pct > Decimal("1")


def test_volatility_arm_defers_when_history_is_short(conn: sqlite3.Connection) -> None:
    _register(conn, "AAA", Market.US, USD)
    _plant(conn, "AAA", Market.US, _daily_closes(CREATED, 20, Decimal("100"), Decimal("110")))
    actual = insight_service._measure_actual(
        conn, _due("AAA", created=CREATED, due=DUE), _pred("volatility"),
        actions=load_action_index(conn), now=NOW, reporting=TWD,
    )
    assert actual is not None
    assert actual.vol_change_pct is None  # 20 closes < 31 needed → pending_data


def _split_vol_fixture(conn: sqlite3.Connection, symbol: str = "SPLT") -> None:
    """A calm ±0.1% close series through a mid-window 7-for-1 (as traded 700 → 100).

    The split sits at created+5 so the raw −86% jump lands INSIDE the due window but NOT
    the create window — the exact shape that fabricates a "vol regime shift" if the series
    is not re-expressed.
    """
    _register(conn, symbol, Market.US, USD)
    insert_corporate_action(
        conn, account_id="schwab", action_date=CREATED + timedelta(days=5),
        kind=CorporateActionKind.SPLIT, from_symbol=symbol, to_symbol=symbol,
        ratio_to=Decimal("7"), ratio_from=Decimal("1"),
    )
    pre = _alternating(CREATED - timedelta(days=40), 45, Decimal("700"), Decimal("0.001"))
    post = _alternating(CREATED + timedelta(days=5), 15, Decimal("100"), Decimal("0.001"))
    _plant(conn, symbol, Market.US, pre + post)


def test_a_split_inside_the_vol_window_is_not_a_regime_shift(
    conn: sqlite3.Connection,
) -> None:
    """Re-expressed (the shipped path), both windows are the SAME calm regime — a 7-for-1
    inside the measurement window must not read as a vol spike (and thereby a fabricated
    hit for a "vol up" call on the model's permanent record)."""
    _split_vol_fixture(conn)
    actual = insight_service._measure_actual(
        conn, _due("SPLT", created=CREATED, due=DUE), _pred("volatility"),
        actions=load_action_index(conn), now=NOW, reporting=TWD,
    )
    assert actual is not None
    assert actual.vol_change_pct is not None
    assert abs(actual.vol_change_pct) < Decimal("0.5")


def test_without_re_expression_the_split_would_measure_a_spike(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The disproof: neuter ``series_in`` (the pre-W6c statement) and the SAME fixture
    measures a giant vol change — the fabrication the arm must never ship."""
    _split_vol_fixture(conn)
    monkeypatch.setattr(
        insight_service, "series_in",
        lambda index, sym, points, *, valued_on: list(points),
    )
    actual = insight_service._measure_actual(
        conn, _due("SPLT", created=CREATED, due=DUE), _pred("volatility"),
        actions=load_action_index(conn), now=NOW, reporting=TWD,
    )
    assert actual is not None
    assert actual.vol_change_pct is not None
    assert actual.vol_change_pct > Decimal("1")
