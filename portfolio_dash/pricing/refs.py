from pydantic import BaseModel

from portfolio_dash.shared.enums import Currency, Market


class InstrumentRef(BaseModel, frozen=True):
    symbol: str
    market: Market
    board: str = ""  # "TWSE" | "TPEx" | ".KL" | "" (US)


class FxPair(BaseModel, frozen=True):
    base: Currency
    quote: Currency
