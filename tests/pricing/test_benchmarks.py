"""Unit tests for the benchmark registry's market map (AI-D22).

The yardstick for a ``relative`` prediction is fixed by the code, never chosen by the LLM:
TW → 0050, US → S&P 500, MY → KLCI (wired 2026-08-27, once ``yf_symbol`` learned that a
``^``-prefixed Yahoo ticker takes no exchange suffix). Every market now has a yardstick, so
the ``None`` degradation path has no market left to exercise it — which is exactly why the
last test below reaches for a monkeypatch instead of deleting it. An unmapped market must
still answer ``None`` rather than a guessed proxy, and code no test exercises is code that
stops working quietly.
"""

import pytest

from portfolio_dash.pricing import benchmarks as bm
from portfolio_dash.pricing.benchmarks import benchmark_for_market
from portfolio_dash.shared.enums import Currency, Market


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


def test_my_maps_to_klci() -> None:
    bench = benchmark_for_market(Market.MY)
    assert bench is not None
    assert bench.key == "klci"
    assert bench.storage_key == "^KLSE"
    # AI-D23: both legs of a `relative` comparison are quoted in the LOCAL currency, so an
    # MYR stock is measured against an MYR index — the reason a USD proxy was refused.
    assert bench.quote_ccy is Currency.MYR


def test_an_unmapped_market_still_answers_none_rather_than_a_proxy(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """AI-D22's red line outlives the market that used to demonstrate it.

    No proxy, no guessed index — ``None`` becomes ``pending_data`` upstream, never a
    fabricated hit or miss. With all three markets now mapped, the only way to keep this
    path honest is to unmap one on purpose.
    """
    monkeypatch.setitem(bm._MARKET_BENCHMARK, Market.MY, "no-such-benchmark-key")
    assert benchmark_for_market(Market.MY) is None      # unknown key → None, not a crash
    monkeypatch.delitem(bm._MARKET_BENCHMARK, Market.MY)
    assert benchmark_for_market(Market.MY) is None      # absent market → None
