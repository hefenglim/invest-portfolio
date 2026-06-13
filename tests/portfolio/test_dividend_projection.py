"""Spec 05 — declared-only annual dividend cash-flow projection (per-account net)."""

from datetime import date
from decimal import Decimal

from portfolio_dash.data_ingestion.config_seed import DEFAULT_ACCOUNTS
from portfolio_dash.portfolio.dashboard_models import ExDividendItem
from portfolio_dash.portfolio.dividends import project_dividends
from portfolio_dash.portfolio.results import Holding
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Account, Instrument


def _accounts() -> dict[str, Account]:
    # Account (shared/models/assets.py) has no dividend_model / fee_rule_set fields;
    # dividend_model is sourced from config-as-code (DEFAULT_ACCOUNTS) inside
    # project_dividends, keyed by account_id.
    return {
        a.account_id: Account(
            account_id=a.account_id,
            name=a.name,
            broker=a.broker,
            settlement_ccy=a.settlement_ccy,
            funding_ccy=a.funding_ccy,
        )
        for a in DEFAULT_ACCOUNTS
    }


def _instruments() -> dict[str, Instrument]:
    return {
        "2330": Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                           sector="Semi", name="TSMC"),
        "AAPL": Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                           sector="Tech", name="Apple"),
    }


def _holdings() -> list[Holding]:
    return [
        Holding(account_id="tw_broker", symbol="2330", quote_ccy=Currency.TWD,
                shares=Decimal("1000"), original_avg=Decimal("500"),
                adjusted_avg=Decimal("500"),
                original_cost_total=Decimal("500000"),
                adjusted_cost_total=Decimal("500000"),
                dividend_portion=Decimal("0"), payback_ratio=Decimal("0")),
        Holding(account_id="schwab", symbol="AAPL", quote_ccy=Currency.USD,
                shares=Decimal("10"), original_avg=Decimal("100"),
                adjusted_avg=Decimal("100"),
                original_cost_total=Decimal("1000"),
                adjusted_cost_total=Decimal("1000"),
                dividend_portion=Decimal("0"), payback_ratio=Decimal("0")),
    ]


def _ev(symbol: str, ex_date: date, cash: Decimal | None,
        ccy: Currency) -> ExDividendItem:
    return ExDividendItem(symbol=symbol, name=symbol, ex_date=ex_date,
                          cash_amount=cash, currency=ccy, source="test")


def test_project_declared_per_account_net() -> None:
    cal = [
        _ev("2330", date(2026, 12, 1), Decimal("5"), Currency.TWD),
        _ev("AAPL", date(2026, 11, 1), Decimal("0.50"), Currency.USD),
    ]
    proj = project_dividends(_holdings(), cal, _accounts(), _instruments(), year=2026)
    assert proj.basis == "declared_only" and proj.year == 2026
    tw = proj.by_currency[Currency.TWD]
    assert tw.declared_gross == Decimal("5000")
    assert tw.declared_net == Decimal("5000")
    assert tw.events == 1
    us = proj.by_currency[Currency.USD]
    assert us.declared_gross == Decimal("5.00")
    assert us.declared_net == Decimal("3.50")  # DRIP 30% withholding
    assert us.events == 1


def test_excludes_other_year_and_stock_events() -> None:
    cal = [
        _ev("2330", date(2025, 12, 1), Decimal("5"), Currency.TWD),   # wrong year
        _ev("AAPL", date(2026, 11, 1), None, Currency.USD),            # no cash (stock)
    ]
    proj = project_dividends(_holdings(), cal, _accounts(), _instruments(), year=2026)
    assert proj.by_currency == {}
