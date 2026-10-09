"""R3 / QA-15 — 年度股利預估 must not multiply a share count the app refuses to trust.

``project_dividends`` computes ``h.shares * ev.cash_amount`` for every holding with
positive shares. ``value_holdings`` (``portfolio/pnl.py``) already refuses to value
exactly two of those holdings, and states the deciding question in its own docstring:
**are the shares right?**

* ``unbookable_action`` — a corporate action was skipped, so ``shares`` is in PRE-action
  terms while the declared per-share dividend amount is, like the price, POST-action;
* ``oversold`` — the position went negative and its basis was discarded; the ledger is
  known to be missing a buy or an opening, so the count is not the owner's real position.

The projection did neither, so it renders a clean, precise, wrong number. This module
builds the corporate-action scenario end to end through the REAL replay (``build_book``)
rather than hand-writing a flagged ``Holding``, so the magnitude below is measured, not
asserted: the same ledger, with the same 3-for-1 split, projects 450 when the action is
applied and 150 when it is refused — understated by the whole ratio.
"""

from datetime import date
from decimal import Decimal

from portfolio_dash.data_ingestion.config_seed import DEFAULT_ACCOUNTS
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.portfolio.dashboard_models import ExDividendItem
from portfolio_dash.portfolio.dividends import project_dividends
from portfolio_dash.portfolio.results import Holding
from portfolio_dash.shared.corporate_actions import (
    CorporateAction,
    CorporateActionKind,
    UnreadableAction,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Account, Instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.models.ledger import LedgerBundle, Transaction

_ZERO = Decimal("0")
_BUY_DAY = date(2026, 1, 5)
_ACTION_DAY = date(2026, 3, 2)
#: Declared cash per share, POST-split (what the provider publishes after a 3-for-1).
_PER_SHARE = Decimal("1.5")

_INSTRUMENTS = {
    "2330": Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                       sector="Semi", name="TSMC"),
}


def _accounts() -> dict[str, Account]:
    return {
        a.account_id: Account(
            account_id=a.account_id, name=a.name, broker=a.broker,
            settlement_ccy=a.settlement_ccy, funding_ccy=a.funding_ccy,
            dividend_model=a.dividend_model,
        )
        for a in DEFAULT_ACCOUNTS
    }


def _calendar() -> list[ExDividendItem]:
    return [ExDividendItem(symbol="2330", name="TSMC", ex_date=date(2026, 6, 1),
                           cash_amount=_PER_SHARE, currency=Currency.TWD, source="test")]


def _tx(side: Side, qty: str, day: date) -> Transaction:
    return Transaction(account_id="tw_broker", symbol="2330", side=side,
                       quantity=Decimal(qty), price=Decimal("500"),
                       fees=_ZERO, tax=_ZERO, trade_date=day)


def _split() -> CorporateAction:
    """The SAME 3-for-1 split in both arms — only its readability differs."""
    return CorporateAction(account_id="tw_broker", date=_ACTION_DAY,
                           kind=CorporateActionKind.SPLIT, from_symbol="2330",
                           to_symbol="2330", ratio_to=Decimal("3"), ratio_from=Decimal("1"))


def _unreadable_split() -> UnreadableAction:
    """The same ledger row as ``_split()``, as the loader records one it cannot convert.

    Reachable in production: ``UnreadableAction`` exists because a stored row with a
    non-integer ratio term used to 500 every page, and dropping it silently was rejected
    for the reason this test measures — "a silently omitted action yields a share count
    wrong by the ratio that looks entirely normal".
    """
    return UnreadableAction(account_id="tw_broker", date=_ACTION_DAY, kind="SPLIT",
                            from_symbol="2330", to_symbol="2330",
                            reason="ratio_to 必須是正整數")


def _project(holdings: list[Holding]) -> dict[Currency, Decimal]:
    proj = project_dividends(holdings, _calendar(), _accounts(), _INSTRUMENTS, year=2026)
    return {ccy: c.declared_gross for ccy, c in proj.by_currency.items()}


# --- the control arm: the action is applied, the projection is right -----------------

def test_control_an_applied_split_projects_on_the_post_action_share_count() -> None:
    book = build_book(
        LedgerBundle(transactions=[_tx(Side.BUY, "100", _BUY_DAY)],
                     instruments=dict(_INSTRUMENTS), actions=[_split()]),
        allow_oversell=True,
    )
    (held,) = book.holdings
    assert held.shares == Decimal("300") and held.unbookable_action is False
    # 300 shares × 1.5 = 450 — the honest figure this scenario is measured against.
    assert _project(book.holdings) == {Currency.TWD: Decimal("450.0")}


# --- the defect: the action is refused, the projection is not ------------------------

def test_unbookable_action_holding_is_excluded_from_the_projection() -> None:
    """The refused split leaves 100 PRE-action shares; 100 × 1.5 = 150, i.e. 1/3 of 450."""
    book = build_book(
        LedgerBundle(transactions=[_tx(Side.BUY, "100", _BUY_DAY)],
                     instruments=dict(_INSTRUMENTS),
                     unreadable_actions=[_unreadable_split()]),
        allow_oversell=True,
    )
    (held,) = book.holdings
    assert held.shares == Decimal("100") and held.unbookable_action is True
    # Before the fix this returned {TWD: 150.0} — a clean, precise number understated by
    # the whole 3-for-1 ratio, printed beside no warning of any kind.
    assert _project(book.holdings) == {}


def test_oversold_holding_is_excluded_from_the_projection() -> None:
    """賣超 is STICKY: a later buy nets the shares POSITIVE, so ``shares > 0`` lets it in.

    The basis was discarded and the ledger is missing a buy or an opening, so the count
    is not the owner's real position — the same reason ``value_holdings`` suppresses it.
    """
    book = build_book(
        LedgerBundle(
            transactions=[
                _tx(Side.BUY, "100", _BUY_DAY),
                _tx(Side.SELL, "150", date(2026, 2, 1)),   # undeclared oversell
                _tx(Side.BUY, "200", date(2026, 4, 1)),    # nets back to +150
            ],
            instruments=dict(_INSTRUMENTS),
        ),
        allow_oversell=True,
    )
    (held,) = book.holdings
    assert held.shares == Decimal("150") and held.oversold is True
    # Before the fix: {TWD: 225.0}.
    assert _project(book.holdings) == {}


def test_excluded_holdings_do_not_count_their_event_either() -> None:
    """The event counter must agree with the money — an excluded position is not an event."""
    book = build_book(
        LedgerBundle(transactions=[_tx(Side.BUY, "100", _BUY_DAY)],
                     instruments=dict(_INSTRUMENTS),
                     unreadable_actions=[_unreadable_split()]),
        allow_oversell=True,
    )
    proj = project_dividends(book.holdings, _calendar(), _accounts(), _INSTRUMENTS,
                             year=2026)
    assert proj.by_currency == {}


def test_a_clean_sibling_holding_still_projects(  # containment, per pnl.py's own rule
) -> None:
    """Exclusion is PER POSITION: one flagged holding must not blank the whole currency."""
    flagged = build_book(
        LedgerBundle(transactions=[_tx(Side.BUY, "100", _BUY_DAY)],
                     instruments=dict(_INSTRUMENTS),
                     unreadable_actions=[_unreadable_split()]),
        allow_oversell=True,
    ).holdings
    clean = build_book(
        LedgerBundle(transactions=[_tx(Side.BUY, "40", _BUY_DAY)],
                     instruments=dict(_INSTRUMENTS)),
        allow_oversell=True,
    ).holdings
    # Same symbol, different account, so only the flagged one is dropped.
    clean = [h.model_copy(update={"account_id": "moomoo_my"}) for h in clean]
    assert _project(flagged + clean) == {Currency.TWD: Decimal("60.0")}
