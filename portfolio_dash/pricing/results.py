from datetime import date, datetime

from pydantic import BaseModel, Field

from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.types import Money


class PriceRow(BaseModel):
    instrument: str
    market: Market
    as_of: date
    close: Money
    open: Money | None = None
    high: Money | None = None
    low: Money | None = None
    volume: Money | None = None
    source: str


class FxRow(BaseModel):
    base: Currency
    quote: Currency
    as_of: date
    rate: Money
    source: str


class DividendEvent(BaseModel):
    instrument: str
    market: Market
    ex_date: date
    pay_date: date | None = None
    cash_amount: Money | None = None
    stock_amount: Money | None = None
    currency: Currency | None = None
    source: str


class PriceRead(BaseModel):
    value: Money
    as_of: date
    source: str
    stale: bool


class FxRead(BaseModel):
    rate: Money
    as_of: date
    source: str
    stale: bool


class RefreshSummary(BaseModel):
    ok: dict[str, str] = Field(default_factory=dict)  # key -> winning source
    failed: list[str] = Field(default_factory=list)  # keys with no data
    fetched_at: datetime
