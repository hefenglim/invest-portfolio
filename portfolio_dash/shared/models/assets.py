"""Account and Instrument models."""

from pydantic import BaseModel

from portfolio_dash.shared.enums import Currency, Market


class Account(BaseModel):
    """A broker account (first-class entity; fee/dividend rules bind here)."""

    account_id: str
    name: str
    broker: str
    settlement_ccy: Currency
    funding_ccy: Currency


class Instrument(BaseModel):
    """A tradable instrument; knows its market and quote currency."""

    symbol: str
    market: Market
    quote_ccy: Currency
    sector: str
    name: str
