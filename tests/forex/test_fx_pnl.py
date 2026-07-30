from datetime import date
from decimal import Decimal

from portfolio_dash.data_ingestion.store import StoredCashMovement
from portfolio_dash.forex.fx_pnl import compute_account_fx
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Account, Instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.models.ledger import FXConversion, Transaction

SCHWAB = Account(account_id="schwab", name="Schwab", broker="Schwab",
                 settlement_ccy=Currency.USD, funding_ccy=Currency.TWD,
                 dividend_model="drip_us")
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
    # Combined unrealized FX is server-computed (stocks + cash), so the frontend never sums.
    assert r.unrealized_fx_total == Decimal("11800")
    assert r.unrealized_fx_total == r.unrealized_fx_stocks + r.unrealized_fx_cash


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
    assert r.unrealized_fx_total is None   # None when the components are None


def test_compute_account_fx_missing_spot_unrealized_none_realized_ok() -> None:
    convs = [FXConversion(account_id="schwab", date=date(2025, 1, 1), from_ccy=Currency.TWD,
                          from_amount=Decimal("320000"), to_ccy=Currency.USD,
                          to_amount=Decimal("10000"))]
    r = compute_account_fx(SCHWAB, Currency.USD, Decimal("10000"), [], [], convs, INSTR,
                           spot=None)
    assert r.realized_fx == Decimal("0")
    assert r.unrealized_fx_stocks is None
    assert r.unrealized_fx_cash is None
    assert r.unrealized_fx_total is None   # None when spot is missing (no unrealized)


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
    assert r.unrealized_fx_total == Decimal("-19000")   # server-computed sum (-9000 + -10000)


def test_compute_account_fx_two_rates_blended_then_reconversion() -> None:
    convs = [
        FXConversion(
            account_id="schwab", date=date(2025, 1, 1), from_ccy=Currency.TWD,
            from_amount=Decimal("320000"), to_ccy=Currency.USD, to_amount=Decimal("10000"),
        ),
        FXConversion(
            account_id="schwab", date=date(2025, 2, 1), from_ccy=Currency.TWD,
            from_amount=Decimal("330000"), to_ccy=Currency.USD, to_amount=Decimal("10000"),
        ),
        FXConversion(
            account_id="schwab", date=date(2025, 6, 1), from_ccy=Currency.USD,
            from_amount=Decimal("5000"), to_ccy=Currency.TWD, to_amount=Decimal("165000"),
        ),
    ]
    r = compute_account_fx(SCHWAB, Currency.USD, Decimal("0"), [], [], convs, INSTR,
                           spot=Decimal("33"))
    # avg_rate = (320000+330000)/(10000+10000) = 32.5; realized = 165000 - 5000*32.5 = 2500
    assert r.avg_rate == Decimal("32.5")
    assert r.realized_fx == Decimal("2500")


def test_realized_fx_rows_per_reconversion() -> None:
    from datetime import date
    from decimal import Decimal

    from portfolio_dash.forex.fx_pnl import realized_fx_rows
    from portfolio_dash.shared.enums import Currency
    from portfolio_dash.shared.models.ledger import FXConversion

    convs = [
        FXConversion(account_id="schwab", date=date(2026, 1, 8), from_ccy=Currency.TWD,
                     from_amount=Decimal("32000"), to_ccy=Currency.USD,
                     to_amount=Decimal("1000")),  # acquisition TWD->USD, avg 32
        FXConversion(account_id="schwab", date=date(2026, 5, 1), from_ccy=Currency.USD,
                     from_amount=Decimal("500"), to_ccy=Currency.TWD,
                     to_amount=Decimal("17000")),  # reconversion: 17000 - 500*32 = +1000
    ]
    rows = realized_fx_rows(convs, Currency.TWD, Currency.USD, Decimal("32"))
    assert len(rows) == 1
    assert rows[0].date == date(2026, 5, 1)
    assert rows[0].foreign_sold == Decimal("500")
    assert rows[0].home_received == Decimal("17000")
    assert rows[0].rate_used == Decimal("32")
    assert rows[0].realized == Decimal("1000")


# --- spec 2026-07-30 F2/F3: coverage scaling, and the silent-gap regression -------------


def _open(amt: str, acq: str | None) -> StoredCashMovement:
    return StoredCashMovement(
        id=0, account_id="schwab", date=date(2026, 1, 5), kind="OPENING",
        ccy=Currency.USD, amount=Decimal(amt),
        acq_home_amount=None if acq is None else Decimal(acq))


def _conv30() -> list[FXConversion]:
    """The measured 2026-07-30 Schwab pool: TWD 2,049,000 -> USD 63,572.99."""
    return [FXConversion(account_id="schwab", date=date(2026, 7, 30),
                         from_ccy=Currency.TWD, from_amount=Decimal("2049000"),
                         to_ccy=Currency.USD, to_amount=Decimal("63572.99"))]


def test_rated_opening_reconciles_the_pool_with_the_funds_view() -> None:
    """Acceptance 1: full coverage -> pool == funds balance, gap 0, ratio 1."""
    txs = [_buy("61985.09", "1", date(2026, 7, 23))]  # drain to the measured balance
    r = compute_account_fx(SCHWAB, Currency.USD, Decimal("79462.20"), txs, [], _conv30(),
                           INSTR, spot=Decimal("32.320999"),
                           movements=[_open("100000", "3135870")])
    assert r.foreign_cash == Decimal("101587.90")   # == /api/cash balances[schwab/USD]
    assert r.covered_ratio == Decimal("1")
    assert r.fx_basis_gap == Decimal("0")
    assert r.foreign_cash_negative is False
    assert r.avg_rate == Decimal("5184870") / Decimal("163572.99")


def test_unrated_opening_scales_BOTH_legs_and_never_goes_negative() -> None:
    """Acceptance 3+4 (F2/F3): pro rata, applied to cash AND stocks.

    The subtraction shortcut (balance - unbased) would be 1,587.90 here and NEGATIVE once
    the balance drops below 100,000 — the reversed-sign figure this spec exists to remove.
    """
    txs = [_buy("61985.09", "1", date(2026, 7, 23))]
    r = compute_account_fx(SCHWAB, Currency.USD, Decimal("79462.20"), txs, [], _conv30(),
                           INSTR, spot=Decimal("32.320999"),
                           movements=[_open("100000", None)])
    ratio = Decimal("63572.99") / Decimal("163572.99")
    delta = Decimal("32.320999") - Decimal("2049000") / Decimal("63572.99")
    assert r.covered_ratio == ratio
    assert r.foreign_cash == Decimal("101587.90")             # full balance is still shown
    assert r.unrealized_fx_cash == Decimal("101587.90") * ratio * delta
    assert r.unrealized_fx_stocks == Decimal("79462.20") * ratio * delta  # F3: stocks too
    assert r.unrealized_fx_cash > 0                           # never the reversed sign
    assert r.fx_basis_gap == Decimal("101587.90") * (Decimal("1") - ratio)


def test_gap_flag_fires_while_the_pool_is_POSITIVE() -> None:
    """Acceptance 5 — the measured 2026-07-30 00:37 silent case.

    The pool had crossed back above zero (+1,587.90), so the old ``foreign_cash < 0``
    symptom flag went quiet while the error stayed at exactly 100,000 USD.
    """
    txs = [_buy("61985.09", "1", date(2026, 7, 23))]
    r = compute_account_fx(SCHWAB, Currency.USD, Decimal("79462.20"), txs, [], _conv30(),
                           INSTR, spot=Decimal("32.320999"),
                           movements=[_open("100000", None)])
    assert r.foreign_cash > 0            # positive: the old flag would have said "fine"
    assert r.foreign_cash_negative is False
    assert r.fx_basis_gap != 0           # the cause-side flag still fires


def test_pro_rata_basis_stays_non_negative_at_every_balance() -> None:
    """Acceptance 3 boundary sweep: 0 / below / above the unbased amount."""
    for spent in ("0", "50000", "150000", "163572.99"):
        txs = [_buy(spent, "1", date(2026, 7, 23))] if spent != "0" else []
        r = compute_account_fx(SCHWAB, Currency.USD, Decimal("0"), txs, [], _conv30(),
                               INSTR, spot=Decimal("32.320999"),
                               movements=[_open("100000", None)])
        assert r.unrealized_fx_cash is not None
        assert r.foreign_cash >= 0
        assert r.unrealized_fx_cash >= 0, f"negative basis at spent={spent}"


def test_no_movements_reproduces_the_previous_engine_exactly() -> None:
    """Acceptance 8: a pre-spec ledger is byte-identical, ratio included."""
    convs = [FXConversion(account_id="schwab", date=date(2025, 1, 1), from_ccy=Currency.TWD,
                          from_amount=Decimal("320000"), to_ccy=Currency.USD,
                          to_amount=Decimal("10000"))]
    txs = [_buy("90", "100", date(2025, 1, 2))]
    r = compute_account_fx(SCHWAB, Currency.USD, Decimal("10800"), txs, [], convs, INSTR,
                           spot=Decimal("33"), movements=[])
    assert r.covered_ratio == Decimal("1") and r.fx_basis_gap == Decimal("0")
    assert str(r.unrealized_fx_stocks) == "10800"
    assert str(r.unrealized_fx_cash) == "1000"


def test_gap_flag_survives_an_EMPTY_pool() -> None:
    """The disclosure must key on the CAUSE, not on the gap AMOUNT.

    Spend the pool down to zero: ``fx_basis_gap`` collapses to 0 (it is amount x share),
    yet ``covered_ratio`` is still < 1 and the STOCK leg is still being scaled — so a
    consumer flagging on the amount would go silent, repeating the very failure mode the
    old ``foreign_cash < 0`` flag had.
    """
    txs = [_buy("163572.99", "1", date(2026, 7, 23))]   # drain the whole pool
    r = compute_account_fx(SCHWAB, Currency.USD, Decimal("79462.20"), txs, [], _conv30(),
                           INSTR, spot=Decimal("32.320999"),
                           movements=[_open("100000", None)])
    assert r.foreign_cash == Decimal("0")
    assert r.fx_basis_gap == Decimal("0")        # the amount really is zero...
    assert r.fx_basis_incomplete is True         # ...but the cause flag still fires
    assert r.covered_ratio < Decimal("1")
