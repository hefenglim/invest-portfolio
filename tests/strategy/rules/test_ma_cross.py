"""Rule ② 50/200 cross: golden/death detection, volume confidence, age decay.

Uses small fast/slow so the SMA pair and the exact cross index are hand-computable.
The canonical fixture (``_CLOSES``, fast=2/slow=4) has a golden cross established at
absolute session index 7 -> ``days_ago == 2``:

    SMA2/SMA4 sign flips from -1 to +1 at t=7 (verified by hand in the test docstring).
"""

from decimal import Decimal

from portfolio_dash.strategy.rules import ma_cross as MC
from portfolio_dash.strategy.rules.params import MaCrossParams


def _s(vals: list[float]) -> list[Decimal]:
    return [Decimal(str(v)) for v in vals]


# fast=2, slow=4, small windows so a cross is hand-checkable.
_P = MaCrossParams(fast=2, slow=4, volume_window=2, cross_lookback=10, decay_sessions=10)
# Decline (fast below slow) then a rise that lifts fast above slow -> golden at t=7.
_CLOSES = _s([20, 18, 16, 14, 12, 10, 14, 18, 22, 26])
# decay at days_ago=2 over 10 sessions = 1 - 2/10 = 0.8.
_DECAY = Decimal("0.8")


def test_golden_cross_index_and_days_ago() -> None:
    rs = MC.evaluate(_CLOSES, None, _P)
    assert rs is not None
    assert rs.state == "golden"
    assert rs.evidence["cross"] == "golden"
    assert rs.evidence["days_ago"] == 2
    assert rs.evidence["decay_factor"] == _DECAY


def test_death_cross_detected() -> None:
    # Rise then decline: fast drops below slow -> death.
    closes = _s([10, 12, 14, 16, 18, 20, 16, 12, 8, 4])
    rs = MC.evaluate(closes, None, _P)
    assert rs is not None
    assert rs.state == "death"
    assert rs.score < Decimal("0")


def test_volume_confirmed_true_scales_full() -> None:
    # Cross-day (index 7) volume 300 > avg of the 2 bars before (100) -> confirmed.
    vols = _s([100, 100, 100, 100, 100, 100, 100, 300, 100, 100])
    rs = MC.evaluate(_CLOSES, vols, _P)
    assert rs is not None
    assert rs.evidence["volume_confirmed"] is True
    assert rs.evidence["confidence_modifier"] == Decimal("1.00")
    # score = +1 * 1.00 * 0.8
    assert rs.score == Decimal("1") * Decimal("1.00") * _DECAY


def test_volume_unconfirmed_scales_075() -> None:
    vols = _s([100, 100, 100, 100, 100, 100, 100, 50, 100, 100])  # 50 <= avg 100
    rs = MC.evaluate(_CLOSES, vols, _P)
    assert rs is not None
    assert rs.evidence["volume_confirmed"] is False
    assert rs.score == Decimal("1") * Decimal("0.75") * _DECAY


def test_volume_unknown_gap_at_cross_scales_085() -> None:
    # None ON the cross day -> unknown (never faked as confirmed).
    vols: list[Decimal | None] = [Decimal("100")] * 7 + [None] + [Decimal("100")] * 2
    rs = MC.evaluate(_CLOSES, vols, _P)
    assert rs is not None
    assert rs.evidence["volume_confirmed"] is None
    assert rs.score == Decimal("1") * Decimal("0.85") * _DECAY


def test_volume_unknown_gap_in_window_scales_085() -> None:
    # None in the pre-cross averaging window (index 5) -> also unknown.
    vols: list[Decimal | None] = [Decimal("100")] * 5 + [None] + [Decimal("100")] * 4
    rs = MC.evaluate(_CLOSES, vols, _P)
    assert rs is not None
    assert rs.evidence["volume_confirmed"] is None
    assert rs.evidence["confidence_modifier"] == Decimal("0.85")


def test_no_volumes_is_unknown() -> None:
    rs = MC.evaluate(_CLOSES, None, _P)
    assert rs is not None
    assert rs.evidence["volume_confirmed"] is None
    assert rs.evidence["confidence_modifier"] == Decimal("0.85")


def test_volume_confirm_disabled_gives_full_confidence() -> None:
    params = MaCrossParams(fast=2, slow=4, volume_confirm=False,
                           cross_lookback=10, decay_sessions=10)
    rs = MC.evaluate(_CLOSES, None, params)
    assert rs is not None
    assert rs.evidence["volume_confirm_enabled"] is False
    assert rs.evidence["confidence_modifier"] == Decimal("1")
    assert rs.score == Decimal("1") * Decimal("1") * _DECAY


def test_decay_reduces_older_cross_score() -> None:
    # Same golden cross but a longer decay horizon -> less decay -> larger score.
    slow_decay = MaCrossParams(fast=2, slow=4, volume_window=2,
                               cross_lookback=100, decay_sessions=100)
    rs_fast = MC.evaluate(_CLOSES, None, _P)
    rs_slow = MC.evaluate(_CLOSES, None, slow_decay)
    assert rs_fast is not None and rs_slow is not None
    assert rs_slow.score > rs_fast.score  # 1-2/100 > 1-2/10


def test_stale_cross_beyond_lookback_falls_to_relationship() -> None:
    # cross_lookback=1 cannot see the cross 2 sessions ago -> standing relationship.
    params = MaCrossParams(fast=2, slow=4, cross_lookback=1, decay_sessions=1)
    rs = MC.evaluate(_CLOSES, None, params)
    assert rs is not None
    assert rs.state == "fast_above"
    assert rs.score == Decimal("0.4")
    assert rs.evidence["cross"] is None


def test_most_recent_of_two_crosses_wins() -> None:
    # Golden at t=7 (days_ago 6) THEN death at t=11 (days_ago 2) — the newer cross
    # must win (deep review 2026-07-10: single-cross fixtures could not catch an
    # oldest-first scan mutation).
    closes = _s([20, 18, 16, 14, 12, 10, 14, 18, 22, 26, 20, 14, 8, 2])
    rs = MC.evaluate(closes, None, _P)
    assert rs is not None
    assert rs.state == "death"
    assert rs.evidence["days_ago"] == 2


def test_volumes_shorter_than_closes_degrade_to_unknown() -> None:
    # Cross at absolute index 7 but only 6 volume entries: must NOT raise; the
    # confirmation is honestly unknown (deep review 2026-07-10 MEDIUM fix).
    vols = _s([100, 100, 100, 100, 100, 100])
    rs = MC.evaluate(_CLOSES, vols, _P)
    assert rs is not None
    assert rs.state == "golden"
    assert rs.evidence["volume_confirmed"] is None
    assert rs.evidence["confidence_modifier"] == Decimal("0.85")


def test_fully_decayed_boundary_is_not_detected_as_cross() -> None:
    # days_ago == decay_sessions would decay to score 0 — a contradictory
    # "detected golden, score 0" state. The detection window excludes the boundary,
    # so it falls through to the standing relationship (deep review 2026-07-10).
    params = MaCrossParams(fast=2, slow=4, cross_lookback=2, decay_sessions=2)
    rs = MC.evaluate(_CLOSES, None, params)  # golden is exactly 2 sessions ago
    assert rs is not None
    assert rs.state == "fast_above"
    assert rs.score == Decimal("0.4")
    assert rs.evidence["cross"] is None


def test_aligned_flat_series_is_neutral() -> None:
    # Flat series: fast MA == slow MA exactly -> aligned, score 0 (not fast_above).
    rs = MC.evaluate(_s([50] * 12), None, _P)
    assert rs is not None
    assert rs.state == "aligned"
    assert rs.score == Decimal("0")
    assert rs.evidence["cross"] is None


def test_insufficient_data_returns_none() -> None:
    assert MC.evaluate(_s([1, 2, 3]), None, _P) is None  # < slow
    assert MC.evaluate([], None, _P) is None


def test_window_days_bounded() -> None:
    rs = MC.evaluate(_CLOSES, None, _P)
    assert rs is not None
    # min(n=10, slow+cross_lookback=14) = 10
    assert rs.window_days == 10
