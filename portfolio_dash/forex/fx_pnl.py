"""Realized + unrealized FX P&L per account, and the reporting-currency rollup."""

from collections.abc import Callable
from decimal import Decimal

from portfolio_dash.forex.pools import average_acquisition_rate, foreign_cash_balance
from portfolio_dash.forex.results import AccountFXResult, FXSummary
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.fx import convert
from portfolio_dash.shared.models.assets import Account, Instrument
from portfolio_dash.shared.models.ledger import Dividend, FXConversion, Transaction

_ZERO = Decimal("0")
SpotRate = Callable[[Currency, Currency], Decimal]


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


def compute_fx_summary(
    accounts: dict[str, Account],
    instruments: dict[str, Instrument],
    transactions: list[Transaction],
    dividends: list[Dividend],
    fx_conversions: list[FXConversion],
    foreign_exposure: dict[str, tuple[Currency, Decimal]],
    current_spot: SpotRate,
    reporting: Currency,
) -> FXSummary:
    """FX P&L for every FX-exposed account + reporting rollup.

    ``foreign_exposure`` maps account_id -> (foreign_ccy, foreign stock market value in
    that foreign ccy), supplied by the orchestrator from the portfolio core's valued
    holdings. Only accounts present in ``foreign_exposure`` are processed.

    ``current_spot(x, x)`` must return ``Decimal("1")`` (identity rate). The foreign->home
    spot is allowed to be missing (degrades to ``None`` unrealized), but the home->reporting
    rate is assumed always resolvable — a missing reporting rate is a configuration error
    (the orchestrator must cover the single reporting currency), so it is allowed to raise.
    """
    by_account: dict[str, AccountFXResult] = {}
    rep_realized = _ZERO
    rep_unrealized = _ZERO
    for account_id, (foreign, stock_value) in foreign_exposure.items():
        account = accounts[account_id]
        home = account.funding_ccy
        txs = [t for t in transactions if t.account_id == account_id]
        divs = [d for d in dividends if d.account_id == account_id]
        convs = [c for c in fx_conversions if c.account_id == account_id]
        try:
            spot: Decimal | None = current_spot(foreign, home)
        except KeyError:
            spot = None
        result = compute_account_fx(
            account, foreign, stock_value, txs, divs, convs, instruments, spot
        )
        by_account[account_id] = result
        # home->reporting is intentionally unguarded (see docstring): a missing reporting
        # rate is a config error, unlike the foreign->home spot which may legitimately lag.
        to_reporting = current_spot(home, reporting)
        if result.realized_fx is not None:
            rep_realized += convert(result.realized_fx, to_reporting)
        if result.unrealized_fx_stocks is not None and result.unrealized_fx_cash is not None:
            rep_unrealized += convert(
                result.unrealized_fx_stocks + result.unrealized_fx_cash, to_reporting
            )
    return FXSummary(
        by_account=by_account,
        reporting_currency=reporting,
        reporting_realized_fx=rep_realized,
        reporting_unrealized_fx=rep_unrealized,
    )
