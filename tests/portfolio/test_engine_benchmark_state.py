"""The benchmark counterfactual split into ``accumulate`` / ``finish`` (PERF spec §8.4).

``counterfactual(flows, closes) == finish(accumulate(flows, closes), closes)`` — and on every
corpus that equals the FROZEN v0.1.31 ``counterfactual`` by :func:`tests.reference.canon.canon`.
The accumulated :class:`BenchState` survives its canonical JSON form unchanged, so a settled
state finished with the same closes gives the same answer.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from portfolio_dash.portfolio.benchmark_counterfactual import (
    BenchState,
    ReportingFlow,
    accumulate,
    counterfactual,
    finish,
)
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.portfolio.timeseries import build_reporting_flows
from portfolio_dash.shared.enums import Market
from tests.reference import trend_v0131 as ref
from tests.reference.canon import canon, first_difference
from tests.reference.corpora import CORPUS_NAMES, TEMPLATE_DIR_NAME, copy_corpus, open_db
from tests.reference.dashboard_inputs import DashboardInputs, benchmark_closes, load_inputs

D = Decimal


@pytest.fixture(scope="module", params=CORPUS_NAMES)
def corpus(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
           ) -> Iterator[tuple[str, sqlite3.Connection, DashboardInputs]]:
    name: str = request.param
    base = tmp_path_factory.getbasetemp() / TEMPLATE_DIR_NAME
    db = copy_corpus(name, base, tmp_path_factory.mktemp(f"bench-{name}") / f"{name}.db")
    conn = open_db(db.path)
    try:
        yield name, conn, load_inputs(conn, db.now, db.reporting)
    finally:
        conn.close()


def _assert_same(label: str, expected: Any, got: Any) -> None:
    a, b = canon(expected), canon(got)
    assert a == b, f"{label}: {first_difference(a, b)}"


def _round_trip(state: BenchState) -> BenchState:
    return BenchState.from_json(json.loads(json.dumps(state.to_json())))


def test_the_split_equals_v0131_on_every_corpus(
    corpus: tuple[str, sqlite3.Connection, DashboardInputs],
) -> None:
    name, conn, i = corpus
    flows = build_reporting_flows(
        i.bundle, i.trend_fx_history, i.reporting, cash_movements=i.cash_movements,
        refused_dividends=build_book(i.bundle, allow_oversell=True).refused_dividends)
    if flows is None:
        pytest.skip(f"{name}: no reporting flows (the benchmark is unavailable)")
    closes = benchmark_closes(conn, i, flows)
    expected = ref.ref_counterfactual(flows, closes)
    _assert_same(f"{name} counterfactual", expected, counterfactual(flows, closes))
    state = accumulate(flows, closes)
    _assert_same(f"{name} finish", expected, finish(state, closes))
    again = _round_trip(state)
    _assert_same(f"{name} round trip", state, again)
    _assert_same(f"{name} finish after the round trip", expected, finish(again, closes))
    # finishing reads only each market's LAST close
    last_only = {m: [series[-1]] for m, series in closes.items()}
    _assert_same(f"{name} last closes only", expected, finish(state, last_only))


_TW = [(date(2026, 1, 5), D("150.25")), (date(2026, 2, 2), D("160.5"))]


@pytest.mark.parametrize("flows", [
    [],                                                                    # no flows
    [ReportingFlow(date(2026, 1, 9), None, D("30"))],                      # cash flow only
    [ReportingFlow(date(2025, 12, 1), Market.TW, D("1000"), "2330")],      # before the index
    [ReportingFlow(date(2026, 1, 9), Market.MY, D("500"), "1155")],        # no benchmark
    [ReportingFlow(date(2026, 1, 9), Market.TW, D("0"), "2330")],          # a zero flow
    [ReportingFlow(date(2026, 1, 9), Market.TW, D("30050"), "2330"),       # an exact quotient
     ReportingFlow(date(2026, 1, 20), Market.TW, D("-1000.5"), "2330"),
     ReportingFlow(date(2026, 1, 21), Market.MY, D("77"), "1155"),
     ReportingFlow(date(2026, 1, 22), None, D("-3"))],
], ids=["none", "cash-only", "before-history", "uncovered", "zero", "mixed"])
def test_the_split_on_the_edges(flows: list[ReportingFlow]) -> None:
    closes = {Market.TW: _TW}
    expected = ref.ref_counterfactual(flows, closes)
    _assert_same("edge", expected, counterfactual(flows, closes))
    state = accumulate(flows, closes)
    _assert_same("edge round trip", state, _round_trip(state))
    _assert_same("edge finish", expected, finish(_round_trip(state), closes))


def test_a_positive_exponent_is_refused_not_rewritten() -> None:
    state = BenchState(units={Market.TW: D("2E+40")}, invested={}, uncovered_gross=D("0"),
                       gross_all=D("0"), uncovered=frozenset(), placeable=1)
    with pytest.raises(ValueError, match="positive exponent"):
        state.to_json()
