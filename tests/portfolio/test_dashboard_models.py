from datetime import datetime
from decimal import Decimal

from portfolio_dash.portfolio.dashboard_models import (
    DashboardData,
    DividendSummary,
    FreshnessReport,
    HoldingRow,
    HoldingSubtotal,
    KpiSummary,
    TrendSeries,
)
from portfolio_dash.portfolio.results import Holding, RealizedPnL
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.wire import to_wire


def _minimal_dashboard() -> DashboardData:
    return DashboardData(
        as_of=datetime(2026, 6, 10, 12, 0),
        reporting_currency=Currency.TWD,
        kpis=KpiSummary(reporting_currency=Currency.TWD,
                        total_market_value=Decimal("639600")),
        holdings=[],
        realized=RealizedPnL(rows=[], by_currency={}),
        returns=None,
        allocation=None,
        currency_view=None,
        fx=None,
        dividends=DividendSummary(by_year=[], total_by_currency={}),
        ex_dividend_calendar=[],
        trend=TrendSeries(points=[], reporting_currency=Currency.TWD, available=False),
        freshness=FreshnessReport(prices=[], fx=[], any_stale=False,
                                  missing_prices=[], missing_fx=[]),
    )


def test_dashboard_data_round_trips_and_preserves_decimal() -> None:
    data = _minimal_dashboard()
    dumped = data.model_dump()
    assert dumped["kpis"]["total_market_value"] == Decimal("639600")
    assert isinstance(dumped["kpis"]["total_market_value"], Decimal)
    assert DashboardData.model_validate(dumped) == data
    assert data.insights == []  # placeholder defaults empty
    assert data.holdings_subtotals == []  # additive field defaults empty


def test_holdings_subtotals_round_trip_and_wire() -> None:
    """Wave A3: the holdings_subtotals cells round-trip as Decimal and serialize to the
    canonical Decimal STRING (money is never a JSON number); Market/None axes pass through,
    and an unavailable figure stays an honest None."""
    data = _minimal_dashboard().model_copy(update={
        "holdings_subtotals": [
            HoldingSubtotal(account_id=None, market=None,
                            total_market_value=Decimal("639600"),
                            unrealized_total=Decimal("12345.00")),
            HoldingSubtotal(account_id="schwab", market=Market.US,
                            total_market_value=Decimal("1E+2"),
                            unrealized_total=None),
        ],
    })
    dumped = data.model_dump()
    assert isinstance(dumped["holdings_subtotals"][0]["total_market_value"], Decimal)
    assert DashboardData.model_validate(dumped) == data  # exact round-trip

    wire = to_wire(dumped)
    grand = wire["holdings_subtotals"][0]
    assert grand["account_id"] is None and grand["market"] is None
    assert grand["total_market_value"] == "639600"   # Decimal -> canonical string
    assert grand["unrealized_total"] == "12345.00"   # trailing zeros preserved
    cell = wire["holdings_subtotals"][1]
    assert cell["market"] == "US"                      # Market enum -> value
    assert cell["total_market_value"] == "100"        # 1E+2 expanded, never scientific
    assert cell["unrealized_total"] is None            # honest None passes through


def test_dashboard_wire_payload_has_no_scientific_notation_decimal() -> None:
    # spec-18 guard (#2c/M1): once a DashboardData with a tiny-rate Decimal flows through
    # the canonical wire encoder, no string field carries scientific notation.
    data = _minimal_dashboard().model_copy(update={
        "kpis": KpiSummary(reporting_currency=Currency.TWD,
                           total_market_value=Decimal("1E-7")),
    })
    wire = to_wire(data.model_dump())
    assert wire["kpis"]["total_market_value"] == "0.0000001"

    def _assert_no_sci(node: object) -> None:
        if isinstance(node, str):
            assert "E" not in node and "e-" not in node and "e+" not in node
        elif isinstance(node, dict):
            for v in node.values():
                _assert_no_sci(v)
        elif isinstance(node, list):
            for v in node:
                _assert_no_sci(v)

    _assert_no_sci(wire)


def test_holding_row_builds_from_holding_dump_plus_enrichment() -> None:
    h = Holding(account_id="schwab", symbol="AAPL", quote_ccy=Currency.USD,
                shares=Decimal("10"), original_avg=Decimal("100"),
                adjusted_avg=Decimal("100"), original_cost_total=Decimal("1000"),
                adjusted_cost_total=Decimal("1000"), dividend_portion=Decimal("0"),
                payback_ratio=Decimal("0"))
    data = h.model_dump()
    data.update(account_name="Charles Schwab", name="Apple", market=Market.US,
                sector="Tech", board="", price_as_of=None, weight=None)
    row = HoldingRow(**data)
    assert row.symbol == "AAPL"
    assert row.account_name == "Charles Schwab"
    assert row.market_value is None and row.weight is None
