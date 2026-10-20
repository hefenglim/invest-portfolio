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

#: Each market's EXCHANGE timezone (IANA key): the zone its trading dates are counted in. The
#: one owner of that fact (owner ruling 2026-10-20, the price-basis split edge). "Which splits
#: could a fetch already have seen?" is answered on the exchange's own calendar date
#: (``pricing.store.split_window_through``), never on the app clock's (Asia/Taipei): a US quote
#: fetched at 16:30 New York is already the NEXT day in Taipei. TW and MY share Taipei's UTC+8
#: with no DST, so their dates equal the app clock's. The market quote jobs' cron zones in
#: ``scheduler.jobs.JOBS`` hold the same values but answer another question (when a job runs,
#: an owner-editable schedule), so they are not derived from this map.
MARKET_TIMEZONE: dict[Market, str] = {
    Market.TW: "Asia/Taipei",
    Market.US: "America/New_York",
    Market.MY: "Asia/Kuala_Lumpur",
}
