from datetime import date
from decimal import Decimal

from portfolio_dash.data_ingestion.store import StoredCashMovement
from portfolio_dash.forex.pools import (
    acquisition_basis,
    average_acquisition_rate,
    foreign_cash_balance,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import Dividend, FXConversion, Transaction

AAPL = Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                  sector="Tech", name="Apple")
INSTR = {"AAPL": AAPL}


def _conv(frm: Currency, famt: str, to: Currency, tamt: str, d: date) -> FXConversion:
    return FXConversion(account_id="schwab", date=d, from_ccy=frm, from_amount=Decimal(famt),
                        to_ccy=to, to_amount=Decimal(tamt))


def _move(kind: str, ccy: Currency, amt: str, acq: str | None = None,
          d: date = date(2025, 1, 1)) -> StoredCashMovement:
    return StoredCashMovement(
        id=0, account_id="schwab", date=d, kind=kind, ccy=ccy, amount=Decimal(amt),
        acq_home_amount=None if acq is None else Decimal(acq))


def test_average_acquisition_rate_weighted() -> None:
    convs = [_conv(Currency.TWD, "320000", Currency.USD, "10000", date(2025, 1, 1)),
             _conv(Currency.TWD, "330000", Currency.USD, "10000", date(2025, 2, 1))]
    assert average_acquisition_rate(convs, Currency.TWD, Currency.USD) == Decimal("32.5")


def test_average_acquisition_rate_none_when_no_conversions() -> None:
    assert average_acquisition_rate([], Currency.TWD, Currency.USD) is None


def test_foreign_cash_balance_reconstruction() -> None:
    convs = [_conv(Currency.TWD, "320000", Currency.USD, "10000", date(2025, 1, 1)),
             _conv(Currency.USD, "1000", Currency.TWD, "33000", date(2025, 6, 1))]
    txs = [
        Transaction(
            account_id="schwab", symbol="AAPL", side=Side.BUY, quantity=Decimal("90"),
            price=Decimal("100"), fees=Decimal("0"), tax=Decimal("0"),
            trade_date=date(2025, 1, 2),
        ),
        Transaction(
            account_id="schwab", symbol="AAPL", side=Side.SELL, quantity=Decimal("10"),
            price=Decimal("110"), fees=Decimal("0"), tax=Decimal("0"),
            trade_date=date(2025, 5, 1),
        ),
    ]
    divs = [Dividend(account_id="schwab", symbol="AAPL", date=date(2025, 3, 1),
                     type=DividendType.CASH, gross=Decimal("50"), withholding=Decimal("0"),
                     net=Decimal("50"))]
    # +10000 -9000 +50 +1100 -1000 = 1150
    assert foreign_cash_balance(txs, divs, convs, INSTR, Currency.USD) == Decimal("1150")


def test_foreign_cash_ignores_drip_dividends() -> None:
    convs = [_conv(Currency.TWD, "320000", Currency.USD, "10000", date(2025, 1, 1))]
    divs = [Dividend(
        account_id="schwab", symbol="AAPL", date=date(2025, 3, 1),
        type=DividendType.DRIP, gross=Decimal("100"), withholding=Decimal("30"),
        net=Decimal("70"), reinvest_shares=Decimal("0.5"), reinvest_price=Decimal("140"),
    )]
    assert foreign_cash_balance([], divs, convs, INSTR, Currency.USD) == Decimal("10000")


# --- spec 2026-07-30: foreign cash movements fund the pool and carry its cost basis ----


def test_foreign_opening_with_cost_joins_balance_and_average() -> None:
    """A rated foreign opening funds the pool AND moves the weighted average (F1)."""
    convs = [_conv(Currency.TWD, "2049000", Currency.USD, "63572.99", date(2026, 7, 1))]
    moves = [_move("OPENING", Currency.USD, "100000", "3135870")]
    assert foreign_cash_balance(
        [], [], convs, INSTR, Currency.USD, movements=moves) == Decimal("163572.99")
    basis = acquisition_basis(convs, moves, Currency.TWD, Currency.USD)
    assert basis.covered_ratio == Decimal("1")
    # (2,049,000 + 3,135,870) / 163,572.99
    assert basis.avg_rate == Decimal("5184870") / Decimal("163572.99")


def test_foreign_opening_without_cost_funds_balance_but_not_average() -> None:
    """No cost recorded -> the amount is still real cash, but the rate is never guessed."""
    convs = [_conv(Currency.TWD, "2049000", Currency.USD, "63572.99", date(2026, 7, 1))]
    moves = [_move("OPENING", Currency.USD, "100000")]
    assert foreign_cash_balance(
        [], [], convs, INSTR, Currency.USD, movements=moves) == Decimal("163572.99")
    basis = acquisition_basis(convs, moves, Currency.TWD, Currency.USD)
    assert basis.avg_rate == Decimal("2049000") / Decimal("63572.99")  # unchanged
    assert basis.covered_ratio == Decimal("63572.99") / Decimal("163572.99")


def test_covered_ratio_is_exactly_one_without_foreign_movements() -> None:
    """Pre-spec ledgers must be untouched — the ratio is the literal 1, so the caller
    can skip the multiply and reproduce byte-identical Decimals."""
    convs = [_conv(Currency.TWD, "320000", Currency.USD, "10000", date(2025, 1, 1))]
    assert acquisition_basis(convs, [], Currency.TWD, Currency.USD).covered_ratio \
        == Decimal("1")
    assert acquisition_basis([], [], Currency.TWD, Currency.USD).covered_ratio \
        == Decimal("1")


def test_home_currency_movement_never_enters_the_foreign_pool() -> None:
    """A TWD deposit into a TWD-funded account is not foreign exposure."""
    convs = [_conv(Currency.TWD, "320000", Currency.USD, "10000", date(2025, 1, 1))]
    moves = [_move("DEPOSIT", Currency.TWD, "500000")]
    assert foreign_cash_balance(
        [], [], convs, INSTR, Currency.USD, movements=moves) == Decimal("10000")
    assert acquisition_basis(convs, moves, Currency.TWD, Currency.USD).covered_ratio \
        == Decimal("1")


def test_foreign_withdraw_debits_balance_but_not_the_basis() -> None:
    """N1: a disposal reduces exposure; under weighted average it changes neither the
    average nor the coverage, and it recognises no realized FX (that needs a conversion)."""
    convs = [_conv(Currency.TWD, "320000", Currency.USD, "10000", date(2025, 1, 1))]
    moves = [_move("WITHDRAW", Currency.USD, "2500")]
    assert foreign_cash_balance(
        [], [], convs, INSTR, Currency.USD, movements=moves) == Decimal("7500")
    basis = acquisition_basis(convs, moves, Currency.TWD, Currency.USD)
    assert basis.avg_rate == Decimal("32")
    assert basis.covered_ratio == Decimal("1")


def test_average_from_a_rated_movement_alone_without_any_conversion() -> None:
    """An account funded ONLY by a rated foreign deposit still has a cost basis —
    it must not fall back to None just because `conversions` is empty."""
    moves = [_move("DEPOSIT", Currency.USD, "1000", "31500")]
    assert average_acquisition_rate(
        [], Currency.TWD, Currency.USD, movements=moves) == Decimal("31.5")
