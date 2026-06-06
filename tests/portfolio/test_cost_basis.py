from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.portfolio.cost_basis import OversellError, build_book
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import (
    Dividend,
    OpeningInventory,
    Transaction,
)

TW = Instrument(symbol="2330.TW", market=Market.TW, quote_ccy=Currency.TWD,
                sector="Tech", name="TSMC")
US = Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                sector="Tech", name="Apple")
INSTR = {"2330.TW": TW, "AAPL": US}


def _buy(sym: str, qty: str, price: str, d: date, fees: str = "0", acc: str = "a") -> Transaction:
    return Transaction(account_id=acc, symbol=sym, side=Side.BUY, quantity=Decimal(qty),
                       price=Decimal(price), fees=Decimal(fees), tax=Decimal("0"), trade_date=d)


def _sell(
    sym: str, qty: str, price: str, d: date,
    fees: str = "0", tax: str = "0", acc: str = "a",
) -> Transaction:
    return Transaction(account_id=acc, symbol=sym, side=Side.SELL, quantity=Decimal(qty),
                       price=Decimal(price), fees=Decimal(fees), tax=Decimal(tax), trade_date=d)


def test_buys_weighted_average_includes_fees() -> None:
    txs = [_buy("AAPL", "10", "100", date(2025, 1, 1), fees="5"),
           _buy("AAPL", "10", "120", date(2025, 1, 2), fees="5")]
    book = build_book(txs, [], [], INSTR)
    h = book.holdings[0]
    assert h.shares == Decimal("20")
    assert h.original_cost_total == Decimal("2210")
    assert h.original_avg == Decimal("110.5")
    assert h.adjusted_avg == Decimal("110.5")
    assert book.gross_invested[Currency.USD] == Decimal("2210")


def test_opening_inventory_seeds_position() -> None:
    oi = OpeningInventory(account_id="a", symbol="2330.TW", shares=Decimal("1000"),
                          original_avg_cost=Decimal("500"), original_cost_total=Decimal("500000"),
                          build_date=date(2024, 12, 31))
    book = build_book([], [], [oi], INSTR)
    h = book.holdings[0]
    assert h.shares == Decimal("1000")
    assert h.original_cost_total == Decimal("500000")
    assert book.gross_invested[Currency.TWD] == Decimal("500000")


def test_sell_realized_vs_adjusted_and_reduces_shares() -> None:
    txs = [_buy("AAPL", "10", "100", date(2025, 1, 1)),
           _sell("AAPL", "4", "150", date(2025, 1, 3), fees="2")]
    book = build_book(txs, [], [], INSTR)
    assert book.realized.rows[0].realized == Decimal("198")
    assert book.realized.rows[0].original_cost_removed == Decimal("400")
    assert book.realized.rows[0].adjusted_cost_removed == Decimal("400")
    assert book.realized.by_currency[Currency.USD] == Decimal("198")
    h = book.holdings[0]
    assert h.shares == Decimal("6")
    assert h.original_cost_total == Decimal("600")


def test_oversell_raises() -> None:
    txs = [_buy("AAPL", "5", "100", date(2025, 1, 1)),
           _sell("AAPL", "6", "150", date(2025, 1, 2))]
    with pytest.raises(OversellError):
        build_book(txs, [], [], INSTR)


def test_cash_dividend_reduces_adjusted_and_split() -> None:
    txs = [_buy("2330.TW", "1000", "100", date(2025, 1, 1))]
    divs = [Dividend(account_id="a", symbol="2330.TW", date=date(2025, 6, 1),
                     type=DividendType.CASH, gross=Decimal("20000"),
                     withholding=Decimal("0"), net=Decimal("20000"))]
    book = build_book(txs, divs, [], INSTR)
    h = book.holdings[0]
    assert h.original_cost_total == Decimal("100000")
    assert h.adjusted_cost_total == Decimal("80000")
    assert h.adjusted_avg == Decimal("80")
    assert h.dividend_portion == Decimal("20000")
    assert h.payback_ratio == Decimal("0.2")


def test_adjusted_cost_may_go_negative() -> None:
    txs = [_buy("2330.TW", "1000", "10", date(2025, 1, 1))]
    divs = [Dividend(account_id="a", symbol="2330.TW", date=date(2025, 6, 1),
                     type=DividendType.CASH, gross=Decimal("12000"),
                     withholding=Decimal("0"), net=Decimal("12000"))]
    book = build_book(txs, divs, [], INSTR)
    assert book.holdings[0].adjusted_cost_total == Decimal("-2000")


def test_drip_adds_zero_cost_shares() -> None:
    txs = [_buy("AAPL", "10", "100", date(2025, 1, 1))]
    divs = [Dividend(account_id="a", symbol="AAPL", date=date(2025, 6, 1),
                     type=DividendType.DRIP, gross=Decimal("100"), withholding=Decimal("30"),
                     net=Decimal("70"), reinvest_shares=Decimal("0.5"),
                     reinvest_price=Decimal("140"))]
    book = build_book(txs, divs, [], INSTR)
    h = book.holdings[0]
    assert h.shares == Decimal("10.5")
    assert h.original_cost_total == Decimal("1000")
    assert h.adjusted_cost_total == Decimal("1000")


def test_stock_dividend_adds_shares_no_cost_change() -> None:
    txs = [_buy("2330.TW", "1000", "100", date(2025, 1, 1))]
    divs = [Dividend(account_id="a", symbol="2330.TW", date=date(2025, 6, 1),
                     type=DividendType.STOCK, gross=Decimal("0"), withholding=Decimal("0"),
                     net=Decimal("0"), reinvest_shares=Decimal("100"))]
    book = build_book(txs, divs, [], INSTR)
    h = book.holdings[0]
    assert h.shares == Decimal("1100")
    assert h.original_cost_total == Decimal("100000")


def test_fully_sold_position_excluded_from_holdings() -> None:
    txs = [_buy("AAPL", "10", "100", date(2025, 1, 1)),
           _sell("AAPL", "10", "150", date(2025, 1, 2))]
    book = build_book(txs, [], [], INSTR)
    assert book.holdings == []
    assert book.realized.by_currency[Currency.USD] == Decimal("500")


def test_equivalence_adjusted_total_equals_original_plus_dividends() -> None:
    txs = [_buy("2330.TW", "1000", "100", date(2025, 1, 1))]
    divs = [Dividend(account_id="a", symbol="2330.TW", date=date(2025, 6, 1),
                     type=DividendType.CASH, gross=Decimal("20000"),
                     withholding=Decimal("0"), net=Decimal("20000"))]
    book = build_book(txs, divs, [], INSTR)
    h = book.holdings[0]
    price = Decimal("110")
    adj_model = (price - h.adjusted_avg) * h.shares
    orig_model = (price - h.original_avg) * h.shares + Decimal("20000")
    assert adj_model == orig_model


def test_dividend_for_unknown_position_raises() -> None:
    # A dividend with no prior buy/opening must fail loud, not create a ghost position.
    divs = [Dividend(account_id="a", symbol="AAPL", date=date(2025, 6, 1),
                     type=DividendType.CASH, gross=Decimal("100"),
                     withholding=Decimal("0"), net=Decimal("100"))]
    with pytest.raises(ValueError, match="unknown position"):
        build_book([], divs, [], INSTR)


def test_multi_account_same_symbol_isolated() -> None:
    txs = [_buy("AAPL", "10", "100", date(2025, 1, 1), acc="schwab"),
           _buy("AAPL", "5", "200", date(2025, 1, 1), acc="moomoo"),
           _sell("AAPL", "4", "150", date(2025, 1, 2), acc="schwab")]
    book = build_book(txs, [], [], INSTR)
    by_acc = {h.account_id: h for h in book.holdings}
    assert by_acc["schwab"].shares == Decimal("6")        # 10 - 4, unaffected by moomoo
    assert by_acc["moomoo"].shares == Decimal("5")        # untouched by schwab's sell
    assert by_acc["moomoo"].original_cost_total == Decimal("1000")
    assert book.gross_invested[Currency.USD] == Decimal("2000")  # 1000 + 1000 (both buys)


def test_opening_inventory_plus_subsequent_buy_accumulates() -> None:
    oi = OpeningInventory(account_id="a", symbol="AAPL", shares=Decimal("10"),
                          original_avg_cost=Decimal("100"), original_cost_total=Decimal("1000"),
                          build_date=date(2024, 12, 31))
    txs = [_buy("AAPL", "10", "120", date(2025, 1, 2))]
    book = build_book(txs, [], [oi], INSTR)
    h = book.holdings[0]
    assert h.shares == Decimal("20")
    assert h.original_cost_total == Decimal("2200")  # 1000 + 1200, accumulated
    assert h.original_avg == Decimal("110")
    assert book.gross_invested[Currency.USD] == Decimal("2200")


def test_sell_then_rebuy_reuses_position() -> None:
    txs = [_buy("AAPL", "10", "100", date(2025, 1, 1)),
           _sell("AAPL", "10", "150", date(2025, 1, 2)),
           _buy("AAPL", "5", "200", date(2025, 1, 3))]
    book = build_book(txs, [], [], INSTR)
    h = book.holdings[0]
    assert h.shares == Decimal("5")
    assert h.original_cost_total == Decimal("1000")  # only the rebuy remains
    assert h.original_avg == Decimal("200")
    assert book.realized.by_currency[Currency.USD] == Decimal("500")  # from the full sell
    assert book.gross_invested[Currency.USD] == Decimal("2000")  # 1000 + 1000 both buys


def test_drip_without_reinvest_shares_raises() -> None:
    # A DRIP/stock dividend missing reinvest_shares must fail loud, not silently drop it.
    txs = [_buy("AAPL", "10", "100", date(2025, 1, 1))]
    divs = [Dividend(account_id="a", symbol="AAPL", date=date(2025, 6, 1),
                     type=DividendType.DRIP, gross=Decimal("100"),
                     withholding=Decimal("30"), net=Decimal("70"))]
    with pytest.raises(ValueError, match="requires reinvest_shares"):
        build_book(txs, divs, [], INSTR)
