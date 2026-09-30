"""Stable cross-cutting enums shared across all layers."""

from enum import StrEnum


class Currency(StrEnum):
    """Quote / settlement currencies handled by the system."""

    TWD = "TWD"
    USD = "USD"
    MYR = "MYR"


class Market(StrEnum):
    """Exchanges/markets where instruments trade."""

    US = "US"
    TW = "TW"
    MY = "MY"


#: A market's quote currency — what a trade there settles in (a US trade on Moomoo MY is USD).
MARKET_QUOTE_CCY: dict[Market, Currency] = {
    Market.TW: Currency.TWD,
    Market.US: Currency.USD,
    Market.MY: Currency.MYR,
}
