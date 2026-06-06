from datetime import date
from decimal import Decimal

from portfolio_dash.forex.fx_pnl import compute_account_fx
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Account, Instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.models.ledger import FXConversion, Transaction

SCHWAB = Account(account_id="schwab", name="Schwab", broker="Schwab",
                 settlement_ccy=Currency.USD, funding_ccy=Currency.TWD)
AAPL = Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                  sector="Tech", name="Apple")
INSTR = {"AAPL": AAPL}


def _buy(qty: str, price: str, d: date) -> Transaction:
    return Transaction(account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal(qty), price=Decimal(price), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=d)


def test_compute_account_fx_unrealized_split() -> None:
    convs = [FXConversion(account_id="schwab", date=date(2025, 1, 1), from_ccy=Currency.TWD,
                          from_amount=Decimal("320000"), to_ccy=Currency.USD,
                          to_amount=Decimal("10000"))]
    txs = [_buy("90", "100", date(2025, 1, 2))]  # spends 9000 USD -> cash 1000
    r = compute_account_fx(SCHWAB, Currency.USD, Decimal("10800"), txs, [], convs, INSTR,
                           spot=Decimal("33"))
    assert r.avg_rate == Decimal("32")
    assert r.foreign_cash == Decimal("1000")
    assert r.realized_fx == Decimal("0")
    assert r.unrealized_fx_stocks == Decimal("10800")
    assert r.unrealized_fx_cash == Decimal("1000")


def test_compute_account_fx_realized_on_reconversion() -> None:
    convs = [
        FXConversion(account_id="schwab", date=date(2025, 1, 1), from_ccy=Currency.TWD,
                     from_amount=Decimal("320000"), to_ccy=Currency.USD,
                     to_amount=Decimal("10000")),
        FXConversion(account_id="schwab", date=date(2025, 6, 1), from_ccy=Currency.USD,
                     from_amount=Decimal("5000"), to_ccy=Currency.TWD,
                     to_amount=Decimal("167500")),
    ]
    r = compute_account_fx(SCHWAB, Currency.USD, Decimal("0"), [], [], convs, INSTR,
                           spot=Decimal("33"))
    # realized = 167500 - 5000 * 32 = 7500
    assert r.realized_fx == Decimal("7500")


def test_compute_account_fx_no_conversions_all_none() -> None:
    r = compute_account_fx(SCHWAB, Currency.USD, Decimal("1000"), [], [], [], INSTR,
                           spot=Decimal("33"))
    assert r.avg_rate is None
    assert r.realized_fx is None
    assert r.unrealized_fx_stocks is None
    assert r.unrealized_fx_cash is None


def test_compute_account_fx_missing_spot_unrealized_none_realized_ok() -> None:
    convs = [FXConversion(account_id="schwab", date=date(2025, 1, 1), from_ccy=Currency.TWD,
                          from_amount=Decimal("320000"), to_ccy=Currency.USD,
                          to_amount=Decimal("10000"))]
    r = compute_account_fx(SCHWAB, Currency.USD, Decimal("10000"), [], [], convs, INSTR,
                           spot=None)
    assert r.realized_fx == Decimal("0")
    assert r.unrealized_fx_stocks is None
    assert r.unrealized_fx_cash is None


def test_compute_account_fx_fx_loss_when_spot_below_avg() -> None:
    convs = [FXConversion(account_id="schwab", date=date(2025, 1, 1), from_ccy=Currency.TWD,
                          from_amount=Decimal("320000"), to_ccy=Currency.USD,
                          to_amount=Decimal("10000"))]
    # spot 31 < avg_rate 32 -> FX loss on both stocks and cash
    r = compute_account_fx(SCHWAB, Currency.USD, Decimal("9000"), [], [], convs, INSTR,
                           spot=Decimal("31"))
    assert r.avg_rate == Decimal("32")
    assert r.unrealized_fx_stocks == Decimal("-9000")   # 9000 * (31-32)
    assert r.unrealized_fx_cash == Decimal("-10000")    # 10000 * (31-32)


def test_compute_account_fx_two_rates_blended_then_reconversion() -> None:
    convs = [
        FXConversion(account_id="schwab", date=date(2025, 1, 1), from_ccy=Currency.TWD,
                     from_amount=Decimal("320000"), to_ccy=Currency.USD, to_amount=Decimal("10000")),
        FXConversion(account_id="schwab", date=date(2025, 2, 1), from_ccy=Currency.TWD,
                     from_amount=Decimal("330000"), to_ccy=Currency.USD, to_amount=Decimal("10000")),
        FXConversion(account_id="schwab", date=date(2025, 6, 1), from_ccy=Currency.USD,
                     from_amount=Decimal("5000"), to_ccy=Currency.TWD, to_amount=Decimal("165000")),
    ]
    r = compute_account_fx(SCHWAB, Currency.USD, Decimal("0"), [], [], convs, INSTR,
                           spot=Decimal("33"))
    # avg_rate = (320000+330000)/(10000+10000) = 32.5; realized = 165000 - 5000*32.5 = 2500
    assert r.avg_rate == Decimal("32.5")
    assert r.realized_fx == Decimal("2500")
