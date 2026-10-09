"""QA-06 — a share-adding dividend onto a FLAT position books, and says that it did.

Owner ruling 2026-09-01: **記錄並標記**. The alternative on the table was refusing it the way
E24 refuses a payout on a vacated ticker; the owner chose to book it, because entitlement on the
ex-date with a flat position on the payment date is ordinary — the CASH branch already treats it
that way (audit H2, 2026-07-26).

What the audit actually found was not a wrong number. Every figure on the revived position is
individually correct under DRIP accounting:

    shares 1.4 · original_cost_total 0 · avg 0 · payback_ratio 0 · unrealized = price × 1.4

and **every flag was clean**, so it was indistinguishable from an ordinary holding whose entire
market value happened to be profit. The flag is the whole fix.
"""

from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.shared.corporate_actions import CorporateAction, CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import (
    Dividend,
    LedgerBundle,
    Transaction,
)

_ZERO = Decimal("0")

_INSTRUMENTS = {
    "AAPL": Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                       sector="Tech", name="Apple"),
    "NEWCO": Instrument(symbol="NEWCO", market=Market.US, quote_ccy=Currency.USD,
                        sector="Tech", name="NewCo"),
}


def _buy(qty: str, price: str, day: date) -> Transaction:
    return Transaction(account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal(qty), price=Decimal(price),
                       fees=_ZERO, tax=_ZERO, trade_date=day)


def _sell(qty: str, price: str, day: date) -> Transaction:
    return Transaction(account_id="schwab", symbol="AAPL", side=Side.SELL,
                       quantity=Decimal(qty), price=Decimal(price),
                       fees=_ZERO, tax=_ZERO, trade_date=day)


def _drip(shares: str, day: date) -> Dividend:
    return Dividend(account_id="schwab", symbol="AAPL", date=day,
                    type=DividendType.DRIP, gross=Decimal("100"),
                    withholding=Decimal("30"), net=Decimal("70"),
                    reinvest_shares=Decimal(shares), reinvest_price=Decimal("50"))


def _closed_then_drip() -> LedgerBundle:
    """Bought, fully sold, and THEN the DRIP lands — the audit's exact shape."""
    return LedgerBundle(
        transactions=[_buy("10", "100", date(2026, 1, 5)),
                      _sell("10", "120", date(2026, 2, 1))],
        dividends=[_drip("1.4", date(2026, 3, 10))],
        instruments=_INSTRUMENTS,
    )


def test_the_payout_is_booked_not_refused() -> None:
    """記錄: the shares really may have been received, so they appear."""
    book = build_book(_closed_then_drip())
    held = [h for h in book.holdings if h.symbol == "AAPL"]
    assert len(held) == 1, "the ruling is to BOOK it — a refusal would drop the position"
    assert held[0].shares == Decimal("1.4")


def test_the_revived_position_is_flagged() -> None:
    """標記: the fix. Without this the row is indistinguishable from a real holding."""
    book = build_book(_closed_then_drip())
    (h,) = [x for x in book.holdings if x.symbol == "AAPL"]
    assert h.revived_by_dividend is True


def test_the_flag_names_the_shape_the_audit_found() -> None:
    """Pin the numbers, so a future change that makes them ordinary also fails here.

    Each of these is individually correct; together they are the disguise.
    """
    book = build_book(_closed_then_drip())
    (h,) = [x for x in book.holdings if x.symbol == "AAPL"]
    assert h.original_cost_total == _ZERO
    assert h.adjusted_cost_total == _ZERO
    assert h.original_avg == _ZERO
    assert h.payback_ratio == _ZERO, "guarded div-by-zero, not a computed ratio"


def test_an_ordinary_drip_onto_a_live_position_is_NOT_flagged() -> None:
    """The counter-example that makes the flag mean something.

    Same dividend, same $0-cost shares — but the lot was never flat, so nothing is unusual and
    a flag here would be noise. A flag raised on every DRIP would be worthless.
    """
    bundle = LedgerBundle(
        transactions=[_buy("10", "100", date(2026, 1, 5))],
        dividends=[_drip("1.4", date(2026, 3, 10))],
        instruments=_INSTRUMENTS,
    )
    (h,) = [x for x in build_book(bundle).holdings if x.symbol == "AAPL"]
    assert h.revived_by_dividend is False
    assert h.shares == Decimal("11.4")
    assert h.original_cost_total == Decimal("1000"), "the live basis is untouched"


def test_a_partial_sell_leaving_shares_is_NOT_flagged() -> None:
    """The boundary is `shares == 0`, not `a sell happened`."""
    bundle = LedgerBundle(
        transactions=[_buy("10", "100", date(2026, 1, 5)),
                      _sell("9", "120", date(2026, 2, 1))],
        dividends=[_drip("1.4", date(2026, 3, 10))],
        instruments=_INSTRUMENTS,
    )
    (h,) = [x for x in build_book(bundle).holdings if x.symbol == "AAPL"]
    assert h.revived_by_dividend is False


def test_a_post_close_CASH_dividend_keeps_its_own_H2_treatment() -> None:
    """QA-06 must not leak into the branch beside it.

    A CASH dividend on a closed position is realized INCOME (audit H2) and creates no holding
    at all — so there is nothing to flag, and the flag must not appear.
    """
    bundle = LedgerBundle(
        transactions=[_buy("10", "100", date(2026, 1, 5)),
                      _sell("10", "120", date(2026, 2, 1))],
        dividends=[Dividend(account_id="schwab", symbol="AAPL", date=date(2026, 3, 10),
                            type=DividendType.CASH, gross=Decimal("70"),
                            withholding=_ZERO, net=Decimal("70"))],
        instruments=_INSTRUMENTS,
    )
    book = build_book(bundle)
    assert [h for h in book.holdings if h.symbol == "AAPL"] == []
    assert any(r.kind == "dividend" and r.realized == Decimal("70")
               for r in book.realized.rows)


def test_the_flag_survives_an_exchange_onto_the_successor() -> None:
    """The doubt travels with the position, or the EXCHANGE launders it.

    Reasoned, not copied: an EXCHANGE moves the zero-basis shares to Q and zeroes the source,
    so the ONLY position the owner will look at afterwards is the destination.
    """
    bundle = LedgerBundle(
        transactions=[_buy("10", "100", date(2026, 1, 5)),
                      _sell("10", "120", date(2026, 2, 1))],
        dividends=[_drip("1.4", date(2026, 3, 10))],
        actions=[CorporateAction(
            account_id="schwab", date=date(2026, 4, 1),
            kind=CorporateActionKind.EXCHANGE,
            from_symbol="AAPL", to_symbol="NEWCO",
            ratio_to=Decimal("1"), ratio_from=Decimal("1"),
        )],
        instruments=_INSTRUMENTS,
    )
    (h,) = [x for x in build_book(bundle).holdings if x.symbol == "NEWCO"]
    assert h.revived_by_dividend is True, "an EXCHANGE must not launder the flag"


@pytest.mark.parametrize("kind", [DividendType.DRIP, DividendType.STOCK])
def test_both_share_adding_types_are_covered(kind: DividendType) -> None:
    """STOCK (配股) adds shares with no cash and reaches the same branch as DRIP."""
    bundle = LedgerBundle(
        transactions=[_buy("10", "100", date(2026, 1, 5)),
                      _sell("10", "120", date(2026, 2, 1))],
        dividends=[Dividend(account_id="schwab", symbol="AAPL", date=date(2026, 3, 10),
                            type=kind, gross=Decimal("100"), withholding=_ZERO,
                            net=Decimal("100"), reinvest_shares=Decimal("2"),
                            reinvest_price=Decimal("50"))],
        instruments=_INSTRUMENTS,
    )
    (h,) = [x for x in build_book(bundle).holdings if x.symbol == "AAPL"]
    assert h.revived_by_dividend is True


# --- §4.4 transfer rules, one replay per action kind ---------------------------------------
# The normative table in `docs/spec/2026-08-06-corporate-actions.md` §4.4 now carries a row for
# `revived_by_dividend`, and its guard (test_corporate_actions.py) refuses a field without one.
# A row is a claim, so each of the three kinds gets a replay that holds it to the claim.

def _action(kind: CorporateActionKind, to_symbol: str, to_: str, from_: str) -> CorporateAction:
    return CorporateAction(
        account_id="schwab", date=date(2026, 4, 1), kind=kind,
        from_symbol="AAPL", to_symbol=to_symbol,
        ratio_to=Decimal(to_), ratio_from=Decimal(from_),
        cost_carry=Decimal("0.4") if kind is CorporateActionKind.SPINOFF else None,
    )


def test_split_leaves_the_flag_on_the_same_position() -> None:
    """§4.4 SPLIT: `unchanged`. The position is the same object; only its share count moves."""
    bundle = LedgerBundle(
        transactions=[_buy("10", "100", date(2026, 1, 5)),
                      _sell("10", "120", date(2026, 2, 1))],
        dividends=[_drip("1.4", date(2026, 3, 10))],
        actions=[_action(CorporateActionKind.SPLIT, "AAPL", "2", "1")],
        instruments=_INSTRUMENTS,
    )
    (h,) = [x for x in build_book(bundle).holdings if x.symbol == "AAPL"]
    assert h.revived_by_dividend is True
    assert h.shares == Decimal("2.8"), "1.4 × 2/1 — the split still applies normally"


def test_spinoff_carries_the_flag_to_the_child() -> None:
    """§4.4 SPINOFF: OR-ed into the child.

    The carve is `basis × cost_carry`, and the parent's basis is zero — so the child is funded
    with zero as well and inherits exactly the same doubt. A child that looked clean would be
    the launder this rule exists to prevent.
    """
    bundle = LedgerBundle(
        transactions=[_buy("10", "100", date(2026, 1, 5)),
                      _sell("10", "120", date(2026, 2, 1))],
        dividends=[_drip("1.4", date(2026, 3, 10))],
        actions=[_action(CorporateActionKind.SPINOFF, "NEWCO", "1", "1")],
        instruments=_INSTRUMENTS,
    )
    book = build_book(bundle)
    parent = next(x for x in book.holdings if x.symbol == "AAPL")
    child = next(x for x in book.holdings if x.symbol == "NEWCO")
    assert parent.revived_by_dividend is True, "the parent keeps its own"
    assert child.revived_by_dividend is True, "and the child inherits it"
    assert child.original_cost_total == _ZERO, "0.4 × 0 — a carve of nothing"
