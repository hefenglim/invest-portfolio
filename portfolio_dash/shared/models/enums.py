"""Domain enums for ledger entries."""

from enum import StrEnum


class Side(StrEnum):
    """Transaction side."""

    BUY = "BUY"
    SELL = "SELL"


class DividendType(StrEnum):
    """Dividend mechanism: cash payout, stock dividend (配股), DRIP reinvest, or net-received."""

    CASH = "CASH"
    STOCK = "STOCK"
    DRIP = "DRIP"
    NET = "NET"


# The cash-money dividend family (domain-ledger.md: TW cash AND MY single-tier
# net both reduce adjusted cost and count as XIRR inflows). ONE definition for
# every replay site — cost basis, trend deltas, XIRR — so they can never drift
# (found 2026-07-03: NET fell into the shares-branch and crashed rebuilds).
CASH_DIVIDEND_TYPES = frozenset({DividendType.CASH, DividendType.NET})
