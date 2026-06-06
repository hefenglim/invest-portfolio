"""Per-account FX pool: weighted-avg acquisition rate and foreign cash reconstruction.

Inputs are already scoped to a single account (the caller filters by account_id).
"""

from decimal import Decimal

from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import Dividend, FXConversion, Transaction

_ZERO = Decimal("0")


def average_acquisition_rate(
    conversions: list[FXConversion], home: Currency, foreign: Currency
) -> Decimal | None:
    """Weighted-average home-per-foreign rate over home->foreign conversions.

    Returns None if the account has no such conversions (no FX cost basis).
    """
    total_home = _ZERO
    total_foreign = _ZERO
    for c in conversions:
        if c.from_ccy == home and c.to_ccy == foreign:
            total_home += c.from_amount
            total_foreign += c.to_amount
    if total_foreign == _ZERO:
        return None
    return total_home / total_foreign


def foreign_cash_balance(
    transactions: list[Transaction],
    dividends: list[Dividend],
    conversions: list[FXConversion],
    instruments: dict[str, Instrument],
    foreign: Currency,
) -> Decimal:
    """Reconstruct the foreign-currency cash balance from the account's ledgers.

    + conversions into foreign, + foreign sale net proceeds, + foreign CASH dividends net,
    - foreign buys (incl. fees+tax), - reconversions out of foreign. DRIP/STOCK dividends
    move no cash (DRIP nets to zero) and are excluded.
    """
    cash = _ZERO
    for c in conversions:
        if c.to_ccy == foreign:
            cash += c.to_amount
        if c.from_ccy == foreign:
            cash -= c.from_amount
    for t in transactions:
        if instruments[t.symbol].quote_ccy != foreign:
            continue
        if t.side is Side.BUY:
            cash -= t.quantity * t.price + t.fees + t.tax
        else:
            cash += t.quantity * t.price - t.fees - t.tax
    for d in dividends:
        if d.type is DividendType.CASH and instruments[d.symbol].quote_ccy == foreign:
            cash += d.net
    return cash
