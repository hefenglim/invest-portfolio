"""Computed FX (換匯) P&L result models."""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel

from portfolio_dash.shared.enums import Currency


class AccountFXResult(BaseModel):
    """Per-account FX P&L. Money figures (realized/unrealized) are in ``home_ccy``;
    ``foreign_cash`` and ``foreign_stock_value`` are in ``foreign_ccy``.

    ``foreign_cash`` may be negative — net foreign drawn beyond the tracked conversions
    (e.g. an untracked funding path or sale proceeds). A negative balance is a signal the
    consumer should flag rather than render an FX figure on directly.
    """

    account_id: str
    home_ccy: Currency
    foreign_ccy: Currency
    avg_rate: Decimal | None
    current_spot: Decimal | None
    foreign_cash: Decimal
    foreign_stock_value: Decimal
    realized_fx: Decimal | None
    unrealized_fx_stocks: Decimal | None
    unrealized_fx_cash: Decimal | None


class FxRealizedRow(BaseModel):
    """One realized-FX event from a reconversion (foreign -> home)."""

    date: date
    foreign_ccy: Currency
    home_ccy: Currency
    foreign_sold: Decimal
    home_received: Decimal
    rate_used: Decimal
    realized: Decimal


class FXSummary(BaseModel):
    """All per-account results plus a reporting-currency rollup.

    Money figures are full-precision (not quantized); quantize at display/settlement.
    """

    by_account: dict[str, AccountFXResult]
    reporting_currency: Currency
    reporting_realized_fx: Decimal
    reporting_unrealized_fx: Decimal
