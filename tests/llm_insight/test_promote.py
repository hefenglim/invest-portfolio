"""Pure shadow/auto-promote + regression decisions (spec 04.6). Deterministic, no LLM."""

from decimal import Decimal

from portfolio_dash.llm_insight import promote


def _cfg(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "auto_promote": False, "shadow_batches": 3, "max_shadows": 2,
        "min_samples": 8, "gap_alert_pp": "10",
    }
    base.update(kw)
    return base


def _score(n: int, miss: int, narrative_sum: int = 0) -> dict[str, object]:
    """A minimal combo_score-shaped dict for the decision functions."""
    avg = "0" if n == 0 else str(Decimal(narrative_sum) / Decimal(n))
    return {"n": n, "miss_count": miss, "avg_narrative": avg}


# --- decide_promotion ---------------------------------------------------------


def test_promote_when_batches_met_and_not_worse() -> None:
    cfg = _cfg(shadow_batches=3)
    active = _score(10, miss=5, narrative_sum=600)   # 50% miss, avg 60
    shadow = _score(3, miss=0, narrative_sum=240)    # 0% miss, avg 80 → clearly better
    assert promote.decide_promotion(active, shadow, cfg) == "promote"


def test_hold_when_shadow_batches_insufficient() -> None:
    cfg = _cfg(shadow_batches=5)
    active = _score(10, miss=8)
    shadow = _score(3, miss=0)  # only 3 < 5 shadow batches
    assert promote.decide_promotion(active, shadow, cfg) == "hold"


def test_hold_when_shadow_worse() -> None:
    cfg = _cfg(shadow_batches=3)
    active = _score(10, miss=2, narrative_sum=800)  # 20% miss, avg 80
    shadow = _score(3, miss=3, narrative_sum=60)    # 100% miss, avg 20 → worse
    assert promote.decide_promotion(active, shadow, cfg) == "hold"


def test_promote_when_equal_miss_rate_tie_breaks_not_worse() -> None:
    # equal miss rate, shadow narrative not lower → "not worse" → promote.
    cfg = _cfg(shadow_batches=3)
    active = _score(10, miss=5, narrative_sum=700)  # 50% miss, avg 70
    shadow = _score(3, miss=2, narrative_sum=240)   # ~66%? no: 2/3=0.667 worse
    # make shadow exactly equal miss rate: 50% of 4 = 2
    shadow = _score(4, miss=2, narrative_sum=320)   # 50% miss, avg 80 → not worse
    assert promote.decide_promotion(active, shadow, cfg) == "promote"


def test_hold_when_no_shadow_samples() -> None:
    cfg = _cfg(shadow_batches=3)
    assert promote.decide_promotion(_score(10, 1), _score(0, 0), cfg) == "hold"


# --- shadow detection ---------------------------------------------------------


def test_shadow_version_when_active_not_latest() -> None:
    # active=1, latest non-archived=3 → shadow is 3.
    assert promote.shadow_version(active_version=1, latest_version=3) == 3


def test_no_shadow_when_active_is_latest() -> None:
    assert promote.shadow_version(active_version=3, latest_version=3) is None


def test_no_shadow_when_no_active_and_single_version() -> None:
    # active None (no manual selection) + latest 1 → no shadow yet (nothing to compare).
    assert promote.shadow_version(active_version=None, latest_version=1) is None


def test_shadow_when_active_none_and_multiple_versions() -> None:
    # active None but multiple versions exist → the latest is the shadow candidate.
    assert promote.shadow_version(active_version=None, latest_version=2) == 2


# --- regression detection -----------------------------------------------------


def test_regression_when_rolling_worsens() -> None:
    # recent half (4) miss rate clearly worse than the earlier half (4) → regress.
    assert promote.is_regressing(recent_miss=4, recent_n=4, baseline_miss=0, baseline_n=4) is True


def test_no_regression_below_min_window() -> None:
    # recent window below the 4-sample minimum → never fires.
    assert promote.is_regressing(recent_miss=3, recent_n=3, baseline_miss=0, baseline_n=4) is False


def test_no_regression_when_stable() -> None:
    assert promote.is_regressing(recent_miss=1, recent_n=4, baseline_miss=1, baseline_n=4) is False
