"""R4 counter-evidence: 「同一筆錢、同樣的日期、放進那個市場的指數，會是多少？」

The review named this the single highest-value missing capability, and the reason is that
without it every return figure is unanchored: XIRR 15% is excellent or dismal depending on
what the market did, and nothing in the app answered that. AI-D43 records why
``twr.convert_closes`` — deliberately shelved by AI-D23 for scoring, where both legs are
natively same-currency — is the right tool HERE, where a MYR-funded US position compared
against a US index and reported in TWD makes FX the subject rather than noise.

Owner ruling 2026-08-26: **lifetime, one number.** The windowed comparison is already
answered by ``twr.build_overlay``; a second, differently-scoped counterfactual would be two
answers to one question (the AI-D2 defect class), and a windowed version additionally needs a
「what was the opening capital at the window start」 convention that nothing else needs.

⚠ The comparison is against **B** (含匯兌總損益 = ``total_value − net_invested``), never
against A (``total_return``). A applies FX to the GAIN only (AI-D41); the counterfactual buys
its units with reporting-currency money at each flow's own trade-date rate, so only B is
measured on the same ruler.
"""

from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.portfolio.benchmark_counterfactual import (
    ReportingFlow,
    counterfactual,
)
from portfolio_dash.shared.enums import Market

D = Decimal


def _closes(pairs: list[tuple[str, str]]) -> list[tuple[date, Decimal]]:
    return [(date.fromisoformat(d), D(v)) for d, v in pairs]


_US = _closes([("2026-01-01", "100"), ("2026-06-01", "110")])


# --- the arithmetic, hand-checked ----------------------------------------------------


def test_one_purchase_against_an_index_that_rose_ten_percent() -> None:
    """1,000 in at 100 buys 10 units; 10 units at 110 is 1,100. Nothing subtle."""
    out = counterfactual(
        [ReportingFlow(date(2026, 1, 1), Market.US, D("1000"))],
        {Market.US: _US},
    )
    assert out.available
    assert out.terminal_value == D("1100")
    assert out.net_invested == D("1000")
    assert out.benchmark_return == D("100")
    assert out.uncovered_markets == ()
    assert out.uncovered_ratio == D("0")


def test_a_sale_sells_units_at_that_days_close() -> None:
    """The counterfactual mirrors the SAME flow stream, so a withdrawal withdraws."""
    out = counterfactual(
        [
            ReportingFlow(date(2026, 1, 1), Market.US, D("1000")),   # 10 units
            ReportingFlow(date(2026, 6, 1), Market.US, D("-550")),   # -5 units at 110
        ],
        {Market.US: _US},
    )
    assert out.net_invested == D("450")
    assert out.terminal_value == D("550")       # 5 units left at 110
    assert out.benchmark_return == D("100")     # unchanged: the sale was at the last close


def test_a_flow_dated_before_the_benchmarks_first_close_is_uncovered_not_dropped() -> None:
    """Silently ignoring it would flatter the counterfactual with money it never held."""
    out = counterfactual(
        [
            ReportingFlow(date(2025, 6, 1), Market.US, D("1000")),   # predates the series
            ReportingFlow(date(2026, 1, 1), Market.US, D("1000")),
        ],
        {Market.US: _US},
    )
    assert out.uncovered_ratio == D("0.5")
    assert out.terminal_value == D("1100")      # only the placeable half


# --- the honesty guards --------------------------------------------------------------


def test_a_market_with_no_benchmark_is_named_not_silently_skipped() -> None:
    """A market absent from the supplied closes map. The money is still real.

    MY used to be that market in production too; it gained KLCI on 2026-08-27. This test
    never depended on the registry — it passes its own closes map — so what it pins is the
    MECHANISM, which is now reached by a market whose benchmark series has not been fetched
    yet (``dashboard.py`` only enters a market whose converted closes are non-empty).
    """
    out = counterfactual(
        [
            ReportingFlow(date(2026, 1, 1), Market.US, D("3000")),
            ReportingFlow(date(2026, 1, 1), Market.MY, D("1000")),
        ],
        {Market.US: _US},
    )
    assert out.uncovered_markets == ("MY",)
    assert out.uncovered_ratio == D("0.25")
    assert out.terminal_value == D("3300")


def test_full_coverage_reports_exactly_zero_not_a_rounded_almost_zero() -> None:
    out = counterfactual([ReportingFlow(date(2026, 1, 1), Market.US, D("1"))], {Market.US: _US})
    assert out.uncovered_ratio == D("0")


def test_no_flows_at_all_is_unavailable_with_a_reason_never_a_zero() -> None:
    """A zero would read as 「the index went nowhere」 rather than 「nothing to compare」."""
    out = counterfactual([], {Market.US: _US})
    assert not out.available and out.terminal_value is None and out.reason


def test_every_flow_uncovered_is_unavailable_not_a_zero_return() -> None:
    out = counterfactual([ReportingFlow(date(2026, 1, 1), Market.MY, D("1000"))], {})
    assert not out.available and out.reason
    assert out.uncovered_ratio == D("1")


def test_a_zero_close_is_never_a_denominator() -> None:
    """A zero close is a data defect; dividing by it would raise or fabricate."""
    out = counterfactual(
        [ReportingFlow(date(2026, 1, 1), Market.US, D("1000"))],
        {Market.US: _closes([("2026-01-01", "0"), ("2026-06-01", "110")])},
    )
    assert out.uncovered_ratio == D("1") and not out.available


def test_every_number_out_is_a_decimal() -> None:
    out = counterfactual(
        [ReportingFlow(date(2026, 1, 1), Market.US, D("1000"))], {Market.US: _US}
    )
    for v in (out.terminal_value, out.net_invested, out.benchmark_return, out.uncovered_ratio):
        assert isinstance(v, Decimal)


# --- per-market attribution ----------------------------------------------------------


def test_each_market_is_routed_to_its_own_index() -> None:
    tw = _closes([("2026-01-01", "50"), ("2026-06-01", "100")])   # +100%
    out = counterfactual(
        [
            ReportingFlow(date(2026, 1, 1), Market.US, D("1000")),   # -> 1,100
            ReportingFlow(date(2026, 1, 1), Market.TW, D("1000")),   # -> 2,000
        ],
        {Market.US: _US, Market.TW: tw},
    )
    assert out.terminal_value == D("3100")
    legs = {leg.market: leg for leg in out.by_market}
    assert legs[Market.US].terminal_value == D("1100")
    assert legs[Market.TW].terminal_value == D("2000")


@pytest.mark.parametrize("bad", [D("0"), D("-1")])
def test_a_nonpositive_close_anywhere_in_the_path_never_silently_scales(bad: Decimal) -> None:
    out = counterfactual(
        [ReportingFlow(date(2026, 6, 1), Market.US, D("1000"))],
        {Market.US: _closes([("2026-01-01", "100"), ("2026-06-01", str(bad))])},
    )
    assert out.uncovered_ratio == D("1")


def test_flows_carry_their_symbol_so_a_drawer_can_filter_without_rebuilding() -> None:
    """One flow stream, two consumers. Rebuilding it per symbol would let the drawer and
    the portfolio card disagree about which flows exist — the exact failure the shared
    ``build_reporting_flows`` extraction exists to prevent."""
    flows = [
        ReportingFlow(date(2026, 1, 1), Market.US, D("1000"), "AAPL"),
        ReportingFlow(date(2026, 1, 1), Market.US, D("1000"), "MSFT"),
    ]
    both = counterfactual(flows, {Market.US: _US})
    one = counterfactual([f for f in flows if f.symbol == "AAPL"], {Market.US: _US})
    assert both.terminal_value == D("2200")
    assert one.terminal_value == D("1100")
