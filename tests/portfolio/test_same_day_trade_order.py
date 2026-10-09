"""DEF-012 (functional test B-18, 2026-09-22): a day's trades replay in WRITE order.

**Measured defect.** 台灣券商 2884 held 100 股 at 9,320. Entered on ONE day, in this order:
① buy 200@44 → ② sell 100@45 (the draft previewed realized −1,580, cost removed 6,047)
→ ③ buy 100@46. Once ③ landed the ledger showed realized **−1,223** / cost removed
**5,690** for ②, and the position's cost at **17,070** where the entry order gives
**16,713.33**. Root cause: ``EventPriority.BUY = 20 < SELL = 30`` — ``build_book`` sorted
the day's events by ``(date, priority)``, so ③ was replayed BEFORE ② regardless of when it
was entered, and the average ② sold at was a different number from the one it was
previewed at.

A transaction has no time-of-day column. The ONLY evidence of intraday order is the row
id — the order the owner entered the rows — so that is the rule now
(``shared/ledger_events.py``: one ``TRADE`` priority, tie-break on position in the
id-ordered list). OPENING and CORPORATE_ACTION still precede the day's trades, DIVIDEND
still follows them; the assertions below pin all of it.
"""

import sqlite3
from datetime import date
from decimal import Decimal

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_transaction,
    load_ledger_bundle,
    upsert_instrument,
    upsert_opening,
)
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.ledger_events import EventPriority
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import (
    Dividend,
    LedgerBundle,
    OpeningInventory,
    Transaction,
)

D = Decimal
DAY = date(2026, 7, 2)
_2884 = Instrument(symbol="2884", market=Market.TW, quote_ccy=Currency.TWD,
                   sector="Financials", name="玉山金")
INSTR = {"2884": _2884}


def _tx(side: Side, qty: str, price: str, *, fees: str = "20", tax: str = "0",
        d: date = DAY) -> Transaction:
    return Transaction(account_id="tw_broker", symbol="2884", side=side, quantity=D(qty),
                       price=D(price), fees=D(fees), tax=D(tax), trade_date=d)


#: The B-18 ledger, exactly as entered. Fees are the TW engine's: 200×44 = 8,800 ×
#: 0.1425% = 12.54 → floor 12 → min 20; sell 100×45 = 4,500 → fee 20, tax floor(13.5) = 13.
_OPENING = OpeningInventory(account_id="tw_broker", symbol="2884", shares=D("100"),
                            original_cost_total=D("9320"), build_date=date(2026, 6, 1))
_B18 = [
    _tx(Side.BUY, "200", "44"),                  # ①
    _tx(Side.SELL, "100", "45", tax="13"),        # ②
    _tx(Side.BUY, "100", "46"),                   # ③
]


def _cents(value: Decimal) -> Decimal:
    return value.quantize(D("0.01"))


def test_b18_the_sell_realizes_at_the_average_of_what_was_held_when_it_was_entered() -> None:
    """② sells out of (9,320 + 8,820) / 300 = 60.4667 — the average the preview showed —
    and ③ is added AFTER it: realized −1,579.67, cost removed 6,046.67, position 16,713.33."""
    book = build_book(LedgerBundle(_B18, opening=[_OPENING], instruments=INSTR))
    (row,) = book.realized.rows
    assert _cents(row.adjusted_cost_removed) == D("6046.67")
    assert _cents(row.realized) == D("-1579.67")
    (h,) = book.holdings
    assert h.shares == D("300")
    assert _cents(h.original_cost_total) == D("16713.33")


def test_b18_the_retired_buys_first_rule_is_the_number_the_owner_saw_and_rejected() -> None:
    """The MUTATION reference: replaying ③ ahead of ② (what ``BUY < SELL`` did) gives
    exactly the figures the functional test measured — 5,690 / −1,223 / 17,070. If a
    future edit re-introduces buys-before-sells, this is the test that says which number
    came back, not merely that one changed."""
    reordered = [_B18[0], _B18[2], _B18[1]]
    book = build_book(LedgerBundle(reordered, opening=[_OPENING], instruments=INSTR))
    (row,) = book.realized.rows
    assert _cents(row.adjusted_cost_removed) == D("5690.00")
    assert _cents(row.realized) == D("-1223.00")
    assert _cents(book.holdings[0].original_cost_total) == D("17070.00")


def test_a_same_day_sell_listed_before_its_buy_is_an_oversell_not_a_covered_sale() -> None:
    """Order is the rule in BOTH directions: a sell that precedes its only cover in the
    ledger is booked first and finds nothing to sell. Under the old rule the buy was hoisted
    ahead of it and the sale looked ordinary."""
    txs = [_tx(Side.SELL, "100", "45", tax="13"), _tx(Side.BUY, "150", "44")]
    book = build_book(LedgerBundle(txs, instruments=INSTR), allow_oversell=True)
    (h,) = book.holdings
    assert h.oversold is True and book.realized.rows == []
    assert h.shares == D("50")
    # ...and the same two rows the other way round are an ordinary partial exit.
    clean = build_book(LedgerBundle(list(reversed(txs)), instruments=INSTR))
    assert clean.holdings[0].shares == D("50") and len(clean.realized.rows) == 1
    assert clean.holdings[0].oversold is False


def test_opening_and_action_still_precede_and_dividend_still_follows_the_days_trades() -> None:
    """The rest of the same-day order is unchanged: an opening dated on the trade day is
    seeded before the trades, and a CASH dividend dated on it reduces the cost the day's
    trades leave behind (DIVIDEND 40 > TRADE 20)."""
    assert EventPriority.OPENING < EventPriority.CORPORATE_ACTION < EventPriority.TRADE
    assert EventPriority.TRADE < EventPriority.DIVIDEND
    assert EventPriority["BUY"] is EventPriority["TRADE"]
    assert EventPriority["SELL"] is EventPriority["TRADE"]
    opening = OpeningInventory(account_id="tw_broker", symbol="2884", shares=D("100"),
                               original_cost_total=D("9320"), build_date=DAY)
    div = Dividend(account_id="tw_broker", symbol="2884", date=DAY, type=DividendType.CASH,
                   gross=D("300"), withholding=D("0"), net=D("300"))
    # sell 40 on the opening's own build date: legal only because OPENING books first.
    book = build_book(LedgerBundle([_tx(Side.SELL, "40", "45", tax="13")],
                                   dividends=[div], opening=[opening], instruments=INSTR))
    (row,) = book.realized.rows
    assert row.adjusted_cost_removed == D("3728")    # 9,320 × 0.4: the dividend had NOT
    (h,) = book.holdings                             # yet reduced the basis it sold from
    assert h.adjusted_cost_total == D("5292")        # (9,320 − 3,728) − 300, applied AFTER


def test_the_store_hands_the_replay_its_rows_in_id_order() -> None:
    """The third sort key is the row's position in ``bundle.transactions``; what makes that
    the WRITE order is ``store.list_transactions``'s ``ORDER BY trade_date ASC, id ASC``.
    Pinned behaviourally: a sell WRITTEN before its same-day buy replays as an oversell,
    and written after it, as a clean round trip."""
    for sell_first in (True, False):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        bootstrap_db(conn)
        seed_accounts(conn)
        upsert_instrument(conn, _2884)
        rows = [(Side.SELL, "100", "45"), (Side.BUY, "150", "44")]
        for side, qty, price in (rows if sell_first else reversed(rows)):
            insert_transaction(conn, account_id="tw_broker", symbol="2884", side=side,
                               quantity=D(qty), price=D(price), fees=D("0"), tax=D("0"),
                               trade_date=DAY)
        book = build_book(load_ledger_bundle(conn), allow_oversell=True)
        (h,) = book.holdings
        if sell_first:
            assert h.oversold is True and book.realized.rows == []
        else:
            assert h.oversold is False and len(book.realized.rows) == 1
        conn.close()


def test_the_demo_shaped_pair_buy_id_below_sell_id_is_unchanged_by_the_rule() -> None:
    """The one same-day buy+sell pair on the demo ledger (tw_broker 2603 2026-07-02, buy
    id 7 / sell id 8) already had the buy first, so its numbers are byte-identical under
    the old and the new rule — the change is not a restatement of existing demo figures."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    bootstrap_db(conn)
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="2603", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Industrials", name="長榮"))
    upsert_opening(conn, account_id="tw_broker", symbol="2603", shares=D("1000"),
                   original_cost_total=D("150000"), build_date=date(2026, 6, 1))
    insert_transaction(conn, account_id="tw_broker", symbol="2603", side=Side.BUY,
                       quantity=D("2000"), price=D("160"), fees=D("456"), tax=D("0"),
                       trade_date=DAY)
    insert_transaction(conn, account_id="tw_broker", symbol="2603", side=Side.SELL,
                       quantity=D("500"), price=D("165"), fees=D("117"), tax=D("247"),
                       trade_date=DAY)
    bundle = load_ledger_bundle(conn)
    new_rule = build_book(bundle)
    # The retired rule, reproduced by hand: buys first, then sells.
    buys_first = LedgerBundle(
        sorted(bundle.transactions, key=lambda t: 0 if t.side is Side.BUY else 1),
        opening=bundle.opening, instruments=bundle.instruments)
    old_rule = build_book(buys_first)
    assert new_rule.realized.rows[0].realized == old_rule.realized.rows[0].realized
    assert new_rule.holdings[0].original_cost_total == old_rule.holdings[0].original_cost_total
    conn.close()
