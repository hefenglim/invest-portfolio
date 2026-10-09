"""DEF-012 (b): the CSV preview judges a same-day sell by the rows ABOVE it, as the
replay will.

A file's rows take ids in file order when they are written, and the replay books one
day's trades in id order (``shared/ledger_events.py``, DEF-012). So a same-day sibling
BELOW the sell is booked after it and cannot cover it — the preview must say 賣超 there,
because the replay will discard the basis there, and a preview that disagrees with what
will be written is worse than no preview (domain-ledger.md, "a preview must mirror the
replay's branches"). Before this change ``shares_through`` counted every same-day sibling
regardless of position: the sell previewed ✓ and replayed as an undeclared oversell.

The STORED ledger is the other half of the rule: every stored row precedes a pending one,
so a stored same-day buy still covers a sell entered afterwards — the manual door is
unchanged, and so is every file whose buy is listed first.
"""

import sqlite3
from datetime import date
from decimal import Decimal

from portfolio_dash.data_ingestion.csv_import import build_transaction_preview
from portfolio_dash.data_ingestion.preview import ImportPreview
from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.data_ingestion.validate import (
    TxnInput,
    siblings_booked_before,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

D = Decimal
HEADER = "account,symbol,side,date,shares,price\n"


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO accounts (account_id,name,broker,settlement_ccy,funding_ccy,"
        "fee_rule_set,dividend_model) VALUES "
        "('schwab','S','Schwab','USD','TWD','schwab','drip_us')"
    )
    upsert_instrument(conn, Instrument(symbol="AAA", market=Market.US, quote_ccy=Currency.USD,
                                       sector="Tech", name="A"))
    conn.commit()


def _kinds(preview: ImportPreview) -> list[set[str]]:
    return [{i.kind for i in r.issues} for r in preview.rows]


def test_a_same_day_sell_listed_above_its_buy_is_flagged_as_the_replay_would_book_it(
    conn: sqlite3.Connection,
) -> None:
    _seed(conn)
    csv = HEADER + "schwab,AAA,sell,2026-03-02,60,55\n" + "schwab,AAA,buy,2026-03-02,100,50\n"
    kinds = _kinds(build_transaction_preview(conn, csv))
    assert "sell_exceeds_holdings" in kinds[0], kinds
    assert kinds[1] == set()
    # The sentence names the cause — the row order — not a phantom shortfall.
    (row,) = [r for r in build_transaction_preview(conn, csv).rows if r.index == 0]
    msg = next(i.message for i in row.issues if i.kind == "sell_exceeds_holdings")
    assert "同日較後面的列" in msg and "移到賣出列之前" in msg


def test_the_same_two_rows_with_the_buy_first_preview_clean(conn: sqlite3.Connection) -> None:
    _seed(conn)
    csv = HEADER + "schwab,AAA,buy,2026-03-02,100,50\n" + "schwab,AAA,sell,2026-03-02,60,55\n"
    assert _kinds(build_transaction_preview(conn, csv)) == [set(), set()]


def test_a_buy_on_an_earlier_day_covers_regardless_of_its_position_in_the_file(
    conn: sqlite3.Connection,
) -> None:
    """Order is only a tie-break WITHIN a day: a buy dated the day before covers the sell
    even when it is listed below it."""
    _seed(conn)
    csv = HEADER + "schwab,AAA,sell,2026-03-02,60,55\n" + "schwab,AAA,buy,2026-03-01,100,50\n"
    assert _kinds(build_transaction_preview(conn, csv)) == [set(), set()]


def test_a_stored_same_day_buy_still_covers_a_sell_entered_afterwards(
    conn: sqlite3.Connection,
) -> None:
    """The manual-door shape: the covering buy is already in the ledger (lower id)."""
    _seed(conn)
    insert_transaction(conn, account_id="schwab", symbol="AAA", side=Side.BUY,
                       quantity=D("100"), price=D("50"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 3, 2))
    csv = HEADER + "schwab,AAA,sell,2026-03-02,60,55\n"
    assert _kinds(build_transaction_preview(conn, csv)) == [set()]


def test_a_later_same_day_sell_does_not_drain_an_earlier_one_either() -> None:
    """``siblings_booked_before`` keeps every row up to and including the one being
    validated, plus every row dated on another day — never a same-day row below it, on
    either side."""
    def row(side: Side, day: date) -> TxnInput:
        return TxnInput(account_id="schwab", symbol="AAA", side=side, quantity=D("1"),
                        price=D("1"), trade_date=day)
    d = date(2026, 3, 2)
    batch = [row(Side.BUY, d), row(Side.SELL, d), row(Side.SELL, d),
             row(Side.BUY, date(2026, 3, 3))]
    before = siblings_booked_before(batch, batch[1])
    assert before == [batch[0], batch[1], batch[3]]
    # A row that is not in the batch at all leaves the batch untouched (the single-row
    # doors pass an empty batch, so this is the defensive branch, not a real caller).
    assert siblings_booked_before(batch, row(Side.SELL, d)) == batch
