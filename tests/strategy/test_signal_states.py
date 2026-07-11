"""Pure transition-detection + store round-trip for the signal_states derived cache.

The scan ORCHESTRATION (which also writes alert_events) is covered by
tests/contract/test_signal_scan.py; here we pin the PURE logic and the conn-bearing store.

Transition semantics follow the deep review 2026-07-10 rulings (HOLD semantics for the
trend + momentum direction rules): a neutral / flat / unmeasured scan HOLDS the last
non-neutral direction/sign rather than resetting it, so a reversal that dwells in the
dead-band still fires exactly once, and a mere drop into neutral (or a data gap) is silent.
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
    HoldState,
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


def _hold(trend_dir: str | None = None, sign: str | None = None) -> HoldState:
    return HoldState(trend_last_dir=trend_dir, momentum_last_sign=sign)


def _fire(stored: DerivedState, new: DerivedState, hold: HoldState) -> list[str]:
    return detect_transitions(stored, new, hold).events


def _run_sequence(
    states: list[DerivedState], initial: HoldState
) -> list[list[str]]:
    """Thread the hold state through a sequence of ``new`` scans (as the scan job does),
    returning the per-step event lists. ``stored`` (used only by the cross rule) is the
    prior state; trend/momentum here carry no cross so it is inert."""
    hold = initial
    prev = _state()
    stepwise: list[list[str]] = []
    for s in states:
        result = detect_transitions(prev, s, hold)
        stepwise.append(result.events)
        hold, prev = result.hold, s
    return stepwise


# --- trend (HOLD semantics; deep review 2026-07-10) ----------------------------


def test_trend_confirmed_direction_change_fires() -> None:
    # up (held) → below_confirmed = down → a genuine reversal fires.
    assert _fire(_state(), _state(trend="below_confirmed"), _hold(trend_dir="up")) \
        == [EVENT_TREND]


def test_trend_none_to_confirmed_is_silent() -> None:
    # deep review 2026-07-10: None→confirmed (no prior direction) is a data-availability
    # event, not a market reversal → SILENT (was: fired under the old raw-state semantics).
    assert _fire(_state(trend="in_band"), _state(trend="above_confirmed"), _hold()) == []


def test_trend_reconfirm_same_direction_through_neutral_silent() -> None:
    # up held, dipped to neutral, re-confirms up → same direction → SILENT (the whipsaw the
    # confirm_days band already suppresses; old semantics emitted 2 events here).
    assert _fire(_state(trend="in_band"), _state(trend="above_confirmed"),
                 _hold(trend_dir="up")) == []


def test_trend_into_neutral_is_silent_and_holds() -> None:
    # confirmed→neutral is SILENT and HOLDS the remembered direction (old semantics fired).
    result = detect_transitions(
        _state(trend="above_confirmed"), _state(trend="in_band"), _hold(trend_dir="up")
    )
    assert result.events == []
    assert result.hold.trend_last_dir == "up"  # neutral HOLDS, never resets


def test_trend_unconfirmed_flapping_is_silent() -> None:
    # above_unconfirmed reads neutral → no confirmed-direction change, held dir unchanged.
    assert _fire(_state(trend="in_band"), _state(trend="above_unconfirmed"),
                 _hold(trend_dir="down")) == []


def test_trend_sequence_dip_and_recover_zero_events() -> None:
    # above_confirmed → in_band → above_confirmed: the band dip holds 'up', re-confirming up
    # is the same direction → ZERO events across the whole sequence.
    steps = _run_sequence(
        [_state(trend="above_confirmed"), _state(trend="in_band"),
         _state(trend="above_confirmed")],
        _hold(),
    )
    assert steps == [[], [], []]


def test_trend_sequence_dip_then_reverse_fires_once() -> None:
    # above_confirmed → in_band → below_confirmed: reversal through neutral fires EXACTLY once.
    steps = _run_sequence(
        [_state(trend="above_confirmed"), _state(trend="in_band"),
         _state(trend="below_confirmed")],
        _hold(),
    )
    assert steps == [[], [], [EVENT_TREND]]


# --- cross ---------------------------------------------------------------------


def test_cross_fresh_from_relationship_fires() -> None:
    assert _fire(_state(cross="fast_above"),
                 _state(cross="golden", days_ago=2), _hold()) == [EVENT_CROSS]


def test_cross_ageing_is_silent() -> None:
    assert _fire(_state(cross="golden", days_ago=3),
                 _state(cross="golden", days_ago=4), _hold()) == []


def test_cross_newer_same_type_fires() -> None:
    assert _fire(_state(cross="golden", days_ago=5),
                 _state(cross="golden", days_ago=0), _hold()) == [EVENT_CROSS]


def test_cross_flip_golden_to_death_fires() -> None:
    assert _fire(_state(cross="golden", days_ago=5),
                 _state(cross="death", days_ago=1), _hold()) == [EVENT_CROSS]


def test_cross_aged_out_to_relationship_silent() -> None:
    # golden → fast_above (cross aged past the lookback) is NOT a new cross.
    assert _fire(_state(cross="golden", days_ago=5),
                 _state(cross="fast_above"), _hold()) == []


def test_cross_null_days_ago_same_type_does_not_refire() -> None:
    # F5 (deep review 2026-07-10): a legacy/hand-edited row with a NULL cross_days_ago must
    # NOT re-fire every scan — the smaller-days_ago clause requires BOTH values present.
    assert _fire(_state(cross="golden", days_ago=None),
                 _state(cross="golden", days_ago=3), _hold()) == []
    # symmetric: a NULL on the NEW side (same state) is likewise silent.
    assert _fire(_state(cross="golden", days_ago=5),
                 _state(cross="golden", days_ago=None), _hold()) == []
    # but a genuine STATE change still fires even with a NULL days_ago present.
    assert _fire(_state(cross="golden", days_ago=None),
                 _state(cross="death", days_ago=1), _hold()) == [EVENT_CROSS]


# --- momentum (HOLD semantics; deep review 2026-07-10) -------------------------


def test_momentum_sign_flip_fires() -> None:
    assert _fire(_state(), _state(momentum="negative"),
                 _hold(sign="positive")) == [EVENT_MOMENTUM]


def test_momentum_into_flat_is_silent_and_holds() -> None:
    # positive→flat is SILENT and HOLDS the sign (old semantics: silent but reset the sign,
    # which made the subsequent flat→negative reversal unreachable).
    result = detect_transitions(_state(), _state(momentum="flat"), _hold(sign="positive"))
    assert result.events == []
    assert result.hold.momentum_last_sign == "positive"  # flat HOLDS the sign


def test_momentum_unmeasured_is_silent_and_holds() -> None:
    result = detect_transitions(_state(), _state(momentum=None), _hold(sign="positive"))
    assert result.events == []
    assert result.hold.momentum_last_sign == "positive"


def test_momentum_sequence_positive_flat_negative_fires_once() -> None:
    # positive → flat → negative across three scans fires EXACTLY once (on the negative scan);
    # the flat dwell holds 'positive' rather than resetting it (the real gap the old code had).
    steps = _run_sequence(
        [_state(momentum="positive"), _state(momentum="flat"), _state(momentum="negative")],
        _hold(),
    )
    assert steps == [[], [], [EVENT_MOMENTUM]]


def test_momentum_flat_then_positive_no_prior_sign_silent() -> None:
    # flat → positive with no prior sign is silent (no sign to reverse from).
    steps = _run_sequence(
        [_state(momentum="flat"), _state(momentum="positive")], _hold()
    )
    assert steps == [[], []]


def test_momentum_sequence_positive_flat_positive_silent() -> None:
    steps = _run_sequence(
        [_state(momentum="positive"), _state(momentum="flat"), _state(momentum="positive")],
        _hold(),
    )
    assert steps == [[], [], []]


# --- combined / hold bookkeeping -----------------------------------------------


def test_no_change_no_events() -> None:
    s = _state(trend="above_confirmed", cross="golden", days_ago=2, momentum="positive")
    assert detect_transitions(s, s, _hold(trend_dir="up", sign="positive")).events == []


def test_multiple_transitions_in_one_step() -> None:
    events = detect_transitions(
        _state(trend="above_confirmed", cross="fast_above", momentum="positive"),
        _state(trend="below_confirmed", cross="death", days_ago=1, momentum="negative"),
        _hold(trend_dir="up", sign="positive"),
    ).events
    assert set(events) == {EVENT_TREND, EVENT_CROSS, EVENT_MOMENTUM}


def test_hold_carried_forward_on_neutral_scan() -> None:
    res = detect_transitions(
        _state(), _state(trend="in_band", momentum="flat"),
        _hold(trend_dir="down", sign="negative"),
    )
    assert res.events == []
    assert res.hold == _hold(trend_dir="down", sign="negative")


def test_hold_updates_on_confirmed_scan() -> None:
    res = detect_transitions(
        _state(), _state(trend="above_confirmed", momentum="positive"), _hold()
    )
    assert res.hold == _hold(trend_dir="up", sign="positive")


def test_seed_hold_resets_from_evaluation() -> None:
    assert ss.seed_hold(_state(trend="above_confirmed", momentum="positive")) \
        == HoldState("up", "positive")
    assert ss.seed_hold(_state(trend="below_confirmed", momentum="negative")) \
        == HoldState("down", "negative")
    # neutral / flat / unmeasured seed to NULL hold (silent, no direction remembered).
    assert ss.seed_hold(_state(trend="in_band", momentum="flat")) == HoldState(None, None)
    assert ss.seed_hold(_state()) == HoldState(None, None)


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
    hold = HoldState("up", "positive")
    ss.upsert_state(c, "2330", d, hold=hold, params_version=PARAMS_VERSION,
                    as_of="2026-06-11", updated_at="2026-06-11T14:30:00+08:00")
    got = ss.get_state(c, "2330")
    assert got is not None
    assert got.symbol == "2330"
    assert got.derived == d
    assert got.hold == hold  # hold columns round-trip
    assert got.params_version == PARAMS_VERSION


def test_store_hold_nulls_roundtrip() -> None:
    c = _conn()
    d = DerivedState(None, None, None, None, None, None)
    ss.upsert_state(c, "NULL", d, hold=HoldState(None, None),
                    params_version=PARAMS_VERSION, as_of="d", updated_at="t")
    got = ss.get_state(c, "NULL")
    assert got is not None
    assert got.hold == HoldState(None, None)


def test_store_upsert_replaces_not_duplicates() -> None:
    c = _conn()
    d1 = DerivedState("above_confirmed", None, None, "positive", Decimal("70"), "x")
    d2 = DerivedState("below_confirmed", None, None, "negative", Decimal("30"), "y")
    for d, h in ((d1, HoldState("up", "positive")), (d2, HoldState("down", "negative"))):
        ss.upsert_state(c, "2330", d, hold=h, params_version=PARAMS_VERSION,
                        as_of="2026-06-11", updated_at="t")
    rows = ss.all_states(c)
    assert len(rows) == 1
    assert rows[0].derived == d2
    assert rows[0].hold == HoldState("down", "negative")


def test_store_get_absent_is_none() -> None:
    assert ss.get_state(_conn(), "NOPE") is None


def test_store_clear_all() -> None:
    c = _conn()
    ss.upsert_state(c, "2330", _state(trend="above_confirmed"), hold=HoldState("up", None),
                    params_version=PARAMS_VERSION, as_of="d", updated_at="t")
    ss.clear_all(c)
    assert ss.all_states(c) == []
