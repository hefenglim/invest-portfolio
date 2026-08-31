"""R3 / QA-05 — 已回本 must mean dividends repaid the cost, not "the basis reached zero".

``api/routers/symbol.py`` decided the 已回本 label with ``adjusted_cost_total <= 0``, plus a
``not short_open`` gate that was added because a declared short's basis is NEGATIVE by
construction. That gate closed ONE of the two ways a basis reaches zero or below without a
single dividend having been paid. The other is 賣超: an acked undeclared oversell DISCARDS
the position's cost basis (``build_book`` sets both totals to 0), so ``<= 0`` is true and
the drawer prints 「已回本・配息已完全沖減成本」 directly above 「累計配息 0」 — on the very
position whose 賣超 badge is asking the owner to fix it.

The three cases this module discriminates, all through the real replay + the real route:

===============  ==================  ==============  ===============
position         adjusted_cost_total  payback_ratio   已回本
===============  ==================  ==============  ===============
genuine payback  −1,980 (legal)      1.1976…         **True**
賣超              0 (DISCARDED)       0               False
declared short   −5,000 (proceeds)   0               False
===============  ==================  ==============  ===============

``payback_ratio`` is the discriminator because it is the ratio the label is ABOUT:
cumulative cash dividends over original cost (manual §6.4). The ``not short_open`` gate is
kept, not replaced — a short's ratio happens to be 0 today, but the reason it must never
be labelled 已回本 is its negative basis, and that reason should stay written down.
"""

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_dividend,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW, DashboardClientFactory

_ZERO = Decimal("0")
_ONE = Decimal("1")


def _seed_three_ways_to_a_non_positive_basis(conn: sqlite3.Connection) -> None:
    """One TW account, three positions whose adjusted basis is <= 0 for three reasons."""
    seed_accounts(conn)
    for symbol, name in (("PAYBK", "Payback"), ("OVRSL", "Oversold"), ("SHRTY", "Short")):
        upsert_instrument(conn, Instrument(symbol=symbol, market=Market.TW,
                                           quote_ccy=Currency.TWD, sector="Semi",
                                           name=name, board="TWSE"))

    # (a) GENUINE PAYBACK — original 100×100 + 20 fee = 10,020; cash dividends 12,000
    #     drive the adjusted basis to −1,980 (explicitly legal, never floored).
    insert_transaction(conn, account_id="tw_broker", symbol="PAYBK", side=Side.BUY,
                       quantity=Decimal("100"), price=Decimal("100"),
                       fees=Decimal("20"), tax=_ZERO, trade_date=date(2026, 1, 5))
    insert_dividend(conn, account_id="tw_broker", symbol="PAYBK", div_date=date(2026, 3, 1),
                    div_type="CASH", gross=Decimal("12000"), withholding=_ZERO,
                    net=Decimal("12000"))

    # (b) 賣超 — an UNDECLARED sell beyond holdings. Written straight to the ledger, which
    #     is what an acked oversell leaves behind: shares −50, cost basis DISCARDED, and
    #     not one dividend ever paid.
    insert_transaction(conn, account_id="tw_broker", symbol="OVRSL", side=Side.BUY,
                       quantity=Decimal("100"), price=Decimal("100"),
                       fees=_ZERO, tax=_ZERO, trade_date=date(2026, 1, 5))
    insert_transaction(conn, account_id="tw_broker", symbol="OVRSL", side=Side.SELL,
                       quantity=Decimal("150"), price=Decimal("110"),
                       fees=_ZERO, tax=_ZERO, trade_date=date(2026, 2, 1))

    # (c) DECLARED SHORT — a real, priced position holding the proceeds as a negative basis.
    insert_transaction(conn, account_id="tw_broker", symbol="SHRTY", side=Side.SELL,
                       quantity=Decimal("50"), price=Decimal("100"),
                       fees=_ZERO, tax=_ZERO, trade_date=date(2026, 1, 5),
                       short_sale=True)

    upsert_prices(conn, [
        PriceRow(instrument=s, market=Market.TW, as_of=date(2026, 6, 9),
                 close=Decimal("120"), source="test")
        for s in ("PAYBK", "OVRSL", "SHRTY")
    ], fetched_at=GOLDEN_NOW)
    conn.commit()


def _detail(factory: DashboardClientFactory, symbol: str
            ) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(aggregate position, the single per-account row)`` for *symbol*."""
    body = factory(_seed_three_ways_to_a_non_positive_basis).get(
        f"/api/symbol/{symbol}/detail").json()
    accounts = body["position_accounts"]
    assert len(accounts) == 1, "fixture holds each symbol in exactly one account"
    return body["position"], accounts[0]


def test_genuine_dividend_payback_is_labelled_recovered(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """Control: the case the label exists for must keep it (both wire sites)."""
    position, account = _detail(dashboard_client_factory, "PAYBK")
    assert Decimal(account["adjusted_cost_total"]) == Decimal("-1980")
    assert Decimal(account["dividend_portion"]) == Decimal("12000")
    assert Decimal(account["payback_ratio"]) > _ONE
    assert account["fully_recovered"] is True
    assert position["fully_recovered"] is True


def test_oversold_discarded_basis_is_not_labelled_recovered(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The defect: zero cost + zero dividends read as 「配息已完全沖減成本」."""
    position, account = _detail(dashboard_client_factory, "OVRSL")
    assert account["oversold"] is True and account["short_open"] is False
    # The basis was DISCARDED, not repaid — both totals are 0 and no dividend exists.
    assert Decimal(account["adjusted_cost_total"]) == _ZERO
    assert Decimal(account["dividend_portion"]) == _ZERO
    assert Decimal(account["payback_ratio"]) == _ZERO
    assert account["fully_recovered"] is False
    assert position["fully_recovered"] is False


def test_open_declared_short_is_not_labelled_recovered(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """Regression: the gate that was already right must stay right."""
    position, account = _detail(dashboard_client_factory, "SHRTY")
    assert account["short_open"] is True
    assert Decimal(account["adjusted_cost_total"]) == Decimal("-5000")
    assert Decimal(account["payback_ratio"]) == _ZERO
    assert account["fully_recovered"] is False
    assert position["fully_recovered"] is False


def test_the_label_never_contradicts_the_payback_figure_beside_it(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The invariant, stated once: 已回本 ⟹ the 回本進度 printed next to it is >= 100%.

    The rendered defect was a label and its own evidence disagreeing on one card
    (「已回本」 above 「回本進度 0%」), so the guard is written as that agreement rather
    than as three separate expected values.
    """
    for symbol in ("PAYBK", "OVRSL", "SHRTY"):
        position, account = _detail(dashboard_client_factory, symbol)
        for row in (position, account):
            if row["fully_recovered"]:
                assert Decimal(row["payback_ratio"]) >= _ONE, symbol
