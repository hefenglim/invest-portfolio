"""QA-03 — the FX pool silently dropped NET dividends.

``forex/pools.py``'s own module header states the identity: for one (account, foreign ccy)
the **FX-exposure view** and the **funds view** (``portfolio/cash.py::cash_balances``) are
"equal by construction: they sum the same flows". The manual asserts it as
``fx.pool_equals_funds`` (§8.3). They were not equal: ``foreign_cash_balance`` filtered
``d.type is DividendType.CASH`` while the funds view filters ``CASH_DIVIDEND_TYPES``
(``{CASH, NET}``).

``shared/models/enums.py`` exists *precisely* to stop that drift — "ONE definition for every
replay site ... so they can never drift (found 2026-07-03: NET fell into the shares-branch
and crashed rebuilds)". ``pools.py`` is a replay site that did not read it.

NET is the MY single-tier net-received dividend (``domain-ledger.md``): it is cash that
landed in the account, so a foreign-currency one funds the pool exactly as a CASH one does.
A dropped NET understates the FX exposure by the whole dividend and, through ``spot -
avg_rate``, understates the unrealized FX gain/loss on it.
"""

from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.data_ingestion.store import StoredDividend
from portfolio_dash.forex.pools import foreign_cash_balance
from portfolio_dash.portfolio.cash import cash_balances
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType
from portfolio_dash.shared.models.ledger import Dividend, FXConversion

AAPL = Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                  sector="Tech", name="Apple")
INSTR = {"AAPL": AAPL}

ACQUIRE = [FXConversion(account_id="schwab", date=date(2026, 1, 8), from_ccy=Currency.TWD,
                        from_amount=Decimal("320000"), to_ccy=Currency.USD,
                        to_amount=Decimal("10000"))]


#: One USD dividend of net 500 on a USD-quoted holding, expressed in the two row shapes the
#: two views actually consume in production: ``forex/`` reads the domain ``Dividend``,
#: ``portfolio/cash.py`` reads the persisted ``StoredDividend``. That split is precisely how
#: the filters were able to drift apart, so the test asserts across both shapes, not one.
_PAID = date(2026, 3, 1)
_GROSS, _WITHHELD, _NET = Decimal("715"), Decimal("215"), Decimal("500")


def _shares(kind: DividendType) -> tuple[Decimal | None, Decimal | None]:
    """A DRIP/STOCK row carries reinvest detail; a plain cash row does not."""
    if kind is DividendType.CASH or kind is DividendType.NET:
        return None, None
    return Decimal("2"), Decimal("250")


def _div(kind: DividendType) -> Dividend:
    shares, price = _shares(kind)
    return Dividend(account_id="schwab", symbol="AAPL", date=_PAID, type=kind,
                    gross=_GROSS, withholding=_WITHHELD, net=_NET,
                    reinvest_shares=shares, reinvest_price=price)


def _stored(kind: DividendType) -> StoredDividend:
    shares, price = _shares(kind)
    return StoredDividend(id=1, account_id="schwab", symbol="AAPL", date=_PAID,
                          type=kind.value, gross=_GROSS, withholding=_WITHHELD, net=_NET,
                          reinvest_shares=shares, reinvest_price=price)


@pytest.mark.parametrize("kind", [DividendType.CASH, DividendType.NET, DividendType.DRIP])
def test_the_fx_pool_equals_the_funds_view_for_every_dividend_kind(
    kind: DividendType,
) -> None:
    """The identity ``pools.py`` claims in its own header, swept over the kinds.

    Measured before the fix: CASH 500 vs 500 (agree); **NET 0 vs 500 (disagree by the whole
    dividend)**; DRIP 0 vs 0 (agree — it moves no cash, and both views know that).
    """
    fx_view = foreign_cash_balance([], [_div(kind)], ACQUIRE, INSTR, Currency.USD)
    funds_view = cash_balances(
        [], ACQUIRE, [], [_stored(kind)], INSTR)[("schwab", Currency.USD)]
    assert fx_view == funds_view


def test_a_foreign_NET_dividend_funds_the_pool() -> None:
    """The absolute figure, not just the identity — 10,000 acquired + 500 received."""
    assert foreign_cash_balance(
        [], [_div(DividendType.NET)], ACQUIRE, INSTR, Currency.USD) == Decimal("10500")


def test_a_DRIP_dividend_still_moves_no_cash() -> None:
    """Guard on the other side of the fix: widening the filter must not sweep DRIP in.

    A DRIP nets to zero (the cash is immediately reinvested), so it is deliberately absent
    from ``CASH_DIVIDEND_TYPES`` and must stay out of the balance.
    """
    assert foreign_cash_balance(
        [], [_div(DividendType.DRIP)], ACQUIRE, INSTR, Currency.USD) == Decimal("10000")


def test_a_NET_dividend_in_the_HOME_currency_never_enters_the_foreign_pool() -> None:
    """The currency test is unchanged: only the pool's own currency counts."""
    tw = Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                    sector="Tech", name="TSMC")
    div = Dividend(account_id="schwab", symbol="2330", date=date(2026, 3, 1),
                   type=DividendType.NET, gross=Decimal("500"), withholding=Decimal("0"),
                   net=Decimal("500"))
    assert foreign_cash_balance(
        [], [div], ACQUIRE, {**INSTR, "2330": tw}, Currency.USD) == Decimal("10000")
