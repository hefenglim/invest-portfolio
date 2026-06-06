"""Computed result models produced by the calculation core."""

from decimal import Decimal

from pydantic import BaseModel

from portfolio_dash.shared.enums import Currency


class Holding(BaseModel):
    """An open position with cost basis and (once valued) market fields."""

    account_id: str
    symbol: str
    quote_ccy: Currency
    shares: Decimal
    original_avg: Decimal
    adjusted_avg: Decimal
    original_cost_total: Decimal
    adjusted_cost_total: Decimal
    dividend_portion: Decimal
    payback_ratio: Decimal
    market_price: Decimal | None = None
    market_value: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    capital_gain: Decimal | None = None
    price_stale: bool = False


class RealizedRow(BaseModel):
    """One realized event from a sell."""

    account_id: str
    symbol: str
    quote_ccy: Currency
    shares_sold: Decimal
    proceeds_net: Decimal
    original_cost_removed: Decimal
    adjusted_cost_removed: Decimal
    realized: Decimal


class RealizedPnL(BaseModel):
    """All realized rows plus per-currency totals."""

    rows: list[RealizedRow]
    by_currency: dict[Currency, Decimal]


class Book(BaseModel):
    """Output of the ledger replay: open holdings, realized, gross capital deployed."""

    holdings: list[Holding]
    realized: RealizedPnL
    gross_invested: dict[Currency, Decimal]


class CurrencyReturn(BaseModel):
    """Per-currency return breakdown."""

    realized: Decimal
    unrealized: Decimal
    total_return: Decimal
    gross_invested: Decimal
    rate: Decimal | None


class ReturnSummary(BaseModel):
    """Per-currency returns + blended reporting-currency total + XIRR."""

    by_currency: dict[Currency, CurrencyReturn]
    reporting_currency: Currency
    reporting_total_return: Decimal
    xirr: Decimal | None = None


class SectorAllocation(BaseModel):
    """Reporting-currency value and weight per sector."""

    by_sector: dict[str, Decimal]
    weights: dict[str, Decimal]
    reporting_currency: Currency


class CombinedView(BaseModel):
    """Per-currency market value + blended reporting-currency total."""

    by_currency_value: dict[Currency, Decimal]
    reporting_total_value: Decimal
    reporting_currency: Currency
