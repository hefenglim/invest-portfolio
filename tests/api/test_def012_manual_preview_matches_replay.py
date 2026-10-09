"""DEF-012 (a): the manual sell preview and the replay that follows agree on ONE day.

B-18's shape, end to end: 2884 held 100 @ 9,320; on one day the owner enters buy 200@44,
previews and writes sell 100@45, then enters buy 100@46. The preview's ``realized_pnl``
and ``cost_removed`` must be the figures the ledger books for that sell AFTER the third row
lands — because the preview appends its draft LAST (the id it will take) and the replay now
books a day's trades in id order. Under the retired ``BUY < SELL`` rule the third buy was
hoisted ahead of the sell and the booked figures moved (−1,580 → −1,223).
"""

import sqlite3
from datetime import date
from decimal import Decimal

from portfolio_dash.api.routers.input_center import ManualBody, _position_preview
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
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

D = Decimal
DAY = date(2026, 7, 2)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    bootstrap_db(conn)
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="2884", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Financials", name="玉山金"))
    upsert_opening(conn, account_id="tw_broker", symbol="2884", shares=D("100"),
                   original_cost_total=D("9320"), build_date=date(2026, 6, 1))
    return conn


def _tx(conn: sqlite3.Connection, side: Side, qty: str, price: str, *,
        fee: str = "20", tax: str = "0") -> None:
    insert_transaction(conn, account_id="tw_broker", symbol="2884", side=side,
                       quantity=D(qty), price=D(price), fees=D(fee), tax=D(tax), trade_date=DAY)


def test_the_previewed_realized_is_the_booked_realized_after_a_later_same_day_buy() -> None:
    conn = _conn()
    _tx(conn, Side.BUY, "200", "44")                                   # ①
    body = ManualBody(account_id="tw_broker", symbol="2884", side="SELL", date=DAY,
                      shares=D("100"), price=D("45"))
    preview = _position_preview(conn, body, D("20"), D("13"), D("4500"))  # ② previewed
    assert preview is not None and preview["realized_pnl"] is not None
    _tx(conn, Side.SELL, "100", "45", tax="13")                        # ② written
    _tx(conn, Side.BUY, "100", "46")                                   # ③ written later
    book = build_book(load_ledger_bundle(conn))
    (row,) = book.realized.rows
    assert D(str(preview["realized_pnl"])) == row.realized
    assert D(str(preview["cost_removed"])) == row.adjusted_cost_removed
    # And they are the B-18 figures the owner read on the draft, not the restated ones.
    assert row.realized.quantize(D("0.01")) == D("-1579.67")
    assert row.adjusted_cost_removed.quantize(D("0.01")) == D("6046.67")
    conn.close()


def test_a_drafted_buy_is_projected_after_the_days_sells_not_before_them() -> None:
    """The other half of B-18: when ③ is DRAFTED, its projected position must be built on
    the ledger AFTER ② — 200 shares at 12,093.33 — so ``new_shares`` is 300 and the new
    cost 16,713.33, not ②-undone 400 → 300 at 17,070."""
    conn = _conn()
    _tx(conn, Side.BUY, "200", "44")
    _tx(conn, Side.SELL, "100", "45", tax="13")
    body = ManualBody(account_id="tw_broker", symbol="2884", side="BUY", date=DAY,
                      shares=D("100"), price=D("46"))
    preview = _position_preview(conn, body, D("20"), D("0"), D("4600"))
    assert preview is not None
    assert preview["old_shares"] == "200"
    assert preview["new_shares"] == "300"
    assert D(str(preview["new_original_avg"])).quantize(D("0.0001")) == \
        (D("16713.33") / 300).quantize(D("0.0001"))
    conn.close()
