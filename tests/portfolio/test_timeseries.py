from datetime import date
from decimal import Decimal

from portfolio_dash.portfolio.timeseries import daily_value_series
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import Dividend, OpeningInventory, Transaction

USD = Currency.USD
TWD = Currency.TWD

INSTRUMENTS = {
    "AAA": Instrument(symbol="AAA", market=Market.US, quote_ccy=USD,
                      sector="Tech", name="AAA Corp"),
    "BBB": Instrument(symbol="BBB", market=Market.TW, quote_ccy=TWD,
                      sector="Semis", name="BBB Corp", board="TWSE"),
}


def _tx(day: date, side: Side, qty: str, price: str, fees: str = "1",
        symbol: str = "AAA") -> Transaction:
    return Transaction(account_id="schwab", symbol=symbol, side=side,
                       quantity=Decimal(qty), price=Decimal(price),
                       fees=Decimal(fees), tax=Decimal("0"), trade_date=day)


def test_carry_forward_values_and_net_invested() -> None:
    txs = [_tx(date(2026, 6, 1), Side.BUY, "10", "100")]
    prices = {"AAA": [(date(2026, 6, 1), Decimal("100")),
                      (date(2026, 6, 3), Decimal("110"))]}
    fx = {(USD, TWD): [(date(2026, 6, 1), Decimal("30"))]}
    series = daily_value_series(txs, [], [], INSTRUMENTS, prices, fx, TWD,
                                end=date(2026, 6, 4))
    assert series.available is True
    assert [p.date for p in series.points] == [
        date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3), date(2026, 6, 4)]
    assert [p.total_value for p in series.points] == [
        Decimal("30000"), Decimal("30000"), Decimal("33000"), Decimal("33000")]
    # net invested = (10*100 + 1 fee) * 30 on every day after the buy
    assert all(p.net_invested == Decimal("30030") for p in series.points)
    assert all(p.incomplete is False for p in series.points)


def test_missing_early_price_flags_incomplete() -> None:
    txs = [_tx(date(2026, 6, 1), Side.BUY, "10", "100")]
    prices = {"AAA": [(date(2026, 6, 2), Decimal("100"))]}  # nothing on day 1
    fx = {(USD, TWD): [(date(2026, 6, 1), Decimal("30"))]}
    series = daily_value_series(txs, [], [], INSTRUMENTS, prices, fx, TWD,
                                end=date(2026, 6, 2))
    assert series.points[0].incomplete is True
    assert series.points[0].total_value == Decimal("0")
    assert series.points[1].incomplete is False
    assert series.points[1].total_value == Decimal("30000")


def test_inverse_pair_fallback() -> None:
    txs = [_tx(date(2026, 6, 1), Side.BUY, "10", "100", fees="0")]
    prices = {"AAA": [(date(2026, 6, 1), Decimal("100"))]}
    fx = {(TWD, USD): [(date(2026, 6, 1), Decimal("0.03125"))]}  # 1/0.03125 = 32
    series = daily_value_series(txs, [], [], INSTRUMENTS, prices, fx, TWD,
                                end=date(2026, 6, 1))
    assert series.available is True
    assert series.points[0].total_value == Decimal("32000")
    assert series.points[0].net_invested == Decimal("32000")


def test_dividend_and_sell_reduce_net_invested() -> None:
    txs = [_tx(date(2026, 6, 1), Side.BUY, "10", "100"),
           _tx(date(2026, 6, 3), Side.SELL, "5", "120")]
    divs = [Dividend(account_id="schwab", symbol="AAA", date=date(2026, 6, 2),
                     type=DividendType.CASH, gross=Decimal("50"),
                     withholding=Decimal("0"), net=Decimal("50"))]
    prices = {"AAA": [(date(2026, 6, 1), Decimal("100")),
                      (date(2026, 6, 3), Decimal("120"))]}
    fx = {(USD, TWD): [(date(2026, 6, 1), Decimal("30"))]}
    series = daily_value_series(txs, divs, [], INSTRUMENTS, prices, fx, TWD,
                                end=date(2026, 6, 3))
    # day1: +1001*30 = 30030 ; day2: -50*30 -> 28530 ; day3: -(600-1)*30 -> 10560
    assert [p.net_invested for p in series.points] == [
        Decimal("30030"), Decimal("28530"), Decimal("10560")]
    assert series.points[2].total_value == Decimal("18000")  # 5 sh * 120 * 30


def test_opening_inventory_counts_as_invested() -> None:
    opening = [OpeningInventory(account_id="tw_broker", symbol="BBB",
                                shares=Decimal("10"),
                                original_cost_total=Decimal("900"),
                                build_date=date(2026, 6, 1))]
    prices = {"BBB": [(date(2026, 6, 1), Decimal("100"))]}
    series = daily_value_series([], [], opening, INSTRUMENTS, prices, {}, TWD,
                                end=date(2026, 6, 1))
    assert series.available is True  # TWD->TWD needs no FX rows
    assert series.points[0].total_value == Decimal("1000")
    assert series.points[0].net_invested == Decimal("900")


def test_missing_flow_fx_makes_series_unavailable() -> None:
    txs = [_tx(date(2026, 6, 1), Side.BUY, "10", "100")]
    prices = {"AAA": [(date(2026, 6, 1), Decimal("100"))]}
    series = daily_value_series(txs, [], [], INSTRUMENTS, prices, {}, TWD,
                                end=date(2026, 6, 2))
    assert series.available is False
    assert series.points == []


def test_empty_ledgers_unavailable() -> None:
    series = daily_value_series([], [], [], INSTRUMENTS, {}, {}, TWD,
                                end=date(2026, 6, 1))
    assert series.available is False
    assert series.points == []
