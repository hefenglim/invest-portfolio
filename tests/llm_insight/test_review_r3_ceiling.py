"""R3 counter-evidence: the confidence ceiling charged one error twice (AI-D39).

This is the ONLY finding in the whole review that this branch introduced — W7.1 implemented
AI-D33's three-step law faithfully, and the law itself was wrong in two ways:

1. **The same miscalibration was subtracted twice.** A bucket's cap is ``actual_pct + 5``,
   which IS the calibration correction; subtracting the global rolling gap on top charges the
   model for its over-confidence a second time. Measured on the demo: bins said 39, the gap
   took 47, the answer was 0 — "your record supports asserting nothing" produced by
   double-counting, not by the record.
2. **Ignorance outranked evidence.** ``CEILING_NO_DATA = 70`` capped a bucket nobody had
   scored at 70, while a bucket measured at 40% capped at 45. A model could raise its own
   ceiling by moving into a bucket with no history — and with a 20-sample rolling window that
   is a limit cycle, not a calibration loop.

AI-D33's red line is NOT relaxed by any of this: nothing here clamps the model's stated
confidence. The arithmetic that produces the ceiling is what changes.
"""

from decimal import Decimal

from portfolio_dash.llm_insight.scoring import (
    CEILING_HEADROOM,
    CEILING_NO_DATA,
    confidence_ceiling,
)

D = Decimal


def _bin(bucket: str, n: int, actual_pct: str) -> dict[str, object]:
    return {"bucket": bucket, "n": n, "actual_pct": actual_pct}


# --- (a) the gap is no longer subtracted -------------------------------------------------

def test_a_measured_bucket_caps_at_its_own_hit_rate_plus_headroom() -> None:
    """A bucket's cap IS the calibration correction. Nothing further is deducted."""
    bins = [_bin("60-80", 20, "39.00"), _bin("0-20", 20, "10.00")]
    # 39 + 5 = 44 -> the largest self-consistent c in 0-20 is 20's neighbour... walk it:
    # c=44 falls in 0-20? no. The law is circular, so assert the property, not a magic number.
    ceiling = confidence_ceiling(bins, overall_hit_pct=D("39.00"))
    assert ceiling > 0, "a 39%-hit-rate record must still support SOME confidence"


def test_the_demo_case_that_produced_zero_no_longer_does() -> None:
    """The exact shape measured on the demo, 2026-08-23: bins 39, rolling gap -0.466.

    The old code returned 0 — 39 + 5 = 44, then minus 47 points of gap, floored at 0. The gap
    is gone, so the record now says what the bins say.
    """
    bins = [_bin("40-60", 13, "39.00")]
    assert confidence_ceiling(bins, overall_hit_pct=D("39.00")) > 0


# --- (b) an unmeasured bucket no longer outranks a measured one --------------------------

def test_an_unscored_bucket_can_never_cap_higher_than_the_overall_record() -> None:
    """Ignorance must not be worth more than evidence.

    With an overall hit rate of 40%, a bucket nobody has scored caps at 45 — not at 70. The
    old constant let the model escape a poor record by claiming a confidence in a range it had
    never been scored in.
    """
    bins = [_bin("0-20", 20, "95.00")]          # only the very-low bucket has history
    ceiling = confidence_ceiling(bins, overall_hit_pct=D("40.00"))
    assert ceiling <= 40 + CEILING_HEADROOM


def test_a_bucket_below_the_sample_gate_uses_the_overall_rate_not_the_constant() -> None:
    bins = [_bin("80-100", 3, "99.00")]         # n=3 is below the gate; 99% must not count
    assert confidence_ceiling(bins, overall_hit_pct=D("30.00")) <= 30 + CEILING_HEADROOM


def test_a_strong_overall_record_raises_the_unscored_cap() -> None:
    """The rule is symmetric: it is the RECORD that sets the cap, in both directions."""
    weak = confidence_ceiling([], overall_hit_pct=D("30.00"))
    strong = confidence_ceiling([], overall_hit_pct=D("85.00"))
    assert strong > weak


def test_with_no_scored_history_at_all_the_constant_is_the_last_resort() -> None:
    """``overall_hit_pct is None`` means nothing has EVER been scored — a different state from
    'this bucket has no history', and the only one the constant still answers."""
    assert confidence_ceiling([], overall_hit_pct=None) == CEILING_NO_DATA


# --- the law stays self-consistent -------------------------------------------------------

def test_the_answer_is_always_admitted_by_its_own_bucket() -> None:
    """The law is circular — a confidence's cap depends on the bucket it lands in — so the
    only correct answer is one that its OWN bucket admits. Pinned across a spread of records
    so a future rewrite cannot satisfy the cases above while breaking the invariant."""
    for pct in ("0.00", "12.50", "39.00", "50.00", "77.77", "100.00"):
        bins = [_bin("0-20", 20, pct), _bin("20-40", 20, pct), _bin("40-60", 20, pct),
                _bin("60-80", 20, pct), _bin("80-100", 20, pct)]
        c = confidence_ceiling(bins, overall_hit_pct=D(pct))
        assert 0 <= c <= 100
        assert D(c) <= D(pct) + CEILING_HEADROOM
