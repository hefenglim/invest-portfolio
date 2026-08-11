"""E24 (D32) — a dividend on a symbol an EXCHANGE moved away.

**Specified, owner-approved 2026-08-10, and never implemented.** The audit called it "a live
defect in W3"; P0 recorded the decision in §8 and did not close the code path. Found again
2026-08-11 while writing the accounting manual, which is the second time this row was
discovered by someone reading rather than by a test — so the test comes first now.

§4.2 leaves the exchanged-away source **in the position map with zeroed fields** (required, so
a later buy on the old ticker cannot reopen it carrying `−ε` of basis). The dividend branch
therefore finds `existing is not None` and `short_shares == 0`, so **neither** the
closed-position refusal nor the dividend-on-short refusal applies, and the payment books:

* **CASH / NET** → post-close realized **income on a dead ticker**;
* **DRIP / STOCK** → `existing.shares += reinvest_shares` **resurrects** the position at
  `avg = 0`. It is delisted, so it never gets a price — and ONE unpriced holding makes
  `returns.py` return `rate=None` for the **whole portfolio**. The headline XIRR goes blank
  indefinitely, with no visible cause.

The second is why this is not a cosmetic row: the damage is portfolio-wide and permanent.

**Narrow scope (D32).** An ordinary sold-out position keeps today's post-close-income
behaviour — the 2026-07-26 audit's H2 ruling, which was itself a deliberate fix. Only the
action-vacated state changes, which is why the guard keys on *how* the position reached zero
and not on `shares == 0`.
"""

import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.store import (
    insert_corporate_action,
    insert_dividend,
    insert_transaction,
    load_ledger_bundle,
    upsert_instrument,
)
from portfolio_dash.portfolio.cost_basis import UnbookableLedgerError, build_book
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

D = Decimal
EXCHANGE_DAY = date(2026, 5, 1)
PAY_DAY = date(2026, 5, 20)          # AFTER the exchange — the whole point


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    c.execute(
        "INSERT INTO accounts (account_id, name, broker, settlement_ccy, funding_ccy, "
        "fee_rule_set, dividend_model) VALUES "
        "('schwab','Schwab','Schwab','USD','TWD','schwab_us','drip_us')"
    )
    for sym in ("OLD", "NEW", "SOLD"):
        upsert_instrument(c, Instrument(symbol=sym, market=Market.US,
                                        quote_ccy=Currency.USD, sector="Tech", name=sym))
    insert_transaction(c, account_id="schwab", symbol="OLD", side=Side.BUY,
                       quantity=D("100"), price=D("50"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 1, 10))
    insert_corporate_action(c, account_id="schwab", action_date=EXCHANGE_DAY,
                            kind=CorporateActionKind.EXCHANGE, from_symbol="OLD",
                            to_symbol="NEW", ratio_to=D("1"), ratio_from=D("1"))
    c.commit()
    return c


def _pay(conn: sqlite3.Connection, div_type: str, **over: object) -> None:
    kw: dict[str, object] = {
        "account_id": "schwab", "symbol": "OLD", "div_date": PAY_DAY,
        "div_type": div_type, "gross": D("100"), "withholding": D("30"), "net": D("70"),
    }
    kw.update(over)
    insert_dividend(conn, **kw)  # type: ignore[arg-type]
    conn.commit()


# --- the two branches the row exists for ------------------------------------------------


def test_a_cash_dividend_on_a_vacated_symbol_is_not_booked_as_income(
    conn: sqlite3.Connection,
) -> None:
    """Pre-fix this appended a ``RealizedRow`` of 70 on a ticker that no longer exists."""
    _pay(conn, "NET", withholding=D("0"), net=D("70"))
    book = build_book(load_ledger_bundle(conn), allow_oversell=True)
    assert [r.symbol for r in book.realized.rows] == []
    assert book.realized.by_currency == {}


def test_a_drip_on_a_vacated_symbol_does_not_resurrect_it(
    conn: sqlite3.Connection,
) -> None:
    """The portfolio-wide one: an `avg = 0` holding on a delisted ticker never gets a price,
    and one unpriced holding blanks the WHOLE portfolio's XIRR (`returns.py` is
    all-or-nothing on the terminal value)."""
    _pay(conn, "DRIP", reinvest_shares=D("2"), reinvest_price=D("35"))
    book = build_book(load_ledger_bundle(conn), allow_oversell=True)
    assert "OLD" not in {h.symbol for h in book.holdings}      # pre-fix: 2 shares at avg 0
    new = next(h for h in book.holdings if h.symbol == "NEW")
    assert new.shares == D("100")                              # the successor is untouched


def test_the_successor_carries_the_flag(conn: sqlite3.Connection) -> None:
    """Refusing silently would be its own defect.

    The vacated source has zero shares, so the holdings loop DROPS it — a flag set there is
    discarded with its carrier (audit F-47, case 2). The successor is the position the owner
    actually holds and the only one they will look at, so the 待釐清 marker goes there.
    """
    _pay(conn, "NET", withholding=D("0"), net=D("70"))
    book = build_book(load_ledger_bundle(conn), allow_oversell=True)
    assert next(h for h in book.holdings if h.symbol == "NEW").unbookable_dividend is True


def test_the_strict_path_raises_and_names_the_symbol(conn: sqlite3.Connection) -> None:
    _pay(conn, "NET", withholding=D("0"), net=D("70"))
    with pytest.raises(UnbookableLedgerError, match="OLD"):
        build_book(load_ledger_bundle(conn), allow_oversell=False)


def test_it_follows_a_chain(conn: sqlite3.Connection) -> None:
    """A de-SPAC into a ticker later renamed is the real-ledger case (§6.2's own example)."""
    upsert_instrument(conn, Instrument(symbol="NEWER", market=Market.US,
                                       quote_ccy=Currency.USD, sector="Tech", name="NEWER"))
    insert_corporate_action(conn, account_id="schwab", action_date=date(2026, 5, 10),
                            kind=CorporateActionKind.EXCHANGE, from_symbol="NEW",
                            to_symbol="NEWER", ratio_to=D("1"), ratio_from=D("1"))
    _pay(conn, "NET", withholding=D("0"), net=D("70"))
    book = build_book(load_ledger_bundle(conn), allow_oversell=True)
    assert [h.symbol for h in book.holdings] == ["NEWER"]
    assert book.holdings[0].unbookable_dividend is True


# --- the narrow scope: everything else is unchanged --------------------------------------


def test_an_ordinary_sold_out_position_keeps_the_h2_behaviour(
    conn: sqlite3.Connection,
) -> None:
    """D32 is narrow ON PURPOSE. The 2026-07-26 audit ruled (H2) that a cash dividend landing
    after an ordinary close is booked as realized INCOME — TW/MY pay weeks after the ex-date,
    so being entitled on the ex-date and flat by payment is ordinary, not exotic. That ruling
    must survive this one: a guard keyed on ``shares == 0`` instead of on *how* the position
    reached zero would silently revert it.
    """
    insert_transaction(conn, account_id="schwab", symbol="SOLD", side=Side.BUY,
                       quantity=D("10"), price=D("20"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 1, 5))
    insert_transaction(conn, account_id="schwab", symbol="SOLD", side=Side.SELL,
                       quantity=D("10"), price=D("25"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 4, 1))
    _pay(conn, "NET", symbol="SOLD", withholding=D("0"), net=D("40"))
    book = build_book(load_ledger_bundle(conn), allow_oversell=True)
    income = [r for r in book.realized.rows if r.symbol == "SOLD" and r.realized == D("40")]
    assert len(income) == 1, "H2's post-close income ruling must survive E24"


def test_a_dividend_BEFORE_the_exchange_books_normally(conn: sqlite3.Connection) -> None:
    """The guard is about the event's position in the replay, not about the symbol."""
    _pay(conn, "NET", div_date=date(2026, 2, 1), withholding=D("0"), net=D("70"))
    book = build_book(load_ledger_bundle(conn), allow_oversell=True)
    new = next(h for h in book.holdings if h.symbol == "NEW")
    # The cash reduced OLD's adjusted basis, and the EXCHANGE carried that reduction over.
    assert new.adjusted_cost_total == D("5000") - D("70")
    assert new.unbookable_dividend is False


def test_a_ledger_with_no_action_is_untouched(conn: sqlite3.Connection) -> None:
    """Containment (D38 invariant 1): the new field must be invisible without an EXCHANGE."""
    insert_transaction(conn, account_id="schwab", symbol="SOLD", side=Side.BUY,
                       quantity=D("10"), price=D("20"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 1, 5))
    _pay(conn, "NET", symbol="SOLD", div_date=date(2026, 3, 1),
         withholding=D("0"), net=D("40"))
    book = build_book(load_ledger_bundle(conn), allow_oversell=True)
    sold = next(h for h in book.holdings if h.symbol == "SOLD")
    assert sold.unbookable_dividend is False
    assert sold.adjusted_cost_total == D("200") - D("40")
