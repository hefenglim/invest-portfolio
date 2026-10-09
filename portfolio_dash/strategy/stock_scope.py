"""The STOCK totals the strategy previews size their weights against (options spec OPT-D5).

What-if and rebalance express a position as a weight of the portfolio. On the dashboard that
weight is ``stock MV / Σ stock MV`` — holdings, allocation, weights and subtotals are
stock-only, while the KPI 總市值 includes open option contracts (at their last trade price, R2;
a sold put's value is negative). A preview that divided by the KPI total would therefore show
a weight the holdings table does not, and a rebalance would size its trades against a total
that a short put lowers.

The stock total is the grand ``(None, None)`` cell of ``DashboardData.holdings_subtotals`` — a
regrouping of the SAME per-holding values the KPI sums, in the same order, so on a ledger with
no option it IS ``kpis.total_market_value`` byte for byte (``portfolio/dashboard.py::
_holdings_subtotals``; pinned by ``tests/portfolio/test_dashboard.py`` and
``tests/contract/test_options_excluded_from_stock_readers.py``). Nothing is computed here: the
figure is read, never re-derived.
"""

from decimal import Decimal

from portfolio_dash.portfolio.dashboard_models import DashboardData


def stock_market_value(data: DashboardData) -> Decimal | None:
    """Σ stock market value in the reporting currency, or ``None`` when the dashboard could not
    form it (the same all-or-nothing FX degradation the KPI uses)."""
    for cell in data.holdings_subtotals:
        if cell.account_id is None and cell.market is None:
            return cell.total_market_value
    # No grand cell: build_dashboard always emits one first, so this is a hand-built
    # DashboardData (a test double). Fall back to the KPI, the figure the cell equals on a
    # stock ledger.
    return data.kpis.total_market_value
