"""Options W1 — the shared result and wire types, before any replay branch uses them.

Spec ``docs/spec/2026-10-09-options-ledger.md`` §3 (``RefusedClose``, the two ``Book`` fields,
``unbookable_close``) and §7.1 (the ``options`` block). Every field defaults to the stock
meaning, so a book or payload built before W2 is unchanged.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from portfolio_dash.portfolio.dashboard_models import (
    DashboardData,
    DividendSummary,
    FreshnessReport,
    HoldingRow,
    KpiSummary,
    OptionMoney,
    OptionOpenRow,
    OptionsBlock,
    TrendSeries,
)
from portfolio_dash.portfolio.results import (
    Book,
    Holding,
    OpenedPosition,
    RealizedPnL,
    RefusedClose,
)
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.wire import to_wire


def _open_row(**over: object) -> OptionOpenRow:
    row: dict[str, object] = dict(
        account_id="schwab", symbol="XYZ261218P00050000", contract="XYZ 2026-12-18 50 Put",
        underlying="XYZ", expiry=date(2026, 12, 18), days_to_expiry=20, strike=Decimal("50"),
        right="P", direction="short", contracts=Decimal("2"),
        share_equivalents=Decimal("-200"), average_premium=Decimal("2.0"),
        premium_total=Decimal("398.70"), last_price=Decimal("2.0"),
        last_price_date=date(2026, 3, 2), market_value=Decimal("-400"),
        unrealized=Decimal("-1.30"), obligation=Decimal("10000"))
    row.update(over)
    return OptionOpenRow(**row)  # type: ignore[arg-type]


def test_book_defaults_are_the_stock_meaning() -> None:
    book = Book(holdings=[], realized=RealizedPnL(rows=[], by_currency={}), gross_invested={})
    assert book.refused_closes == [] and book.realized_close_reasons == {}
    assert OpenedPosition(account_id="a", symbol="X", shares=Decimal(1)).unbookable_close is False


def test_refused_close_carries_the_spec_fields() -> None:
    rc = RefusedClose(txn_id=9, account_id="schwab", symbol="XYZ261218P00050000",
                      date=date(2026, 12, 18), side=Side.BUY, quantity=Decimal("100"),
                      close_reason="expired", reason="…", remedy="…")
    assert set(RefusedClose.model_fields) == {
        "txn_id", "account_id", "symbol", "date", "side", "quantity", "close_reason",
        "reason", "remedy"}
    assert to_wire(rc.model_dump())["quantity"] == "100"


def test_unbookable_close_never_reaches_the_holdings_wire() -> None:
    h = Holding(account_id="schwab", symbol="XYZ", quote_ccy=Currency.USD, shares=Decimal(1),
                original_avg=Decimal(1), adjusted_avg=Decimal(1),
                original_cost_total=Decimal(1), adjusted_cost_total=Decimal(1),
                dividend_portion=Decimal(0), payback_ratio=Decimal(0),
                unbookable_close=True)
    data = h.model_dump()
    data.update(account_name="{account:schwab}", name="XYZ", market="US", sector="",
                board="")
    assert "unbookable_close" not in HoldingRow(**data).model_dump()
    assert "unbookable_close" not in HoldingRow.model_fields


def test_an_unpriced_underlying_is_omitted_from_the_wire() -> None:
    bare = to_wire(_open_row().model_dump())
    assert "underlying_price" not in bare and "underlying_price_date" not in bare
    priced = to_wire(_open_row(underlying_price=Decimal("51.20"),
                               underlying_price_date=date(2026, 3, 2)).model_dump())
    assert priced["underlying_price"] == "51.20"
    assert priced["underlying_price_date"] == "2026-03-02"
    assert priced["strike"] == "50" and priced["obligation"] == "10000"


def test_the_block_round_trips() -> None:
    block = OptionsBlock(open=[_open_row()],
                         market_value=OptionMoney(by_currency={Currency.USD: Decimal("-400")},
                                                  reporting=Decimal("-12800")))
    wire = to_wire(block.model_dump())
    assert wire["market_value"] == {"by_currency": {"USD": "-400"}, "reporting": "-12800"}
    assert wire["note"] is None and wire["trend_excludes_value"] is False
    assert set(wire) == {"open", "obligation_subtotals", "market_value", "unrealized",
                         "refused_closes", "realized", "trend_excludes_value", "note",
                         "principal_fx_note"}
    assert set(wire["realized"]) == {"rows", "by_contract", "by_year", "by_underlying",
                                     "realized_total"}


def test_dashboard_options_defaults_to_none() -> None:
    data = DashboardData(
        as_of=datetime(2026, 6, 10, 12, 0), reporting_currency=Currency.TWD,
        kpis=KpiSummary(reporting_currency=Currency.TWD, total_market_value=Decimal("1")),
        holdings=[], realized=RealizedPnL(rows=[], by_currency={}), returns=None,
        allocation=None, currency_view=None, fx=None,
        dividends=DividendSummary(by_year=[], total_by_currency={}),
        ex_dividend_calendar=[],
        trend=TrendSeries(points=[], reporting_currency=Currency.TWD, available=False),
        freshness=FreshnessReport(prices=[], fx=[], any_stale=False, missing_prices=[],
                                  missing_fx=[]))
    assert data.options is None


def test_cash_lines_carry_the_trade_id_and_no_label() -> None:
    """Spec §3 / §6.2: ``pool_lines`` sets ``CashLine.txn_id`` (an identity) on trade lines;
    ``option_side`` stays ``None`` there — only ``account_statement`` labels a line."""
    from portfolio_dash.data_ingestion.store import StoredTransaction
    from portfolio_dash.portfolio.cash import CashLine, pool_lines
    from portfolio_dash.shared.enums import Market
    from portfolio_dash.shared.models.assets import Instrument
    from portfolio_dash.shared.models.ledger import Transaction

    inst = {"XYZ": Instrument(symbol="XYZ", market=Market.US, quote_ccy=Currency.USD,
                              sector="", name="XYZ Corp")}
    stored = StoredTransaction(id=41, account_id="acc", symbol="XYZ", side=Side.BUY,
                               quantity=Decimal(1), price=Decimal(10), fees=Decimal(0),
                               tax=Decimal(0), trade_date=date(2026, 1, 5))
    draft = Transaction(account_id="acc", symbol="XYZ", side=Side.SELL, quantity=Decimal(1),
                        price=Decimal(12), fees=Decimal(0), tax=Decimal(0),
                        trade_date=date(2026, 1, 6))
    lines = pool_lines("acc", Currency.USD, [], [], [stored, draft], [], inst)
    assert [(ln.kind, ln.txn_id, ln.option_side) for ln in lines] == [
        ("buy", 41, None), ("sell", None, None)]
    plain = CashLine(date(2026, 1, 1), "deposit", "", Decimal(1))
    assert plain.txn_id is None and plain.option_side is None


def _adjust_bundle() -> object:
    from portfolio_dash.shared.corporate_actions import CorporateAction, CorporateActionKind
    from portfolio_dash.shared.enums import Market
    from portfolio_dash.shared.models.assets import Instrument
    from portfolio_dash.shared.models.ledger import LedgerBundle, Transaction

    src, dst = "XYZ261218P00060000", "XYZ261218P00020000"
    insts = {s: Instrument(symbol=s, market=Market.US, quote_ccy=Currency.USD, sector="",
                           name=s, asset_type="option") for s in (src, dst)}
    sto = Transaction(id=1, account_id="schwab", symbol=src, side=Side.SELL,
                      quantity=Decimal(100), price=Decimal(2), fees=Decimal("0.65"),
                      tax=Decimal(0), trade_date=date(2026, 3, 2), short_sale=True)
    adj = CorporateAction(account_id="schwab", date=date(2026, 6, 1),
                          kind=CorporateActionKind.OPTION_ADJUST, from_symbol=src,
                          to_symbol=dst, ratio_to=Decimal(3), ratio_from=Decimal(1), id=5)
    return LedgerBundle(transactions=[sto], instruments=insts, actions=[adj])


def test_until_w2_the_replay_refuses_an_option_adjust() -> None:
    """W1 placeholder: the kind exists, the replay refuses it like any unapplicable action —
    never a silent EXCHANGE-style move (W2 replaces this with the §4.3 transfer)."""
    import pytest

    from portfolio_dash.portfolio.cost_basis import UnbookableLedgerError, build_book
    from portfolio_dash.shared.models.ledger import LedgerBundle

    bundle = _adjust_bundle()
    assert isinstance(bundle, LedgerBundle)
    book = build_book(bundle, allow_oversell=True)
    (ua,) = book.unapplied_actions
    assert ua.action_id == 5 and "尚未支援" in ua.reason
    assert [(h.symbol, h.shares) for h in book.holdings] == [
        ("XYZ261218P00060000", Decimal(-100))]
    with pytest.raises(UnbookableLedgerError, match="尚未支援"):
        build_book(bundle)
