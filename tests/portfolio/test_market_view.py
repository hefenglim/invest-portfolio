"""Unit tests for portfolio.market_view — pure market slicing (per_market spec)."""

from decimal import Decimal

from portfolio_dash.portfolio import market_view as mv
from portfolio_dash.portfolio.dashboard_models import HoldingRow
from portfolio_dash.shared.enums import Currency, Market


def _row(symbol: str, market: Market, sector: str, value: Decimal | None) -> HoldingRow:
    return HoldingRow(
        account_id="a", account_name="A", symbol=symbol, name=symbol, market=market,
        sector=sector, board="", quote_ccy=Currency.TWD, shares=Decimal("1"),
        original_avg=Decimal("1"), adjusted_avg=Decimal("1"),
        original_cost_total=Decimal("1"), adjusted_cost_total=Decimal("1"),
        dividend_portion=Decimal("0"), payback_ratio=Decimal("0"),
        market_value=value,
    )


def test_market_holdings_filters_and_preserves_order() -> None:
    rows = [
        _row("2330", Market.TW, "Semi", Decimal("100")),
        _row("AAPL", Market.US, "Tech", Decimal("50")),
        _row("2412", Market.TW, "Telecom", Decimal("50")),
    ]
    out = mv.market_holdings(rows, "TW")
    assert [h.symbol for h in out] == ["2330", "2412"]


def test_market_allocation_reweights_within_market() -> None:
    rows = [
        _row("2330", Market.TW, "Semi", Decimal("150")),
        _row("2412", Market.TW, "Telecom", Decimal("50")),
        _row("AAPL", Market.US, "Tech", Decimal("999")),  # other market never counted
    ]
    alloc = mv.market_allocation(rows, "TW")
    assert alloc["Semi"] == Decimal("0.75")
    assert alloc["Telecom"] == Decimal("0.25")


def test_market_allocation_skips_missing_values_and_empty_market() -> None:
    rows = [
        _row("2330", Market.TW, "Semi", Decimal("100")),
        _row("00919", Market.TW, "ETF", None),  # missing price: skipped, never guessed
    ]
    alloc = mv.market_allocation(rows, "TW")
    assert alloc == {"Semi": Decimal("1")}
    assert mv.market_allocation(rows, "MY") == {}


def test_market_ccy_map_is_one_to_one() -> None:
    assert mv.MARKET_QUOTE_CCY == {"TW": "TWD", "US": "USD", "MY": "MYR"}
