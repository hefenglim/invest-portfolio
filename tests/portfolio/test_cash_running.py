"""Unit tests: date-aware pool timeline (C3 running-min) + opening-kind credit (C4) +
structured line detail / account-level statement (FU-D5)."""

from datetime import date
from decimal import Decimal

from portfolio_dash.data_ingestion.store import (
    StoredCashMovement,
    StoredDividend,
    StoredFxConversion,
    StoredTransaction,
)
from portfolio_dash.portfolio.cash import (
    CashLine,
    account_statement,
    cash_balances,
    option_sides_of,
    pool_lines,
    running_min,
    running_statement,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side


def _mv(d: str, kind: str, amt: str, mid: int = 0,
        ccy: Currency = Currency.USD, note: str | None = None) -> StoredCashMovement:
    return StoredCashMovement(id=mid, account_id="acc", date=date.fromisoformat(d),
                              kind=kind, ccy=ccy, amount=Decimal(amt), note=note)


def _lines(movements: list[StoredCashMovement]) -> list[CashLine]:
    return pool_lines("acc", Currency.USD, movements, [], [], [], {})


def _inst(symbol: str = "AAPL", ccy: Currency = Currency.USD,
          name: str = "Apple", market: Market = Market.US) -> Instrument:
    return Instrument(symbol=symbol, market=market, quote_ccy=ccy, sector="Tech", name=name)


def test_running_min_catches_backdated_dip() -> None:
    # deposit AFTER the withdrawal: end aggregate is +200 (passes an end-check), but the
    # pool dipped to -500 in between -> the date-aware running-min catches it (audit C3).
    movements = [_mv("2026-02-01", "WITHDRAW", "500"), _mv("2026-03-01", "DEPOSIT", "700")]
    assert running_min(_lines(movements)) == Decimal("-500")


def test_running_min_non_negative_when_funded_first() -> None:
    movements = [_mv("2026-01-01", "DEPOSIT", "700"), _mv("2026-02-01", "WITHDRAW", "500")]
    assert running_min(_lines(movements)) == Decimal("0")


def test_opening_kind_is_credit_in_balance_and_lines() -> None:
    movements = [_mv("2026-01-01", "OPENING", "1000")]
    bal = cash_balances(movements, [], [], [], {})
    assert bal[("acc", Currency.USD)] == Decimal("1000")  # deposit-like credit
    lines = _lines(movements)
    assert lines[0].kind == "opening" and lines[0].delta == Decimal("1000")


def test_running_statement_annotates_balance() -> None:
    movements = [_mv("2026-01-01", "OPENING", "1000"), _mv("2026-02-01", "WITHDRAW", "300")]
    stmt = running_statement(_lines(movements))
    assert [str(b) for _ln, b in stmt] == ["1000", "700"]


# --- FU-D5: structured line detail ------------------------------------------


def test_pool_lines_trade_carries_structured_detail() -> None:
    tx = StoredTransaction(id=1, account_id="acc", symbol="AAPL", side=Side.BUY,
                           quantity=Decimal("10"), price=Decimal("100"),
                           fees=Decimal("1"), tax=Decimal("0.5"), trade_date=date(2026, 1, 10))
    lines = pool_lines("acc", Currency.USD, [], [], [tx], [], {"AAPL": _inst()})
    assert len(lines) == 1
    ln = lines[0]
    assert ln.kind == "buy" and ln.symbol == "AAPL" and ln.name == "Apple"
    assert ln.qty == Decimal("10") and ln.price == Decimal("100")
    assert ln.fee == Decimal("1") and ln.tax == Decimal("0.5")
    # delta math is unchanged: -(10*100 + 1 + 0.5)
    assert ln.delta == Decimal("-1001.5")


def test_pool_lines_dividend_carries_symbol_name_only() -> None:
    div = StoredDividend(id=1, account_id="acc", symbol="AAPL", date=date(2026, 3, 1),
                         type="CASH", gross=Decimal("10"), withholding=Decimal("3"),
                         net=Decimal("7"))
    lines = pool_lines("acc", Currency.USD, [], [], [], [div], {"AAPL": _inst()})
    assert len(lines) == 1
    ln = lines[0]
    assert ln.kind == "dividend" and ln.symbol == "AAPL" and ln.name == "Apple"
    assert ln.delta == Decimal("7")
    # trade-only detail stays None for a dividend
    assert ln.qty is None and ln.price is None and ln.fee is None and ln.tax is None


def test_pool_lines_fx_legs_carry_rate_and_counter() -> None:
    fx = StoredFxConversion(id=1, account_id="acc", date=date(2026, 1, 8),
                            from_ccy=Currency.TWD, from_amount=Decimal("32000"),
                            to_ccy=Currency.USD, to_amount=Decimal("1000"))
    # USD pool: the received leg (fx_in). Counter = the from-pool it drained (negative TWD).
    usd = pool_lines("acc", Currency.USD, [], [fx], [], [], {})
    assert len(usd) == 1
    a = usd[0]
    assert a.kind == "fx_in" and a.delta == Decimal("1000")
    assert a.fx_rate == Decimal("32")  # 32000 / 1000
    assert a.counter_ccy == "TWD" and a.counter_amount == Decimal("-32000")
    # TWD pool: the spent leg (fx_out). Counter = the to-pool it fed (positive USD).
    twd = pool_lines("acc", Currency.TWD, [], [fx], [], [], {})
    assert len(twd) == 1
    b = twd[0]
    assert b.kind == "fx_out" and b.delta == Decimal("-32000")
    assert b.fx_rate == Decimal("32")
    assert b.counter_ccy == "USD" and b.counter_amount == Decimal("1000")


def test_pool_lines_movement_and_rebate_have_no_structured_detail() -> None:
    lines = _lines([_mv("2026-06-01", "REBATE", "109", note="2026-05 折讓款")])
    assert len(lines) == 1
    ln = lines[0]
    assert ln.kind == "rebate" and ln.delta == Decimal("109") and ln.ref == "2026-05 折讓款"
    assert ln.symbol is None and ln.qty is None and ln.fx_rate is None and ln.counter_ccy is None


# --- FU-D5: account-level (all-currency) statement --------------------------


def test_account_statement_all_ccy_groups_pools_with_own_balance() -> None:
    movements = [_mv("2026-01-01", "DEPOSIT", "50000", ccy=Currency.TWD),
                 _mv("2026-02-01", "DEPOSIT", "1000", ccy=Currency.USD)]
    fx = StoredFxConversion(id=1, account_id="acc", date=date(2026, 1, 8),
                            from_ccy=Currency.TWD, from_amount=Decimal("32000"),
                            to_ccy=Currency.USD, to_amount=Decimal("1000"))
    stmts = dict(account_statement("acc", movements, [fx], [], [], {}, option_side_of={}))
    assert Currency.TWD in stmts and Currency.USD in stmts and Currency.MYR not in stmts
    # each pool's running balance stays within its own currency (never blended)
    assert stmts[Currency.TWD][-1][1] == Decimal("18000")   # 50000 − 32000
    assert stmts[Currency.USD][-1][1] == Decimal("2000")    # 1000 fx_in + 1000 deposit


def test_account_statement_single_ccy_returns_only_that_pool() -> None:
    movements = [_mv("2026-01-01", "DEPOSIT", "50000", ccy=Currency.TWD),
                 _mv("2026-02-01", "DEPOSIT", "1000", ccy=Currency.USD)]
    stmts = account_statement("acc", movements, [], [], [], {}, option_side_of={},
                              ccy=Currency.USD)
    assert [c for c, _ in stmts] == [Currency.USD]
    assert stmts[0][1][-1][1] == Decimal("1000")


# --- options spec §6.2: the statement lines carry the §6.1 label -----------------------------

_PUT = "XYZ261218P00050000"


def _opt_inst() -> Instrument:
    return Instrument(symbol=_PUT, market=Market.US, quote_ccy=Currency.USD, sector="",
                      name="XYZ 2026-12-18 50 Put", asset_type="option")


def _opt_tx(tid: int, side: Side, qty: str, price: str, d: date, *, short: bool = False,
            close: str | None = None) -> StoredTransaction:
    return StoredTransaction(id=tid, account_id="acc", symbol=_PUT, side=side,
                             quantity=Decimal(qty), price=Decimal(price), fees=Decimal("0"),
                             tax=Decimal("0"), trade_date=d, short_sale=short,
                             close_reason=close)  # type: ignore[arg-type]


def test_account_statement_labels_option_lines_by_txn_id() -> None:
    txs = [_opt_tx(7, Side.SELL, "200", "1.5", date(2026, 11, 2), short=True),
           _opt_tx(9, Side.BUY, "200", "0", date(2026, 12, 18), close="expired"),
           StoredTransaction(id=3, account_id="acc", symbol="AAPL", side=Side.BUY,
                             quantity=Decimal("1"), price=Decimal("100"), fees=Decimal("0"),
                             tax=Decimal("0"), trade_date=date(2026, 1, 2))]
    insts = {_PUT: _opt_inst(), "AAPL": _inst()}
    sides = option_sides_of(txs, [], insts)
    assert sides == {7: "賣出開倉", 9: "到期作廢"}
    ((ccy, stmt),) = account_statement("acc", [], [], txs, [], insts, option_side_of=sides)
    assert ccy is Currency.USD
    assert {ln.txn_id: ln.option_side for ln, _bal in stmt} == {
        3: None, 7: "賣出開倉", 9: "到期作廢"}
    # The guards' arithmetic never sees a label (spec §6.2 / plan invariant 5).
    assert all(ln.option_side is None
               for ln in pool_lines("acc", Currency.USD, [], [], txs, [], insts))


def test_option_sides_of_is_empty_on_a_stock_ledger() -> None:
    tx = StoredTransaction(id=1, account_id="acc", symbol="AAPL", side=Side.BUY,
                           quantity=Decimal("1"), price=Decimal("1"), fees=Decimal("0"),
                           tax=Decimal("0"), trade_date=date(2026, 1, 2))
    assert option_sides_of([tx], [], {"AAPL": _inst()}) == {}
