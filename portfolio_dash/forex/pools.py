"""Per-account FX pool: weighted-avg acquisition rate and foreign cash reconstruction.

Inputs are already scoped to a single account (the caller filters by account_id).

Two distinct "cash" definitions live in this codebase — do not conflate them (audit C9):

* **FX-exposure view** (this module, :func:`foreign_cash_balance`): the foreign-currency
  balance whose home-currency cost basis drives 換匯損益 (realized/unrealized FX P&L). It
  reconstructs the FOREIGN pool from conversions + foreign trades + foreign cash dividends.
  Its purpose is attribution of currency gain/loss against the weighted-avg acquisition
  rate — NOT operational cash tracking.

* **Funds view** (``portfolio/cash.py`` :func:`cash_balances`): the operational cash pool
  per (account, currency) that the 資金管理 page shows — deposits/withdrawals/openings +
  fx legs + trade settlements + cash dividends. It answers "how much cash is in this
  account right now" and drives the overdraft guard; it feeds NO return metric.

They diverge whenever cash enters/leaves a pool WITHOUT an FX conversion — e.g. a plain
deposit, a same-currency trade, or a cash dividend. The funds view moves; the FX-exposure
view (which only tracks foreign amounts acquired via conversion) may not. Keep them
separate: one is for FX P&L attribution, the other for cash operations.
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
