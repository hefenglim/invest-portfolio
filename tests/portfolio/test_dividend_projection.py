"""Spec 05 — declared-only annual dividend cash-flow projection (per-account net)."""

from datetime import date
from decimal import Decimal

from portfolio_dash.data_ingestion.config_seed import DEFAULT_ACCOUNTS
from portfolio_dash.portfolio.dashboard_models import ExDividendItem
from portfolio_dash.portfolio.dividends import project_dividends
from portfolio_dash.portfolio.results import Holding
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Account, Instrument, MarketRule


def _accounts() -> dict[str, Account]:
    # dividend_model is a required Account field sourced from the DB; here it is
    # carried from the config seed so project_dividends reads it from this param.
    return {
        a.account_id: Account(
            account_id=a.account_id,
            name=a.name,
            broker=a.broker,
            settlement_ccy=a.settlement_ccy,
            funding_ccy=a.funding_ccy,
            dividend_model=a.dividend_model,
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


def _holding(account_id: str, symbol: str, ccy: Currency,
             shares: Decimal) -> Holding:
    return Holding(account_id=account_id, symbol=symbol, quote_ccy=ccy,
                   shares=shares, original_avg=Decimal("100"),
                   adjusted_avg=Decimal("100"),
                   original_cost_total=shares * Decimal("100"),
                   adjusted_cost_total=shares * Decimal("100"),
                   dividend_portion=Decimal("0"), payback_ratio=Decimal("0"))


def test_multi_account_same_symbol_counts_event_once() -> None:
    # AAPL held in both schwab (10 sh) and moomoo_my (20 sh); both DRIP.
    # A single AAPL ex-div event sums shares across accounts but counts as 1 event.
    holdings = [
        _holding("schwab", "AAPL", Currency.USD, Decimal("10")),
        _holding("moomoo_my", "AAPL", Currency.USD, Decimal("20")),
    ]
    cal = [_ev("AAPL", date(2026, 11, 1), Decimal("0.50"), Currency.USD)]
    proj = project_dividends(holdings, cal, _accounts(), _instruments(), year=2026)
    us = proj.by_currency[Currency.USD]
    assert us.declared_gross == Decimal("15.00")  # 30 sh * 0.50
    assert us.declared_net == Decimal("10.50")    # 15 * 0.70 (both DRIP)
    assert us.events == 1


def test_unheld_symbol_contributes_no_bucket() -> None:
    # Calendar event for a symbol with no matching holding adds nothing; that
    # currency is absent from by_currency (contributed stays False).
    holdings = [_holding("schwab", "AAPL", Currency.USD, Decimal("10"))]
    cal = [_ev("MSFT", date(2026, 11, 1), Decimal("0.75"), Currency.USD)]
    proj = project_dividends(holdings, cal, _accounts(), _instruments(), year=2026)
    assert Currency.USD not in proj.by_currency
    assert proj.by_currency == {}


def test_dual_market_account_resolves_dividend_model_per_market() -> None:
    """One account bound to TWO markets applies a DIFFERENT dividend model to each of its
    holdings by the holding's instrument market (the merged Moomoo shape): a US-market
    holding gets DRIP (30% withholding) while an MY-market holding of the SAME account
    gets cash (net = gross).

    The account SCALAR ``dividend_model`` is set to ``drip_us`` -- deliberately WRONG for
    the MY leg. The asserted outcome (US net = 0.7*gross AND MY net = gross, same
    account) is UNREACHABLE under any single scalar (a scalar makes both legs DRIP or
    both cash), so passing proves per-market resolution off ``market_rules``, not the
    fallback scalar.
    """
    merged = Account(
        account_id="moomoo_merged", name="Moomoo (merged)", broker="Moomoo",
        settlement_ccy=Currency.USD, funding_ccy=Currency.MYR,
        dividend_model="drip_us",  # scalar fallback -- wrong for the MY leg on purpose
        market_rules={
            "US": MarketRule(fee_rule_set="moomoo_us", dividend_model="drip_us"),
            "MY": MarketRule(fee_rule_set="moomoo_my", dividend_model="cash"),
        },
    )
    accounts = {merged.account_id: merged}
    instruments = {
        "MSFT": Instrument(symbol="MSFT", market=Market.US, quote_ccy=Currency.USD,
                           sector="Tech", name="Microsoft"),
        "1155": Instrument(symbol="1155", market=Market.MY, quote_ccy=Currency.MYR,
                           sector="Banking", name="Maybank", board=".KL"),
    }
    holdings = [
        _holding("moomoo_merged", "MSFT", Currency.USD, Decimal("10")),
        _holding("moomoo_merged", "1155", Currency.MYR, Decimal("1000")),
    ]
    cal = [
        _ev("MSFT", date(2026, 5, 10), Decimal("0.50"), Currency.USD),
        _ev("1155", date(2026, 4, 15), Decimal("0.30"), Currency.MYR),
    ]
    proj = project_dividends(holdings, cal, accounts, instruments, year=2026)

    us = proj.by_currency[Currency.USD]
    assert us.declared_gross == Decimal("5.00")   # 10 * 0.50
    assert us.declared_net == Decimal("3.50")     # US leg -> drip_us: 5.00 * 0.70
    assert us.events == 1

    my = proj.by_currency[Currency.MYR]
    assert my.declared_gross == Decimal("300.00")  # 1000 * 0.30
    assert my.declared_net == Decimal("300.00")    # MY leg -> cash: net = gross (NOT 0.7)
    assert my.events == 1
