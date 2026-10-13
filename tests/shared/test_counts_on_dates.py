"""`LedgerBundle.counts_on_dates()` names exactly the days `LedgerBundle.through()` changes.

The daily trend replay rebuilds the book only on those days and reuses it on every other
(2026-10-13). That is exact only while the two agree field for field: a ledger that
`valued_as_of` cuts but `counts_on_dates` forgets would let a stale book stand on that
ledger's dates — a wrong trend that looks right. So this pins the agreement for every list
field of the bundle, and fails when a new list field arrives without a row here.
"""

import dataclasses
from datetime import date, timedelta
from decimal import Decimal

from portfolio_dash.shared.corporate_actions import (
    CorporateAction,
    CorporateActionKind,
    UnreadableAction,
)
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import (
    Dividend,
    LedgerBundle,
    OpeningInventory,
    Transaction,
)

D = Decimal


def _bundle() -> LedgerBundle:
    """One row in every list field, each on its own date."""
    return LedgerBundle(
        transactions=[Transaction(account_id="schwab", symbol="AAA", side=Side.BUY,
                                  quantity=D("10"), price=D("100"), fees=D("0"),
                                  tax=D("0"), trade_date=date(2026, 6, 3))],
        dividends=[
            Dividend(account_id="schwab", symbol="AAA", date=date(2026, 6, 8),
                     type=DividendType.CASH, gross=D("5"), withholding=D("0"), net=D("5")),
            # a 配股 with a known ex-date counts from the EX-date (R6), not its payment date
            Dividend(account_id="tw_broker", symbol="BBB", date=date(2026, 6, 20),
                     ex_date=date(2026, 6, 12), type=DividendType.STOCK, gross=D("0"),
                     withholding=D("0"), net=D("0"), reinvest_shares=D("10")),
        ],
        opening=[OpeningInventory(account_id="tw_broker", symbol="BBB", shares=D("100"),
                                  original_cost_total=D("9000"),
                                  build_date=date(2026, 6, 1))],
        actions=[CorporateAction(account_id="schwab", date=date(2026, 6, 5),
                                 kind=CorporateActionKind.SPLIT, from_symbol="AAA",
                                 to_symbol="AAA", ratio_to=D("2"), ratio_from=D("1"))],
        unreadable_actions=[UnreadableAction(account_id="tw_broker", date=date(2026, 6, 15),
                                             kind="SPLIT", from_symbol="BBB",
                                             to_symbol="BBB", reason="ratio_to 必須是正整數")],
    )


def test_every_list_field_is_exercised() -> None:
    """A new ledger field must get a row above, or the agreement below proves nothing for it."""
    bundle = _bundle()
    for f in dataclasses.fields(LedgerBundle):
        value = getattr(bundle, f.name)
        if isinstance(value, list):
            assert value, f"LedgerBundle.{f.name} has no row in this fixture"


def test_through_changes_exactly_on_the_counts_on_dates() -> None:
    bundle = _bundle()
    expected = {date(2026, 6, 1), date(2026, 6, 3), date(2026, 6, 5), date(2026, 6, 8),
                date(2026, 6, 12), date(2026, 6, 15)}
    assert bundle.counts_on_dates() == expected   # the 配股 counts from 6/12, not 6/20
    day = date(2026, 5, 30)
    while day <= date(2026, 6, 25):
        changed = bundle.through(day) != bundle.through(day - timedelta(days=1))
        assert changed == (day in expected), day
        day += timedelta(days=1)


def test_empty_bundle_has_no_dates() -> None:
    assert LedgerBundle().counts_on_dates() == set()
