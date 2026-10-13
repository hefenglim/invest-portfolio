"""The daily trend replays the book only on the days a row counts from (2026-10-13).

`daily_value_series` rebuilt the book from the whole ledger once per CALENDAR day. On the
owner's 6-year ledger that was 2,181 replays per dashboard for 461 distinct event days —
26 s locally, minutes per page on the prod VM, while the demo's short synthetic corpus never
showed it. Between two event days `through(day)` holds the same rows, so the book is reused.

Two halves are pinned, as for every performance change to a money figure: the series is
IDENTICAL to rebuilding every day (every point, every flag), and the replay count is the
number of event days in the window, not the number of days.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from portfolio_dash.portfolio import timeseries as ts
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.portfolio.dashboard_models import TrendSeries
from portfolio_dash.portfolio.results import Book
from portfolio_dash.portfolio.returns import CashMovementFlow
from portfolio_dash.portfolio.timeseries import daily_value_series
from portfolio_dash.shared.corporate_actions import (
    CorporateAction,
    CorporateActionKind,
    UnreadableAction,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import (
    Dividend,
    LedgerBundle,
    OpeningInventory,
    Transaction,
)

D = Decimal
USD, TWD = Currency.USD, Currency.TWD
START, END = date(2026, 3, 1), date(2026, 4, 30)
NO_CASH: list[CashMovementFlow] = []

INSTRUMENTS = {
    "AAA": Instrument(symbol="AAA", market=Market.US, quote_ccy=USD, sector="Tech",
                      name="AAA Corp"),
    "BBB": Instrument(symbol="BBB", market=Market.TW, quote_ccy=TWD, sector="Semis",
                      name="BBB Corp", board="TWSE"),
    "CCC": Instrument(symbol="CCC", market=Market.US, quote_ccy=USD, sector="Tech",
                      name="CCC Corp"),
}


def _tx(day: date, side: Side, qty: str, price: str, symbol: str = "AAA",
        account: str = "schwab") -> Transaction:
    return Transaction(account_id=account, symbol=symbol, side=side, quantity=D(qty),
                       price=D(price), fees=D("1"), tax=D("0"), trade_date=day)


def _ledger() -> LedgerBundle:
    """Every shape the per-day book can take: opening, buys and sells, an acked oversell,
    a split, a cash dividend, a 配股 counted from its ex-date, an unreadable action, and a
    trade dated after the window."""
    return LedgerBundle(
        transactions=[
            _tx(date(2026, 3, 2), Side.BUY, "10", "100"),
            _tx(date(2026, 3, 9), Side.BUY, "5", "104"),
            _tx(date(2026, 3, 16), Side.SELL, "4", "110"),
            _tx(date(2026, 3, 23), Side.SELL, "30", "50", symbol="CCC"),   # 賣超 (acked)
            _tx(date(2026, 4, 6), Side.BUY, "40", "51", symbol="CCC"),
            _tx(date(2026, 4, 20), Side.BUY, "1000", "60", symbol="BBB", account="tw_broker"),
            _tx(date(2026, 5, 12), Side.BUY, "3", "120"),                  # after END
        ],
        dividends=[
            Dividend(account_id="schwab", symbol="AAA", date=date(2026, 3, 27),
                     type=DividendType.CASH, gross=D("6"), withholding=D("1.8"),
                     net=D("4.2")),
            Dividend(account_id="tw_broker", symbol="BBB", date=date(2026, 4, 28),
                     ex_date=date(2026, 4, 14), type=DividendType.STOCK, gross=D("0"),
                     withholding=D("0"), net=D("0"), reinvest_shares=D("50")),
        ],
        opening=[OpeningInventory(account_id="tw_broker", symbol="BBB", shares=D("2000"),
                                  original_cost_total=D("110000"),
                                  build_date=date(2026, 3, 1))],
        actions=[CorporateAction(account_id="schwab", date=date(2026, 4, 1),
                                 kind=CorporateActionKind.SPLIT, from_symbol="AAA",
                                 to_symbol="AAA", ratio_to=D("2"), ratio_from=D("1"))],
        unreadable_actions=[UnreadableAction(account_id="tw_broker", date=date(2026, 4, 24),
                                             kind="SPLIT", from_symbol="BBB",
                                             to_symbol="BBB", reason="ratio_to 必須是正整數")],
        instruments=INSTRUMENTS,
    )


def _prices() -> dict[str, list[tuple[date, Decimal]]]:
    out: dict[str, list[tuple[date, Decimal]]] = {"AAA": [], "BBB": [], "CCC": []}
    day, i = START, 0
    while day <= END:
        if day.weekday() < 5:          # weekends carry the Friday close forward
            out["AAA"].append((day, D(100 + i) if day < date(2026, 4, 1) else D(55 + i)))
            out["BBB"].append((day, D(60) + D(i) / 10))
            out["CCC"].append((day, D(50) + D(i) / 4))
            i += 1
        day += timedelta(days=1)
    return out


FX = {(USD, TWD): [(START, D("31.5")), (date(2026, 4, 1), D("32.1"))]}


def _series(bundle: LedgerBundle) -> TrendSeries:
    return daily_value_series(bundle, _prices(), FX, TWD, end=END, cash_movements=NO_CASH)


def test_series_is_identical_to_rebuilding_every_day(monkeypatch: pytest.MonkeyPatch) -> None:
    reused = _series(_ledger())

    every_day = {START + timedelta(days=n) for n in range((END - START).days + 1)}
    monkeypatch.setattr(LedgerBundle, "counts_on_dates", lambda self: every_day)
    rebuilt = _series(_ledger())

    assert reused.available and rebuilt.available
    assert reused == rebuilt
    # the fixture is not trivially flat: it values, it flags, and it moves
    assert any(p.incomplete for p in reused.points)
    assert any(not p.incomplete for p in reused.points)
    assert len({p.total_value for p in reused.points}) > 20


def test_the_book_is_replayed_once_per_event_day(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[date] = []
    real = build_book

    def counted(bundle: LedgerBundle, *, allow_oversell: bool = False) -> Book:
        days = bundle.counts_on_dates()
        calls.append(max(days) if days else START)
        return real(bundle, allow_oversell=allow_oversell)

    monkeypatch.setattr(ts, "build_book", counted)
    bundle = _ledger()
    _series(bundle)

    # (`replay_refusals`' whole-ledger replay goes through `returns`, not this module)
    in_window = {d for d in bundle.counts_on_dates() if START <= d <= END}
    assert len(calls) == len(in_window) == 11
    assert sorted(calls) == sorted(in_window)       # one replay per event day, each its own
    assert len(calls) < (END - START).days + 1 == 61


def test_a_window_that_ends_before_the_first_event_replays_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unchanged edge: no day in the window → the loop never runs, so no per-day replay."""
    calls = [0]
    real = build_book

    def counted(bundle: LedgerBundle, *, allow_oversell: bool = False) -> Book:
        calls[0] += 1
        return real(bundle, allow_oversell=allow_oversell)

    monkeypatch.setattr(ts, "build_book", counted)
    series = daily_value_series(_ledger(), _prices(), FX, TWD, end=date(2026, 2, 1),
                                cash_movements=NO_CASH)
    assert series.points == []
    assert calls[0] == 0
