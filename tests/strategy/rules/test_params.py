"""Frozen-parameter discipline: v1 defaults, version stamp, immutability.

The parameters ARE the reproducibility contract (replay/rebuild), so they must be
frozen and stamped. Every default is a Decimal (never float).
"""

from collections.abc import Callable
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from portfolio_dash.strategy.rules import params as P


def test_params_version_is_rules_v1() -> None:
    assert P.PARAMS_VERSION == "rules-v1"


def test_default_params_values() -> None:
    dp = P.default_params()
    assert dp.trend == P.TrendFilterParams(ma=200, band=Decimal("0.02"), confirm_days=2)
    assert dp.cross.fast == 50 and dp.cross.slow == 200
    assert dp.cross.volume_confirm is True and dp.cross.volume_window == 20
    assert dp.cross.cross_lookback == dp.cross.decay_sessions == 120
    assert dp.momentum.lookback_sessions == 252 and dp.momentum.skip_sessions == 21
    assert dp.rsi.period == 14
    assert dp.rsi.overbought == Decimal("70") and dp.rsi.oversold == Decimal("30")
    # Weights sum to exactly 100.
    w = dp.weights
    assert w.trend + w.cross + w.momentum + w.rsi_context == Decimal("100")


def test_defaults_are_decimal_not_float() -> None:
    dp = P.default_params()
    for value in (dp.trend.band, dp.momentum.flat_epsilon, dp.momentum.full_scale,
                  dp.rsi.overbought, dp.rsi.oversold, dp.weights.trend):
        assert isinstance(value, Decimal)


def test_default_params_are_independent_but_equal() -> None:
    # default_factory means two builds are equal-by-value yet distinct objects.
    a, b = P.default_params(), P.default_params()
    assert a == b
    assert a is not b


@pytest.mark.parametrize("build", [
    P.TrendFilterParams,
    P.MaCrossParams,
    P.MomentumParams,
    P.RsiRegimeParams,
    P.CompositeWeights,
    P.RulesParams,
])
def test_params_are_frozen(build: Callable[[], object]) -> None:
    obj = build()
    with pytest.raises(FrozenInstanceError):
        obj.ma = 5  # type: ignore[attr-defined]
