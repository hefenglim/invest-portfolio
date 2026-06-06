"""Source-of-truth ledger models: transactions, dividends, FX, opening inventory."""

from datetime import date

from pydantic import BaseModel

from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.types import Money


class Transaction(BaseModel):
    """A buy or sell. Fees/tax are the snapshot taken at entry; stored, never recomputed."""

    account_id: str
    symbol: str
    side: Side
    quantity: Money
    price: Money
    fees: Money
    tax: Money
    trade_date: date


class Dividend(BaseModel):
    """A dividend event. `net` is what reduces adjusted cost (cash) or was reinvested."""

    account_id: str
    symbol: str
    date: date
    type: DividendType
    gross: Money
    withholding: Money
    net: Money
    reinvest_shares: Money | None = None
    reinvest_price: Money | None = None


class FXConversion(BaseModel):
    """An actual currency conversion (primarily consumed by sub-project ② forex)."""

    account_id: str
    date: date
    from_ccy: Currency
    from_amount: Money
    to_ccy: Currency
    to_amount: Money


class OpeningInventory(BaseModel):
    """A pre-existing position seeded at a build date (not a trade flow; feeds XIRR)."""

    account_id: str
    symbol: str
    shares: Money
    original_avg_cost: Money
    original_cost_total: Money
    build_date: date
