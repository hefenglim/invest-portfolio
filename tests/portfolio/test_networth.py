"""FU-D29 net worth (deferred C8): the daily cash series + trend composition.

Fixed-fixture, hand-computed. The consistency backbone: the terminal day of
``daily_cash_series`` must reconstruct the verified ``cash_balances`` reporting
total (same rows, both paths), and ``compose_net_worth`` must add ``net_worth``
without disturbing any pre-existing ``TrendPoint`` field. Decimal end to end.
"""

from datetime import date
from decimal import Decimal

from portfolio_dash.data_ingestion.store import (
    StoredCashMovement,
    StoredDividend,
    StoredFxConversion,
    StoredTransaction,
)
from portfolio_dash.portfolio.cash import cash_balances
from portfolio_dash.portfolio.dashboard_models import TrendPoint, TrendSeries
from portfolio_dash.portfolio.networth import (
    CashDay,
    compose_net_worth,
    daily_cash_series,
)
from portfolio_dash.portfolio.timeseries import FxHistory, _fx_at
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

TWD = Currency.TWD
USD = Currency.USD
MYR = Currency.MYR

INSTRUMENTS = {
    "AAA": Instrument(symbol="AAA", market=Market.US, quote_ccy=USD,
                      sector="Tech", name="AAA Corp"),
    "BBB": Instrument(symbol="BBB", market=Market.TW, quote_ccy=TWD,
                      sector="Semis", name="BBB Corp", board="TWSE"),
}


def _mv(acc: str, d: str, kind: str, ccy: Currency, amt: str,
        mid: int = 0) -> StoredCashMovement:
    return StoredCashMovement(id=mid, account_id=acc, date=date.fromisoformat(d),
                              kind=kind, ccy=ccy, amount=Decimal(amt), note=None)


def _fx(acc: str, d: str, from_ccy: Currency, from_amt: str,
        to_ccy: Currency, to_amt: str, fid: int = 0) -> StoredFxConversion:
    return StoredFxConversion(id=fid, account_id=acc, date=date.fromisoformat(d),
                              from_ccy=from_ccy, from_amount=Decimal(from_amt),
                              to_ccy=to_ccy, to_amount=Decimal(to_amt))


def _tx(acc: str, sym: str, d: str, side: Side, qty: str, price: str,
        tid: int = 0) -> StoredTransaction:
    return StoredTransaction(id=tid, account_id=acc, symbol=sym, side=side,
                             quantity=Decimal(qty), price=Decimal(price),
                             fees=Decimal("0"), tax=Decimal("0"),
                             trade_date=date.fromisoformat(d))


def _div(acc: str, sym: str, d: str, net: str, div_type: str = "CASH",
         did: int = 0) -> StoredDividend:
    return StoredDividend(id=did, account_id=acc, symbol=sym, date=date.fromisoformat(d),
                          type=div_type, gross=Decimal(net), withholding=Decimal("0"),
                          net=Decimal(net))


# --- Fixture A: multi-ccy pools (TWD accounts + a USD pool funded by conversion),
#     a trade-settlement day, a cash-dividend day, an fx-conversion day (both legs).


def _fixture_a() -> tuple[
    list[StoredCashMovement], list[StoredFxConversion],
    list[StoredTransaction], list[StoredDividend], FxHistory,
]:
    movements = [
        _mv("tw_broker", "2026-06-01", "DEPOSIT", TWD, "500000"),
        _mv("schwab", "2026-06-01", "DEPOSIT", TWD, "320000"),
    ]
    fx = [_fx("schwab", "2026-06-02", TWD, "310000", USD, "10000")]  # rate 31
    txs = [
        _tx("tw_broker", "BBB", "2026-06-03", Side.BUY, "100", "500"),   # −50,000 TWD
        _tx("schwab", "AAA", "2026-06-03", Side.BUY, "20", "250"),       # −5,000 USD
    ]
    divs = [_div("tw_broker", "BBB", "2026-06-04", "1000")]             # +1,000 TWD
    fx_history: FxHistory = {
        (USD, TWD): [(date(2026, 6, 1), Decimal("30")), (date(2026, 6, 4), Decimal("32"))]
    }
    return movements, fx, txs, divs, fx_history


def test_daily_cash_series_carry_forward_multi_ccy() -> None:
    movements, fx, txs, divs, fx_history = _fixture_a()
    series = daily_cash_series(movements, fx, txs, divs, INSTRUMENTS, fx_history, TWD,
                              end=date(2026, 6, 5))
    # 06-01: tw 500,000 + schwab-TWD 320,000 (USD pool not yet funded).
    assert series[date(2026, 6, 1)] == CashDay(Decimal("820000"), False)
    # 06-02: fx_out drains schwab TWD to 10,000; USD pool 10,000 @30 -> 300,000.
    assert series[date(2026, 6, 2)] == CashDay(Decimal("810000"), False)
    # 06-03: BBB buy -50,000 (tw 450,000); AAA buy -5,000 USD (5,000 @30 -> 150,000).
    assert series[date(2026, 6, 3)] == CashDay(Decimal("610000"), False)
    # 06-04: dividend +1,000 (tw 451,000); USD rate rolls to 32 (5,000 -> 160,000).
    assert series[date(2026, 6, 4)] == CashDay(Decimal("621000"), False)
    # 06-05: carry-forward, unchanged.
    assert series[date(2026, 6, 5)] == CashDay(Decimal("621000"), False)


def test_terminal_day_reconstructs_cash_balances_reporting_total() -> None:
    """Consistency anchor (b): the last day equals the verified end-balance total."""
    movements, fx, txs, divs, fx_history = _fixture_a()
    end = date(2026, 6, 5)
    balances = cash_balances(movements, fx, txs, divs, INSTRUMENTS)
    # Independent reporting total from the END balances at the day's carry-forward FX.
    expected = Decimal("0")
    for (_acct, ccy), bal in balances.items():
        if ccy == TWD:
            expected += bal
        else:
            rate = _fx_at(fx_history, end, ccy, TWD)
            assert rate is not None
            expected += bal * rate
    series = daily_cash_series(movements, fx, txs, divs, INSTRUMENTS, fx_history, TWD,
                              end=end)
    assert series[end].reporting_total == expected == Decimal("621000")


def test_fx_conversion_day_moves_both_legs() -> None:
    """The fx-conversion day: the drained home leg + the funded foreign leg net out
    in the reporting total (no phantom gain/loss from the conversion itself)."""
    movements = [_mv("schwab", "2026-06-01", "DEPOSIT", TWD, "320000")]
    fx = [_fx("schwab", "2026-06-02", TWD, "320000", USD, "10000")]  # rate 32
    fx_history: FxHistory = {(USD, TWD): [(date(2026, 6, 2), Decimal("32"))]}
    series = daily_cash_series(movements, fx, [], [], INSTRUMENTS, fx_history, TWD,
                              end=date(2026, 6, 2))
    # Day of conversion at spot 32: TWD 0 + USD 10,000 @32 = 320,000 (== pre-conversion).
    assert series[date(2026, 6, 1)] == CashDay(Decimal("320000"), False)
    assert series[date(2026, 6, 2)] == CashDay(Decimal("320000"), False)


def test_missing_fx_day_marks_incomplete_and_excludes_pool() -> None:
    """A non-zero foreign pool with no on-or-before FX -> that day is incomplete and
    the pool value is excluded (never guessed); the reporting-ccy pool still counts."""
    movements = [
        _mv("tw_broker", "2026-06-01", "DEPOSIT", TWD, "100000"),
        _mv("schwab", "2026-06-01", "DEPOSIT", USD, "1000"),
    ]
    series = daily_cash_series(movements, [], [], [], INSTRUMENTS, {}, TWD,
                              end=date(2026, 6, 1))
    day = series[date(2026, 6, 1)]
    assert day.incomplete is True          # USD pool has no FX at all
    assert day.reporting_total == Decimal("100000")  # only the TWD pool contributes


def test_zero_balance_pool_missing_fx_does_not_poison_day() -> None:
    """A pool that nets to zero on a day needs no FX and must not mark the day
    incomplete; the same pool while non-zero (and FX-less) does."""
    movements = [
        _mv("schwab", "2026-06-01", "DEPOSIT", USD, "1000"),
        _mv("schwab", "2026-06-02", "WITHDRAW", USD, "1000"),
    ]
    series = daily_cash_series(movements, [], [], [], INSTRUMENTS, {}, TWD,
                              end=date(2026, 6, 2))
    assert series[date(2026, 6, 1)].incomplete is True   # USD 1,000, no FX
    assert series[date(2026, 6, 2)] == CashDay(Decimal("0"), False)  # USD 0 -> not poisoned


def test_pool_overdraft_renders_negative_never_floored() -> None:
    """A buy with no funding drives the pool negative; the balance renders as-is
    (the cash view surfaces overdraft — it is never floored at zero)."""
    txs = [_tx("tw_broker", "BBB", "2026-06-01", Side.BUY, "100", "500")]  # −50,000, no deposit
    series = daily_cash_series([], [], txs, [], INSTRUMENTS, {}, TWD, end=date(2026, 6, 1))
    assert series[date(2026, 6, 1)] == CashDay(Decimal("-50000"), False)


def test_empty_ledgers_return_empty() -> None:
    assert daily_cash_series([], [], [], [], INSTRUMENTS, {}, TWD, end=date(2026, 6, 1)) == {}


# --- compose_net_worth ------------------------------------------------------


def _trend(points: list[tuple[str, str, str, bool]]) -> TrendSeries:
    return TrendSeries(
        points=[TrendPoint(date=date.fromisoformat(d), total_value=Decimal(tv),
                           net_invested=Decimal(ni), incomplete=inc)
                for d, tv, ni, inc in points],
        reporting_currency=TWD,
    )


def test_compose_adds_net_worth_equal_to_value_plus_cash() -> None:
    trend = _trend([("2026-06-01", "30000", "30030", False),
                    ("2026-06-02", "33000", "30030", False)])
    cash = {date(2026, 6, 1): CashDay(Decimal("5000"), False),
            date(2026, 6, 2): CashDay(Decimal("-2000"), False)}
    out = compose_net_worth(trend, cash)
    p0, p1 = out.points
    assert p0.net_worth == Decimal("35000")   # 30000 + 5000
    assert p1.net_worth == Decimal("31000")   # 33000 + (-2000), overdraft flows through
    # Invariant (a): net_worth − total_value == cash_of_day exactly, for every point.
    assert p0.net_worth is not None and p0.net_worth - p0.total_value == Decimal("5000")
    assert p1.net_worth is not None and p1.net_worth - p1.total_value == Decimal("-2000")


def test_compose_none_on_cash_incomplete_day() -> None:
    trend = _trend([("2026-06-01", "30000", "30030", False),
                    ("2026-06-02", "33000", "30030", False)])
    cash = {date(2026, 6, 1): CashDay(Decimal("5000"), True),   # a pool lacked FX
            date(2026, 6, 2): CashDay(Decimal("5000"), False)}
    out = compose_net_worth(trend, cash)
    assert out.points[0].net_worth is None          # honest gap, not a fabricated total
    assert out.points[1].net_worth == Decimal("38000")


def test_compose_holdings_incomplete_still_gets_partial_net_worth() -> None:
    """A holdings-incomplete day (partial total_value) mirrors the existing lines: it
    still draws a net_worth value (flagged by the shared incomplete marker), not a gap."""
    trend = _trend([("2026-06-01", "0", "30030", True)])  # missing price -> incomplete
    cash = {date(2026, 6, 1): CashDay(Decimal("5000"), False)}
    out = compose_net_worth(trend, cash)
    assert out.points[0].net_worth == Decimal("5000")


def test_compose_cash_before_first_line_is_zero() -> None:
    trend = _trend([("2026-06-01", "30000", "30030", False)])
    out = compose_net_worth(trend, {})  # no cash entries at all -> treated as 0
    assert out.points[0].net_worth == Decimal("30000")


def test_compose_leaves_pre_existing_fields_byte_identical() -> None:
    """Invariant (c): composition changes ONLY net_worth; every other field is copied
    byte-identically, and the input series is not mutated."""
    trend = _trend([("2026-06-01", "30000", "30030", False),
                    ("2026-06-02", "33000", "30030", True)])
    before = [p.model_dump(exclude={"net_worth"}) for p in trend.points]
    out = compose_net_worth(trend, {date(2026, 6, 1): CashDay(Decimal("5000"), False)})
    after = [p.model_dump(exclude={"net_worth"}) for p in out.points]
    assert before == after
    # the input objects are untouched (compose returns copies)
    assert all(p.net_worth is None for p in trend.points)
    assert out.reporting_currency == trend.reporting_currency
    assert out.available == trend.available


def test_compose_empty_series_is_noop() -> None:
    empty = TrendSeries(points=[], reporting_currency=TWD, available=False)
    assert compose_net_worth(empty, {}) is empty
