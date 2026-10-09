"""Options W2 — R2 valuation: an open contract is priced at its last ORDINARY trade (spec §5)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from portfolio_dash.portfolio.options import (
    LastTrade,
    last_trade_prices,
    ledger_has_options,
    stock_realized,
)
from portfolio_dash.portfolio.results import RealizedPnL, RealizedRow
from portfolio_dash.shared.corporate_actions import CorporateAction, CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.models.ledger import LedgerBundle, Transaction

D = Decimal
PUT = "XYZ261218P00050000"
SRC = "XYZ261218P00060000"
DST = "XYZ261218P00020000"


def _inst(symbol: str, asset_type: str = "option") -> Instrument:
    return Instrument(symbol=symbol, market=Market.US, quote_ccy=Currency.USD, sector="",
                      name=symbol, asset_type=asset_type)  # type: ignore[arg-type]


_INSTS = {s: _inst(s) for s in (PUT, SRC, DST)} | {"XYZ": _inst("XYZ", "stock")}


def _tx(symbol: str, price: str, day: date, *, account: str = "schwab",
        side: Side = Side.SELL, close: str | None = None) -> Transaction:
    return Transaction(account_id=account, symbol=symbol, side=side, quantity=D(100),
                       price=D(price), fees=D(0), tax=D(0), trade_date=day,
                       short_sale=side is Side.SELL and close is None,
                       close_reason=close)  # type: ignore[arg-type]


def _adjust(day: date, ratio_to: str = "3") -> CorporateAction:
    return CorporateAction(account_id="schwab", date=day,
                           kind=CorporateActionKind.OPTION_ADJUST, from_symbol=SRC,
                           to_symbol=DST, ratio_to=D(ratio_to), ratio_from=D(1), id=1)


def test_the_last_ordinary_trade_across_accounts_on_or_before_the_day() -> None:
    bundle = LedgerBundle(transactions=[
        _tx(PUT, "2.00", date(2026, 3, 2)),
        _tx(PUT, "2.50", date(2026, 4, 1), account="moomoo_my"),
        _tx(PUT, "0", date(2026, 4, 2), side=Side.BUY, close="assigned"),   # not a price
        _tx(PUT, "9.99", date(2026, 7, 1)),                                 # after the day
    ], instruments=_INSTS)
    assert last_trade_prices(bundle, date(2026, 6, 10), unapplied=[]) == {
        PUT: LastTrade(price=D("2.50"), on=date(2026, 4, 1))}
    assert last_trade_prices(bundle, date(2026, 3, 31), unapplied=[]) == {
        PUT: LastTrade(price=D("2.00"), on=date(2026, 3, 2))}


def test_after_an_adjustment_with_no_later_trade_the_predecessor_price_over_ratio() -> None:
    bundle = LedgerBundle(transactions=[_tx(SRC, "6.00", date(2026, 3, 2))],
                          instruments=_INSTS, actions=[_adjust(date(2026, 6, 1))])
    assert last_trade_prices(bundle, date(2026, 6, 10), unapplied=[])[DST] == LastTrade(
        price=D("2.00"), on=date(2026, 3, 2))
    # a trade on the successor after the adjustment replaces it
    later = LedgerBundle(transactions=[_tx(SRC, "6.00", date(2026, 3, 2)),
                                       _tx(DST, "1.75", date(2026, 6, 5))],
                         instruments=_INSTS, actions=[_adjust(date(2026, 6, 1))])
    assert last_trade_prices(later, date(2026, 6, 10), unapplied=[])[DST] == LastTrade(
        price=D("1.75"), on=date(2026, 6, 5))
    # a successor trade from before the adjustment survives it only when it is LATER than
    # the predecessor's (final review R1-M2, 2026-10-10): here it is, 05-01 against 03-02
    before = LedgerBundle(transactions=[_tx(SRC, "6.00", date(2026, 3, 2)),
                                        _tx(DST, "1.10", date(2026, 5, 1))],
                          instruments=_INSTS, actions=[_adjust(date(2026, 6, 1))])
    assert last_trade_prices(before, date(2026, 6, 10), unapplied=[])[DST] == LastTrade(
        price=D("1.10"), on=date(2026, 5, 1))
    # ... and when it is older than the predecessor's last trade, the derived price wins
    older = LedgerBundle(transactions=[_tx(DST, "1.10", date(2026, 2, 1)),
                                       _tx(SRC, "6.00", date(2026, 3, 2))],
                         instruments=_INSTS, actions=[_adjust(date(2026, 6, 1))])
    assert last_trade_prices(older, date(2026, 6, 10), unapplied=[])[DST].price == D("2.00")


def test_a_same_day_trade_on_the_successor_follows_the_adjustment() -> None:
    bundle = LedgerBundle(transactions=[_tx(SRC, "6.00", date(2026, 3, 2)),
                                        _tx(DST, "1.90", date(2026, 6, 1))],
                          instruments=_INSTS, actions=[_adjust(date(2026, 6, 1))])
    assert last_trade_prices(bundle, date(2026, 6, 1), unapplied=[])[DST].price == D("1.90")


def test_the_block_exists_only_with_a_traded_or_adjusted_contract() -> None:
    assert not ledger_has_options(LedgerBundle(instruments=_INSTS))      # registered only
    assert not ledger_has_options(LedgerBundle(
        transactions=[_tx("XYZ", "50", date(2026, 3, 2), side=Side.BUY)], instruments=_INSTS))
    assert ledger_has_options(LedgerBundle(transactions=[_tx(PUT, "2", date(2026, 3, 2))],
                                           instruments=_INSTS))
    assert ledger_has_options(LedgerBundle(instruments=_INSTS,
                                           actions=[_adjust(date(2026, 6, 1))]))


def test_stock_realized_drops_option_rows_and_re_sums() -> None:
    def row(symbol: str, realized: str) -> RealizedRow:
        return RealizedRow(account_id="schwab", symbol=symbol, quote_ccy=Currency.USD,
                           sell_date=date(2026, 5, 1), shares_sold=D(1), proceeds_net=D(1),
                           original_cost_removed=D(0), adjusted_cost_removed=D(0),
                           realized=D(realized))
    rp = RealizedPnL(rows=[row("XYZ", "10"), row(PUT, "400"), row("XYZ", "5")],
                     by_currency={Currency.USD: D("415")})
    out = stock_realized(rp, frozenset({PUT}))
    assert [r.symbol for r in out.rows] == ["XYZ", "XYZ"]
    assert out.by_currency == {Currency.USD: D("15")}


def test_past_expiry_holdings_are_the_books_open_contracts_past_their_expiry() -> None:
    from portfolio_dash.portfolio.cost_basis import build_book
    from portfolio_dash.portfolio.options import past_expiry_holdings

    old = "XYZ260515P00050000"
    insts = _INSTS | {old: _inst(old)}
    book = build_book(LedgerBundle(transactions=[
        _tx(old, "1", date(2026, 3, 2)),                     # short 100, expired 5/15
        _tx(PUT, "2", date(2026, 3, 2)),                     # short 100, expires 12/18
        _tx("XYZ", "50", date(2026, 3, 2), side=Side.BUY),   # a stock is never listed
    ], instruments=insts), allow_oversell=True)
    listed = past_expiry_holdings(book.holdings, insts, date(2026, 6, 10))
    assert [(h.symbol, h.shares) for h in listed] == [(old, D(-100))]
    assert past_expiry_holdings(book.holdings, insts, date(2026, 5, 15)) == []
