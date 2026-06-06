"""Realized + unrealized FX P&L per account, and the reporting-currency rollup."""

from decimal import Decimal

from portfolio_dash.forex.pools import average_acquisition_rate, foreign_cash_balance
from portfolio_dash.forex.results import AccountFXResult
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.assets import Account, Instrument
from portfolio_dash.shared.models.ledger import Dividend, FXConversion, Transaction

_ZERO = Decimal("0")


def _realized_fx(
    conversions: list[FXConversion], home: Currency, foreign: Currency, avg_rate: Decimal | None
) -> Decimal | None:
    """Sum realized FX P&L over reconversions (foreign -> home).

    For each conversion from foreign to home:
        gain = home_received - (foreign_sold * avg_rate)

    Returns None if avg_rate is None (no FX cost basis established).
    """
    if avg_rate is None:
        return None
    total = _ZERO
    for c in conversions:
        if c.from_ccy == foreign and c.to_ccy == home:
            total += c.to_amount - c.from_amount * avg_rate
    return total


def compute_account_fx(
    account: Account,
    foreign: Currency,
    foreign_stock_value: Decimal,
    transactions: list[Transaction],
    dividends: list[Dividend],
    conversions: list[FXConversion],
    instruments: dict[str, Instrument],
    spot: Decimal | None,
) -> AccountFXResult:
    """FX P&L for one account (ledgers already scoped to it).

    ``foreign_stock_value`` is the current market value of equity holdings in the
    foreign currency (supplied by the portfolio core).
    ``spot`` is the current foreign->home exchange rate (None if unavailable).

    Unrealized figures are None when avg_rate is None (no conversions) or spot is None.
    Realized figures are None when avg_rate is None; zero if no reconversions occurred.
    """
    home = account.funding_ccy
    avg_rate = average_acquisition_rate(conversions, home, foreign)
    foreign_cash = foreign_cash_balance(transactions, dividends, conversions, instruments, foreign)
    realized = _realized_fx(conversions, home, foreign, avg_rate)

    if avg_rate is None or spot is None:
        unreal_stocks: Decimal | None = None
        unreal_cash: Decimal | None = None
    else:
        unreal_stocks = foreign_stock_value * (spot - avg_rate)
        unreal_cash = foreign_cash * (spot - avg_rate)

    return AccountFXResult(
        account_id=account.account_id,
        home_ccy=home,
        foreign_ccy=foreign,
        avg_rate=avg_rate,
        current_spot=spot,
        foreign_cash=foreign_cash,
        foreign_stock_value=foreign_stock_value,
        realized_fx=realized,
        unrealized_fx_stocks=unreal_stocks,
        unrealized_fx_cash=unreal_cash,
    )
