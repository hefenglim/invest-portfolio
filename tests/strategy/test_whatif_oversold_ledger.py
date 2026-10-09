"""試算 must not 500 because some *other* position in the ledger is oversold.

Found by W5 while building the drawer footer, 2026-08-11, and **older than the
corporate-actions feature** — reproducible with no action in the ledger at all.

`compute_whatif` replays strictly (`build_book` with `allow_oversell` defaulting False —
correct: a 試算 must not quote off a book whose basis was discarded) and caught only
`UnbookableLedgerError`. But `OversellError` is a **separate hierarchy** — `Exception`, not
`ValueError` — so it escaped as an unhandled 500.

Blast radius is the part that matters: the symbol drawer posts 試算 **on open**, so one
undeclared oversell anywhere made *every* symbol's drawer log a 500, including symbols in
other accounts and other markets. A never-500 violation of the same class as the one
`finding-oversell-dashboard-500` recorded — degradation has to be applied at every
`build_book` call site, and this site was reached by a second exception type nobody
re-checked when the first was handled.
"""

import sqlite3
from datetime import date, datetime
from decimal import Decimal

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.strategy.whatif import WhatIfError, compute_whatif

D = Decimal
_NOW = datetime(2026, 8, 11, 12, 0, 0)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    seed_accounts(c)          # the real account + fee-rule seed, not a hand-rolled row
    create_pricing_tables(c)  # `bootstrap_db` does not create `prices`; 試算 reads it
    upsert_instrument(c, Instrument(symbol="AAA", market=Market.US, quote_ccy=Currency.USD,
                                    sector="Tech", name="AAA"))
    upsert_instrument(c, Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                                    sector="Tech", name="TSMC"))
    # A healthy US position the owner wants to 試算…
    insert_transaction(c, account_id="schwab", symbol="AAA", side=Side.BUY, quantity=D("100"),
                       price=D("50"), fees=D("1"), tax=D("0"), trade_date=date(2026, 1, 10))
    # …and, in a DIFFERENT account and a DIFFERENT market, an undeclared oversell.
    insert_transaction(c, account_id="tw_broker", symbol="2330", side=Side.SELL,
                       quantity=D("100"), price=D("600"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 2, 1))
    c.commit()
    return c


def test_an_unrelated_oversell_degrades_instead_of_raising(conn: sqlite3.Connection) -> None:
    """`WhatIfError` is what the route maps to a 4xx; anything else is a 500."""
    with pytest.raises(WhatIfError) as caught:
        compute_whatif(conn, now=_NOW, reporting=Currency.TWD, symbol="AAA",
                       side=Side.SELL, shares=D("10"), price=D("60"),
                       account_id="schwab")
    assert "2330" in str(caught.value)      # …and it says WHICH position to go and fix


def test_a_clean_ledger_still_computes(conn: sqlite3.Connection) -> None:
    """Detection power in the other direction: the guard must not swallow the happy path."""
    conn.execute("DELETE FROM transactions WHERE symbol='2330'")
    conn.commit()
    result = compute_whatif(conn, now=_NOW, reporting=Currency.TWD, symbol="AAA",
                            side=Side.SELL, shares=D("10"), price=D("60"),
                            account_id="schwab")
    assert result["account_id"] == "schwab" and result["oversell"] is False
