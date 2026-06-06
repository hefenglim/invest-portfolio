"""Domain enums for ledger entries."""

from enum import StrEnum


class Side(StrEnum):
    """Transaction side."""

    BUY = "BUY"
    SELL = "SELL"


class DividendType(StrEnum):
    """Dividend mechanism: cash payout, stock dividend (配股), or DRIP reinvest."""

    CASH = "CASH"
    STOCK = "STOCK"
    DRIP = "DRIP"
