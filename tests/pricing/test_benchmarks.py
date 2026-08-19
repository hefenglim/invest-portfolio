"""Unit tests for the benchmark registry's market map (AI-D22).

The yardstick for a ``relative`` prediction is fixed by the code, never chosen by the LLM:
TW → 0050, US → S&P 500, and MY honestly has none (an MYR stock vs a USD index is noise,
so the caller degrades to ``pending_data`` rather than guessing a proxy).
"""

from portfolio_dash.pricing.benchmarks import benchmark_for_market
from portfolio_dash.shared.enums import Market


def test_tw_maps_to_0050() -> None:
    bench = benchmark_for_market(Market.TW)
    assert bench is not None
    assert bench.key == "0050"
    assert bench.storage_key == "0050"


def test_us_maps_to_sp500() -> None:
    bench = benchmark_for_market(Market.US)
    assert bench is not None
    assert bench.key == "sp500"
    assert bench.storage_key == "^GSPC"


def test_my_has_no_benchmark_and_that_is_the_honest_answer() -> None:
    # AI-D22: no proxy, no guessed index — None → pending_data, never a fabricated miss.
    assert benchmark_for_market(Market.MY) is None
