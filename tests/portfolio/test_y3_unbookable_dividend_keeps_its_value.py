"""Y3 (owner ruling 2026-10-05): a HELD position flagged ``unbookable_dividend`` keeps its market
value in the trend — the day is marked incomplete, the value is not dropped.

The flag means a payout was refused (it landed on an open declared short, or on a ticker an
EXCHANGE had vacated). The SHARES are right and the price is global and current, so the
position's market value is genuinely correct — ``Holding``'s taxonomy says so, and
``pnl.value_holdings`` never suppresses it (the KPI total counts it). ``daily_value_series`` was
the one reader that disagreed: it dropped the position's whole value on every day after the
refusal, so the trend, the net-worth line and the KPI total disagreed about a position nobody
doubted. Only the payout is 待釐清, so only the day's marker says so.

A held 賣超 and a position a corporate action could not be applied to keep today's treatment —
their value IS undefined (a discarded basis; shares in pre-action terms against a post-action
price), so the trend still excludes it. The unapplied-action control is
``test_timeseries.py::test_skipped_corporate_action_makes_the_day_incomplete``; the 賣超 one is
here.
"""

from datetime import date
from decimal import Decimal

from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.portfolio.returns import CashMovementFlow
from portfolio_dash.portfolio.timeseries import daily_value_series
from portfolio_dash.shared.corporate_actions import CorporateAction, CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import Dividend, LedgerBundle, Transaction

D = Decimal
TWD = Currency.TWD
NO_CASH: list[CashMovementFlow] = []
_INSTR = {s: Instrument(symbol=s, market=Market.TW, quote_ccy=TWD, sector="Semis", name=s,
                        board="TWSE") for s in ("BBB", "DDD")}
_PRICES = {s: [(date(2026, 6, 1), D("50"))] for s in ("BBB", "DDD")}
_FX: dict[tuple[Currency, Currency], list[tuple[date, Decimal]]] = {}


def _tx(day: date, side: Side, qty: str, symbol: str = "BBB", *,
        short: bool = False) -> Transaction:
    return Transaction(account_id="tw_broker", symbol=symbol, side=side, quantity=D(qty),
                       price=D("50"), fees=D("0"), tax=D("0"), trade_date=day,
                       short_sale=short)


def _cash_div(day: date, symbol: str = "BBB") -> Dividend:
    return Dividend(account_id="tw_broker", symbol=symbol, date=day, type=DividendType.CASH,
                    gross=D("300"), withholding=D("0"), net=D("300"))


def _points(bundle: LedgerBundle, end: date) -> dict[date, tuple[bool, Decimal]]:
    series = daily_value_series(bundle, _PRICES, _FX, TWD, end=end, cash_movements=NO_CASH)
    assert series.available is True
    return {p.date: (p.incomplete, p.total_value) for p in series.points}


def test_a_held_short_keeps_its_value_after_a_refused_dividend() -> None:
    """A declared short of 100 @50 — a −5,000 liability the series must carry (2026-07-31) —
    then a payout lands on it and is refused. The liability did not go anywhere."""
    bundle = LedgerBundle([_tx(date(2026, 6, 1), Side.SELL, "100", short=True)],
                          dividends=[_cash_div(date(2026, 6, 2))], instruments=_INSTR)
    (held,) = build_book(bundle, allow_oversell=True).holdings
    assert held.short_open and held.unbookable_dividend          # the shape under test
    pts = _points(bundle, date(2026, 6, 3))
    assert pts[date(2026, 6, 1)] == (False, D("-5000"))
    assert pts[date(2026, 6, 2)] == (True, D("-5000"))           # marked, NOT dropped
    assert pts[date(2026, 6, 3)] == (True, D("-5000"))


def test_a_held_successor_keeps_its_value_after_a_refused_vacated_dividend() -> None:
    """BBB exchanged into DDD, then a payout on BBB (E24): the flag goes on the HELD DDD,
    whose 100 shares at 50 are exactly as real after the refusal as before it."""
    bundle = LedgerBundle(
        [_tx(date(2026, 6, 1), Side.BUY, "100")],
        dividends=[_cash_div(date(2026, 6, 3))], instruments=_INSTR,
        actions=[CorporateAction(account_id="tw_broker", date=date(2026, 6, 2),
                                 kind=CorporateActionKind.EXCHANGE, from_symbol="BBB",
                                 to_symbol="DDD", ratio_to=D("1"), ratio_from=D("1"))])
    (held,) = build_book(bundle, allow_oversell=True).holdings
    assert held.symbol == "DDD" and held.unbookable_dividend
    pts = _points(bundle, date(2026, 6, 4))
    assert pts[date(2026, 6, 2)] == (False, D("5000"))
    assert pts[date(2026, 6, 3)] == (True, D("5000"))            # marked, NOT dropped
    assert pts[date(2026, 6, 4)] == (True, D("5000"))


def test_a_held_oversold_position_is_still_excluded() -> None:
    """The control: a 賣超's basis was discarded and its share count is the net of a sale that
    exceeded the holding — no honest value, so the day is marked AND the value withheld,
    exactly as before Y3."""
    bundle = LedgerBundle([_tx(date(2026, 6, 1), Side.BUY, "100"),
                           _tx(date(2026, 6, 2), Side.SELL, "200"),
                           _tx(date(2026, 6, 3), Side.BUY, "300")], instruments=_INSTR)
    (held,) = build_book(bundle, allow_oversell=True).holdings
    assert held.oversold and held.shares == D("200")
    pts = _points(bundle, date(2026, 6, 3))
    assert pts[date(2026, 6, 1)] == (False, D("5000"))
    assert pts[date(2026, 6, 3)] == (True, D("0"))
