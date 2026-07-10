"""Pure transition-detection + store round-trip for the signal_states derived cache.

The scan ORCHESTRATION (which also writes alert_events) is covered by
tests/contract/test_signal_scan.py; here we pin the PURE logic and the conn-bearing store.
"""

import sqlite3
from decimal import Decimal

from portfolio_dash.strategy import signal_states as ss
from portfolio_dash.strategy.rules import engine
from portfolio_dash.strategy.rules.params import PARAMS_VERSION, default_params
from portfolio_dash.strategy.signal_states import (
    EVENT_CROSS,
    EVENT_MOMENTUM,
    EVENT_TREND,
    DerivedState,
    detect_transitions,
)


def _state(
    trend: str | None = None,
    cross: str | None = None,
    days_ago: int | None = None,
    momentum: str | None = None,
) -> DerivedState:
    return DerivedState(
        trend_state=trend, cross_state=cross, cross_days_ago=days_ago,
        momentum_state=momentum, tech_score=None, evaluation_context=None,
    )


# --- trend ---------------------------------------------------------------------


def test_trend_confirmed_direction_change_fires() -> None:
    assert detect_transitions(_state(trend="above_confirmed"),
                              _state(trend="below_confirmed")) == [EVENT_TREND]


def test_trend_enter_confirmed_from_neutral_fires() -> None:
    assert EVENT_TREND in detect_transitions(_state(trend="in_band"),
                                             _state(trend="above_confirmed"))


def test_trend_unconfirmed_flapping_is_silent() -> None:
    # above_unconfirmed and in_band both read neutral → no confirmed-direction change.
    assert detect_transitions(_state(trend="in_band"),
                              _state(trend="above_unconfirmed")) == []


def test_trend_same_confirmed_state_silent() -> None:
    assert detect_transitions(_state(trend="above_confirmed"),
                              _state(trend="above_confirmed")) == []


# --- cross ---------------------------------------------------------------------


def test_cross_fresh_from_relationship_fires() -> None:
    assert detect_transitions(_state(cross="fast_above"),
                              _state(cross="golden", days_ago=2)) == [EVENT_CROSS]


def test_cross_ageing_is_silent() -> None:
    assert detect_transitions(_state(cross="golden", days_ago=3),
                              _state(cross="golden", days_ago=4)) == []


def test_cross_newer_same_type_fires() -> None:
    assert detect_transitions(_state(cross="golden", days_ago=5),
                              _state(cross="golden", days_ago=0)) == [EVENT_CROSS]


def test_cross_flip_golden_to_death_fires() -> None:
    assert detect_transitions(_state(cross="golden", days_ago=5),
                              _state(cross="death", days_ago=1)) == [EVENT_CROSS]


def test_cross_aged_out_to_relationship_silent() -> None:
    # golden → fast_above (cross aged past the lookback) is NOT a new cross.
    assert detect_transitions(_state(cross="golden", days_ago=5),
                              _state(cross="fast_above")) == []


# --- momentum ------------------------------------------------------------------


def test_momentum_sign_flip_fires() -> None:
    assert detect_transitions(_state(momentum="positive"),
                              _state(momentum="negative")) == [EVENT_MOMENTUM]


def test_momentum_into_flat_is_silent() -> None:
    assert detect_transitions(_state(momentum="positive"),
                              _state(momentum="flat")) == []


def test_momentum_unmeasured_is_silent() -> None:
    assert detect_transitions(_state(momentum="positive"),
                              _state(momentum=None)) == []


def test_no_change_no_events() -> None:
    s = _state(trend="above_confirmed", cross="golden", days_ago=2, momentum="positive")
    assert detect_transitions(s, s) == []


def test_multiple_transitions_in_one_step() -> None:
    events = detect_transitions(
        _state(trend="above_confirmed", cross="fast_above", momentum="positive"),
        _state(trend="below_confirmed", cross="death", days_ago=1, momentum="negative"),
    )
    assert set(events) == {EVENT_TREND, EVENT_CROSS, EVENT_MOMENTUM}


# --- extract_state -------------------------------------------------------------


def test_extract_state_none_is_all_none() -> None:
    d = ss.extract_state(None)
    assert d == DerivedState(None, None, None, None, None, None)


def test_extract_state_thin_series_degrades_honestly() -> None:
    # A single close: every rule needs far more history → all None, composite None.
    sig = engine.evaluate_symbol([Decimal("100")], None, default_params())
    d = ss.extract_state(sig)
    assert d.trend_state is None and d.cross_state is None
    assert d.momentum_state is None and d.tech_score is None


def test_extract_state_full_series() -> None:
    closes = [Decimal(100) + Decimal(i) * Decimal("2") for i in range(300)]
    sig = engine.evaluate_symbol(closes, None, default_params())
    d = ss.extract_state(sig)
    assert d.trend_state == "above_confirmed"
    assert d.momentum_state == "positive"
    assert d.evaluation_context is not None
    assert d.tech_score is not None


# --- store round-trip ----------------------------------------------------------


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ss.ensure_table(c)
    return c


def test_store_upsert_get_roundtrip() -> None:
    c = _conn()
    d = DerivedState("above_confirmed", "golden", 3, "positive",
                     Decimal("77.5"), "strong_uptrend")
    ss.upsert_state(c, "2330", d, params_version=PARAMS_VERSION,
                    as_of="2026-06-11", updated_at="2026-06-11T14:30:00+08:00")
    got = ss.get_state(c, "2330")
    assert got is not None
    assert got.symbol == "2330"
    assert got.derived == d
    assert got.params_version == PARAMS_VERSION


def test_store_upsert_replaces_not_duplicates() -> None:
    c = _conn()
    d1 = DerivedState("above_confirmed", None, None, "positive", Decimal("70"), "x")
    d2 = DerivedState("below_confirmed", None, None, "negative", Decimal("30"), "y")
    for d in (d1, d2):
        ss.upsert_state(c, "2330", d, params_version=PARAMS_VERSION,
                        as_of="2026-06-11", updated_at="t")
    rows = ss.all_states(c)
    assert len(rows) == 1 and rows[0].derived == d2


def test_store_get_absent_is_none() -> None:
    assert ss.get_state(_conn(), "NOPE") is None


def test_store_clear_all() -> None:
    c = _conn()
    ss.upsert_state(c, "2330", _state(trend="above_confirmed"),
                    params_version=PARAMS_VERSION, as_of="d", updated_at="t")
    ss.clear_all(c)
    assert ss.all_states(c) == []
