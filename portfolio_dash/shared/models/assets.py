"""Account and Instrument models."""

from decimal import Decimal

from pydantic import BaseModel

from portfolio_dash.shared.enums import Currency, Market


class Account(BaseModel):
    """A broker account (first-class entity; fee/dividend rules bind here)."""

    account_id: str
    name: str
    broker: str
    settlement_ccy: Currency
    funding_ccy: Currency
    dividend_model: str  # DB truth; per-account dividend rule (e.g. drip_us, cash)


class Instrument(BaseModel):
    """A tradable instrument; knows its market and quote currency."""

    symbol: str
    market: Market
    quote_ccy: Currency
    sector: str
    name: str
    board: str = ""  # "TWSE" | "TPEx" | ".KL" | "" (US / unresolved)
    target_low: Decimal | None = None  # price-alert floor (spec 10)
    target_high: Decimal | None = None  # price-alert ceiling (FU-D28)
    is_etf: bool = False  # single source of truth for ETF (never derive from sector)
    archived: bool = False  # FU-D13: stop-tracking flag; stays registered, off fetch scopes
