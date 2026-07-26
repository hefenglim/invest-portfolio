"""Computed result models produced by the calculation core."""

from datetime import date
from decimal import Decimal
from typing import Literal

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
    # 賣超/oversold: net shares went negative (a sell exceeded holdings, written after an
    # acked oversell). Cost basis + P&L are 待釐清 (not computed — see cost_basis.build_book);
    # the position is excluded from portfolio aggregates and flagged for the user to fix
    # (e.g. record the missing opening inventory / buy). Lightweight: NOT short accounting.
    oversold: bool = False


class RealizedRow(BaseModel):
    """One realized event: a sell, or a cash dividend paid after the position closed.

    ``kind`` (audit H2, 2026-07-26) distinguishes the two, because a post-close dividend is
    realized INCOME, not a capital gain:

    * ``"sale"`` — the original meaning. ``shares_sold`` > 0; ``realized`` = net proceeds −
      adjusted cost removed.
    * ``"dividend"`` — a CASH-family dividend whose payment date falls after the position
      already reached zero shares (TW/MY pay weeks after the ex-date, so selling out in
      between is ordinary). There is no cost left to reduce, so the payout is booked here
      instead of vanishing with the closed position. ``shares_sold`` and both cost fields
      are 0; ``proceeds_net`` == ``realized`` == the dividend net; ``sell_date`` is the
      dividend's payment date.

    Consumers that mean CAPITAL GAIN specifically (the tax package's realized-gains sheet)
    MUST filter on ``kind == "sale"`` — the dividend is reported there on its own sheet,
    from the dividend ledger, and counting it twice would misstate taxable income.
    """

    account_id: str
    symbol: str
    quote_ccy: Currency
    sell_date: date
    shares_sold: Decimal
    proceeds_net: Decimal
    original_cost_removed: Decimal
    adjusted_cost_removed: Decimal
    realized: Decimal
    kind: Literal["sale", "dividend"] = "sale"


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
