"""Realized + unrealized FX P&L per account, and the reporting-currency rollup."""

from collections.abc import Callable, Sequence
from decimal import Decimal

from portfolio_dash.forex.pools import MovementRow, acquisition_basis, foreign_cash_balance
from portfolio_dash.forex.results import AccountFXResult, FxRealizedRow, FXSummary
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.fx import convert
from portfolio_dash.shared.models.assets import Account, Instrument
from portfolio_dash.shared.models.ledger import Dividend, FXConversion, Transaction

_ZERO = Decimal("0")
_ONE = Decimal("1")
SpotRate = Callable[[Currency, Currency], Decimal]


def realized_fx_rows(
    conversions: list[FXConversion], home: Currency, foreign: Currency,
    avg_rate: Decimal | None,
) -> list[FxRealizedRow]:
    """Per-reconversion realized FX rows (foreign -> home). Empty if no avg_rate.

    For each conversion from foreign to home, the realized gain is the cost-basis
    formula ``home_received - (foreign_sold * avg_rate)`` — deliberately NOT
    shared.fx.convert, since avg_rate is a derived pool rate, not a market spot.
    """
    if avg_rate is None:
        return []
    out: list[FxRealizedRow] = []
    for c in conversions:
        if c.from_ccy == foreign and c.to_ccy == home:
            out.append(FxRealizedRow(
                date=c.date, foreign_ccy=foreign, home_ccy=home,
                foreign_sold=c.from_amount, home_received=c.to_amount,
                rate_used=avg_rate,
                realized=c.to_amount - c.from_amount * avg_rate,
            ))
    return out


def _realized_fx(
    conversions: list[FXConversion], home: Currency, foreign: Currency, avg_rate: Decimal | None
) -> Decimal | None:
    """Sum realized FX P&L over reconversions (foreign -> home).

    Returns None if avg_rate is None (no FX cost basis established).
    """
    if avg_rate is None:
        return None
    total: Decimal = sum(
        (r.realized for r in realized_fx_rows(conversions, home, foreign, avg_rate)), _ZERO
    )
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
    movements: Sequence[MovementRow] | None = None,
) -> AccountFXResult:
    """FX P&L for one account (ledgers already scoped to it).

    ``foreign_stock_value`` is the current market value of equity holdings in the
    foreign currency (supplied by the portfolio core).
    ``spot`` is the current foreign->home exchange rate (None if unavailable).
    ``movements`` are the account's cash movements; foreign-currency ones now fund the
    pool and (when they carry ``acq_home_amount``) its cost basis — spec 2026-07-30.

    Unrealized figures are None when avg_rate is None (no basis at all) or spot is None.
    Realized figures are None when avg_rate is None; zero if no reconversions occurred.
    """
    home = account.funding_ccy
    basis = acquisition_basis(conversions, movements or [], home, foreign)
    avg_rate = basis.avg_rate
    ratio = basis.covered_ratio
    foreign_cash = foreign_cash_balance(
        transactions, dividends, conversions, instruments, foreign, movements=movements)
    realized = _realized_fx(conversions, home, foreign, avg_rate)

    # F2/F3: one ratio, applied to the WHOLE foreign exposure. Cash is fungible, so an
    # outflow draws proportionally from the basis-known and basis-unknown parts; and the
    # stock leg is scaled too because ``avg_rate`` itself came from the covered population.
    # ``ratio == _ONE`` skips the multiply so a fully-covered pool (every ledger written
    # before this spec) yields Decimals byte-identical to the previous engine.
    unreal_total: Decimal | None
    if avg_rate is None or spot is None:
        unreal_stocks: Decimal | None = None
        unreal_cash: Decimal | None = None
        unreal_total = None
    else:
        cash_base = foreign_cash if ratio == _ONE else foreign_cash * ratio
        stock_base = foreign_stock_value if ratio == _ONE else foreign_stock_value * ratio
        unreal_stocks = stock_base * (spot - avg_rate)
        unreal_cash = cash_base * (spot - avg_rate)
        # Combined unrealized FX computed with Decimal at the source, so the wire carries
        # the sum as a Decimal string and the frontend never re-adds the two components.
        # None whenever either component is None (they are always both-or-neither here).
        unreal_total = unreal_stocks + unreal_cash

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
        unrealized_fx_total=unreal_total,
        # Derived-on-the-server display values (the frontend computes neither).
        spot_delta=(spot - avg_rate if spot is not None and avg_rate is not None else None),
        covered_ratio=ratio,
        fx_basis_gap=(_ZERO if ratio == _ONE else foreign_cash * (_ONE - ratio)),
        fx_basis_incomplete=ratio != _ONE,
        foreign_cash_negative=foreign_cash < _ZERO,
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
    movements: Sequence[MovementRow] | None = None,
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
        moves = [m for m in (movements or []) if m.account_id == account_id]
        try:
            spot: Decimal | None = current_spot(foreign, home)
        except KeyError:
            spot = None
        result = compute_account_fx(
            account, foreign, stock_value, txs, divs, convs, instruments, spot, moves
        )
        by_account[account_id] = result
        # home==reporting short-circuits to the identity rate so the rollup can't be broken
        # by an incomplete current_spot. Otherwise home->reporting is intentionally
        # unguarded (see docstring): a missing reporting rate is a config error, unlike the
        # foreign->home spot which may legitimately lag.
        to_reporting = Decimal("1") if home == reporting else current_spot(home, reporting)
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
