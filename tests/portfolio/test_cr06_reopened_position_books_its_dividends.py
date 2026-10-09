"""CR-06 (pre-merge review, 2026-10-04): a position an EXCHANGE vacated and something later
REOPENED is live again — its dividends book.

E24 (D32) refuses a dividend on a symbol an EXCHANGE moved away, keyed on
``_Position.vacated_to``. That marker was set by the EXCHANGE and NEVER cleared, so once the
old ticker was reopened — a later buy, an opening inventory, or a reverse EXCHANGE back onto
it — every later dividend on the live position was still refused as 「已換股」:

* strict path (重算 / tax package / 試算) → ``UnbookableLedgerError``;
* dashboard path → the payout dropped from cost basis and XIRR, and the 待釐清 flag sent down
  ``_follow_exchange_chain`` to the position the owner no longer holds.

The marker now describes the position's CURRENT life: anything that puts the position back in
play clears it. The E24 refusal on a still-vacated ticker is unchanged
(``test_dividend_after_exchange.py``).
"""

import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_corporate_action,
    insert_dividend,
    insert_transaction,
    load_ledger_bundle,
    upsert_instrument,
    upsert_opening,
)
from portfolio_dash.portfolio.cost_basis import UnbookableLedgerError, build_book
from portfolio_dash.portfolio.results import Book, Holding
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import DashboardClientFactory

D = Decimal
EXCHANGE_DAY = date(2026, 5, 1)
REOPEN_DAY = date(2026, 5, 10)
PAY_DAY = date(2026, 5, 20)
NET = D("7")


def _base(c: sqlite3.Connection) -> None:
    c.execute(
        "INSERT OR IGNORE INTO accounts (account_id, name, broker, settlement_ccy, "
        "funding_ccy, fee_rule_set, dividend_model) VALUES "
        "('schwab','Schwab','Schwab','USD','TWD','schwab_us','drip_us')"
    )
    for sym in ("OLD", "NEW"):
        upsert_instrument(c, Instrument(symbol=sym, market=Market.US,
                                        quote_ccy=Currency.USD, sector="Tech", name=sym))
    insert_transaction(c, account_id="schwab", symbol="OLD", side=Side.BUY,
                       quantity=D("100"), price=D("50"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 1, 10))
    insert_corporate_action(c, account_id="schwab", action_date=EXCHANGE_DAY,
                            kind=CorporateActionKind.EXCHANGE, from_symbol="OLD",
                            to_symbol="NEW", ratio_to=D("1"), ratio_from=D("1"))
    c.commit()


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    _base(c)
    return c


def _pay(conn: sqlite3.Connection, symbol: str, when: date = PAY_DAY,
         net: Decimal = NET) -> None:
    insert_dividend(conn, account_id="schwab", symbol=symbol, div_date=when, div_type="NET",
                    gross=net, withholding=D("0"), net=net)
    conn.commit()


def _holding(conn: sqlite3.Connection, symbol: str, *,
             strict: bool) -> tuple[Book, Holding | None]:
    book = build_book(load_ledger_bundle(conn), allow_oversell=not strict)
    return book, next((h for h in book.holdings if h.symbol == symbol), None)


def test_a_buy_on_the_old_ticker_reopens_it_and_its_dividend_books(
    conn: sqlite3.Connection,
) -> None:
    insert_transaction(conn, account_id="schwab", symbol="OLD", side=Side.BUY,
                       quantity=D("10"), price=D("40"), fees=D("0"), tax=D("0"),
                       trade_date=REOPEN_DAY)
    _pay(conn, "OLD")
    book, old = _holding(conn, "OLD", strict=True)           # strict: must not raise
    assert old is not None and old.shares == D("10")
    assert old.adjusted_cost_total == D("400") - D("7")       # the payout reduced the basis
    assert old.unbookable_dividend is False
    assert book.refused_dividends == []
    _book, new = _holding(conn, "NEW", strict=False)
    assert new is not None and new.unbookable_dividend is False   # nothing misattributed


def test_an_opening_on_the_old_ticker_reopens_it(conn: sqlite3.Connection) -> None:
    upsert_opening(conn, account_id="schwab", symbol="OLD", shares=D("5"),
                   original_cost_total=D("200"), build_date=REOPEN_DAY)
    conn.commit()
    _pay(conn, "OLD")
    book, old = _holding(conn, "OLD", strict=True)
    assert old is not None and old.adjusted_cost_total == D("200") - D("7")
    assert book.refused_dividends == []


def test_a_reverse_exchange_reopens_the_old_ticker_and_the_flag_follows_the_live_one(
    conn: sqlite3.Connection,
) -> None:
    """OLD → NEW, then NEW → OLD (a rename reverted). OLD is the live position again: its
    dividend books, and a dividend on the now-vacated NEW is refused with the 待釐清 flag on
    OLD — not on NEW, which the holdings loop drops."""
    insert_corporate_action(conn, account_id="schwab", action_date=REOPEN_DAY,
                            kind=CorporateActionKind.EXCHANGE, from_symbol="NEW",
                            to_symbol="OLD", ratio_to=D("1"), ratio_from=D("1"))
    conn.commit()
    _pay(conn, "OLD")
    book, old = _holding(conn, "OLD", strict=True)
    assert old is not None and old.shares == D("100")
    assert old.adjusted_cost_total == D("5000") - D("7")
    assert book.refused_dividends == []

    _pay(conn, "NEW", when=date(2026, 5, 25), net=D("3"))
    with pytest.raises(UnbookableLedgerError, match="NEW"):
        build_book(load_ledger_bundle(conn), allow_oversell=False)
    book, old = _holding(conn, "OLD", strict=False)
    assert [d.symbol for d in book.refused_dividends] == ["NEW"]
    assert old is not None and old.unbookable_dividend is True
    assert old.adjusted_cost_total == D("5000") - D("7")      # OLD's own payout still booked


def test_a_still_vacated_ticker_keeps_the_e24_refusal(conn: sqlite3.Connection) -> None:
    """Scope guard: nothing reopened OLD, so D32's refusal stands exactly as before."""
    _pay(conn, "OLD")
    with pytest.raises(UnbookableLedgerError, match="OLD"):
        build_book(load_ledger_bundle(conn), allow_oversell=False)
    book, new = _holding(conn, "NEW", strict=False)
    assert new is not None and new.unbookable_dividend is True
    assert [d.symbol for d in book.refused_dividends] == ["OLD"]


def test_recompute_accepts_the_reopened_ledger(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The strict door the owner presses: 重算 answered 422 「已換股」 for a live position."""

    def seed(c: sqlite3.Connection) -> None:
        seed_accounts(c)
        _base(c)
        insert_transaction(c, account_id="schwab", symbol="OLD", side=Side.BUY,
                           quantity=D("10"), price=D("40"), fees=D("0"), tax=D("0"),
                           trade_date=REOPEN_DAY)
        _pay(c, "OLD")

    client = dashboard_client_factory(seed)
    r = client.post("/api/actions/recompute")
    assert r.status_code == 200, r.json()
