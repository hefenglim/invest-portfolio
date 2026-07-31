"""Declared short sale + sticky 賣超 (spec 2026-07-31, option C).

The owner's rule, verbatim: 買回的每股成本結算獲利，剩下的股數以本次成本為起點.
That is standard short-cover accounting; these tests pin it, and pin the guard rails that
keep it from ever being applied to a row nobody declared.
"""

from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.portfolio.cost_basis import (
    OversellError,
    UnbookableLedgerError,
    build_book,
)
from portfolio_dash.portfolio.pnl import value_holdings
from portfolio_dash.portfolio.results import Book
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import Dividend, Transaction

TSLA = Instrument(symbol="TSLA", market=Market.US, quote_ccy=Currency.USD,
                  sector="Auto", name="Tesla")
INSTR = {"TSLA": TSLA}
D = Decimal


def _tx(side: Side, qty: str, price: str, d: date, *, short: bool = False) -> Transaction:
    return Transaction(account_id="schwab", symbol="TSLA", side=side, quantity=D(qty),
                       price=D(price), fees=D("0"), tax=D("0"), trade_date=d,
                       short_sale=short)


def _book(txs: list[Transaction]) -> Book:
    return build_book(txs, [], [], INSTR, allow_oversell=True)


def test_declared_short_then_cover_settles_at_the_cover_price() -> None:
    """6 held, sell 100 short, buy 100 back at 366.

    Long 6 realize normally (260 vs 240 cost). The other 94 are short at 260 and cover at
    366 -> realized (260-366)*94 = -9,964, dated the COVER day. The 6 shares left over
    start at the covering buy's own cost, 366.
    """
    txs = [
        _tx(Side.BUY, "6", "240", date(2026, 4, 1)),
        _tx(Side.SELL, "100", "260", date(2026, 6, 10), short=True),
        _tx(Side.BUY, "100", "366", date(2026, 7, 23)),
    ]
    book = _book(txs)
    rows = sorted(book.realized.rows, key=lambda r: (r.sell_date, r.kind))
    assert [r.kind for r in rows] == ["sale", "short_cover"]
    assert rows[0].shares_sold == D("6")
    assert rows[0].realized == D("120")                    # (260-240)*6
    cover = rows[1]
    assert cover.sell_date == date(2026, 7, 23)            # realizes on the COVER date
    assert cover.shares_sold == D("94")
    assert cover.realized == D("-9964")                    # (260-366)*94
    held = {(h.account_id, h.symbol): h for h in book.holdings}[("schwab", "TSLA")]
    assert held.shares == D("6")
    assert held.original_cost_total == D("2196")           # 6 * 366
    assert held.original_avg == D("366")                   # the covering buy's own cost
    assert held.short_open is False and held.oversold is False


def test_open_short_is_reported_as_a_signed_position_not_hidden() -> None:
    """An uncovered short must still appear, with the proceeds as its basis."""
    txs = [_tx(Side.SELL, "10", "260", date(2026, 6, 10), short=True)]
    held = {(h.account_id, h.symbol): h for h in _book(txs).holdings}[("schwab", "TSLA")]
    assert held.shares == D("-10")
    assert held.original_cost_total == D("-2600")
    assert held.original_avg == D("260")       # the short's average sale price
    assert held.short_open is True and held.oversold is False


def test_partial_cover_leaves_the_rest_short() -> None:
    txs = [
        _tx(Side.SELL, "10", "260", date(2026, 6, 10), short=True),
        _tx(Side.BUY, "4", "250", date(2026, 7, 1)),
    ]
    book = _book(txs)
    cover = [r for r in book.realized.rows if r.kind == "short_cover"]
    assert len(cover) == 1 and cover[0].shares_sold == D("4")
    assert cover[0].realized == D("40")                     # (260-250)*4
    held = {(h.account_id, h.symbol): h for h in book.holdings}[("schwab", "TSLA")]
    assert held.shares == D("-6") and held.short_open is True


def test_undeclared_oversell_is_still_blocked_and_never_becomes_a_short() -> None:
    """The whole point of the flag: a missing buy must not turn into a fabricated loss."""
    txs = [
        _tx(Side.BUY, "6", "240", date(2026, 4, 1)),
        _tx(Side.SELL, "100", "260", date(2026, 6, 10)),   # NOT declared
    ]
    with pytest.raises(OversellError):
        build_book(txs, [], [], INSTR)
    book = _book(txs)                                       # acked path
    assert not [r for r in book.realized.rows if r.kind == "short_cover"]


def test_oversold_flag_is_sticky_across_a_later_buy() -> None:
    """The 2026-07-30 defect: a later buy restored a positive position and cleared the
    warning, while the basis the oversell discarded stayed gone."""
    txs = [
        _tx(Side.BUY, "6", "240", date(2026, 4, 1)),
        _tx(Side.SELL, "100", "260", date(2026, 6, 10)),   # undeclared -> basis discarded
        _tx(Side.BUY, "100", "366", date(2026, 7, 23)),    # nets back to +6
    ]
    held = {(h.account_id, h.symbol): h for h in _book(txs).holdings}[("schwab", "TSLA")]
    assert held.shares == D("6")
    assert held.oversold is True, "a later buy must not clear the 賣超 warning"


def test_ordinary_ledger_is_byte_identical_to_the_pre_short_engine() -> None:
    """No short anywhere -> the exact previous numbers, including the cost total."""
    txs = [
        _tx(Side.BUY, "10", "250", date(2026, 3, 1)),
        _tx(Side.SELL, "4", "260", date(2026, 3, 20)),
    ]
    book = _book(txs)
    held = {(h.account_id, h.symbol): h for h in book.holdings}[("schwab", "TSLA")]
    # Byte-identical, exponent included: the ordinary buy takes the exact-total branch and
    # the ordinary sell keeps its `frac` arithmetic untouched, so the string must match what
    # the pre-short engine produced — not merely be numerically equal.
    assert str(held.original_cost_total) == "1500.0"
    assert held.shares == D("6")
    assert held.short_open is False and held.oversold is False
    assert [r.kind for r in book.realized.rows] == ["sale"]


# --- Fable-5 audit remediation (F1-F5, 2026-07-31) -------------------------------------


def _div(kind: DividendType, net: str, d: date, shares: str | None = None) -> Dividend:
    return Dividend(account_id="schwab", symbol="TSLA", date=d, type=kind, gross=D(net),
                    withholding=D("0"), net=D(net),
                    reinvest_shares=None if shares is None else D(shares),
                    reinvest_price=None if shares is None else D("100"))


def test_F1_cash_dividend_during_a_short_is_never_booked_as_income() -> None:
    """A short seller PAYS the dividend in lieu. The audit-H2 post-close branch fires on
    `shares == 0`, which is also an open short's long lot — it credited the payout."""
    txs = [_tx(Side.SELL, "10", "260", date(2026, 6, 10), short=True)]
    book = build_book(txs, [_div(DividendType.CASH, "50", date(2026, 6, 20))], [], INSTR,
                      allow_oversell=True)
    assert book.realized.rows == []
    held = book.holdings[0]
    assert held.shares == D("-10") and held.original_avg == D("260")
    assert held.unbookable_dividend is True


def test_F2_drip_during_a_short_never_adds_long_shares() -> None:
    """The killer case: a DRIP equal to the short netted the position to zero, and the
    holding — with its −2,600 of proceeds — disappeared from the report entirely."""
    txs = [_tx(Side.SELL, "10", "260", date(2026, 6, 10), short=True)]
    book = build_book(txs, [_div(DividendType.DRIP, "0", date(2026, 6, 20), shares="10")],
                      [], INSTR, allow_oversell=True)
    assert len(book.holdings) == 1, "the short must not vanish"
    held = book.holdings[0]
    assert held.shares == D("-10")
    assert held.original_cost_total == D("-2600")
    assert held.short_open is True and held.unbookable_dividend is True


def test_F1_F2_strict_path_fails_loud() -> None:
    """A dedicated exception type, and it SUBCLASSES ValueError on purpose: the call sites
    that already degrade on `except (ValueError, KeyError)` keep working untouched, while
    the strict sites (重算 / what-if / tax export) catch it precisely and answer 422 instead
    of letting it escape as a 500 (found by the 2026-07-31 phase-2 audit)."""
    txs = [_tx(Side.SELL, "10", "260", date(2026, 6, 10), short=True)]
    with pytest.raises(UnbookableLedgerError, match="放空部位") as exc:
        build_book(txs, [_div(DividendType.CASH, "50", date(2026, 6, 20))], [], INSTR)
    assert isinstance(exc.value, ValueError)
    assert "2026-06-20" in str(exc.value)      # names the offending row's date


def test_F3_unrealized_pct_keeps_its_sign_on_a_short() -> None:
    """`unrealized_pnl / original_cost_total` with a NEGATIVE basis renders a profitable
    short as a loss — the audit-H1 flip arriving from the other direction."""
    txs = [_tx(Side.SELL, "10", "260", date(2026, 6, 10), short=True)]
    held = value_holdings(_book(txs).holdings, {"TSLA": D("250")})[0]
    assert held.unrealized_pnl == D("100")                      # price fell -> profit
    assert held.unrealized_pnl / abs(held.original_cost_total) > 0
