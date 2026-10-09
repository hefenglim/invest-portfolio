"""CR-03 (pre-merge review, 2026-10-04): a correction door must replay the would-be ledger in
EXACTLY the order the stored ledger will have after the write.

``edit_transaction`` built the would-be list by replacing the edited row IN PLACE, so it kept
its OLD list position — while ``build_book`` orders a day's trades by that list index and the
stored ledger lists them by id (DEF-012). Measured on the reviewer's probe: SELL 500 (id 1,
03-10) and BUY 1,000 (id 2, entered later, back-dated 03-01). Moving the buy's date to 03-10
answered 200 and asked nothing; after the write the sell (lower id) books first, the position
goes sticky 賣超, and 500 shares carry a 500,000 basis with no realized row.

The fix is in ``replay_guard._to_models`` — every would-be list goes back into the store's own
order (``store.*_order_key``, the Python twin of each ``list_*`` ORDER BY) — so it covers every
door that rewrites rows: transaction / dividend / opening / corporate-action edits and deletes
and the import-batch undo.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import date
from decimal import Decimal

from portfolio_dash.api.replay_guard import _to_models
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    corporate_action_order_key,
    dividend_order_key,
    insert_corporate_action,
    insert_dividend,
    insert_transaction,
    list_corporate_actions,
    list_dividends,
    list_opening,
    list_transactions,
    load_ledger_bundle,
    opening_order_key,
    transaction_order_key,
    upsert_instrument,
    upsert_opening,
)
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import DashboardClientFactory, init_golden_base


def _base(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Semiconductors", name="TSMC", board="TWSE"))


def test_moving_a_buy_onto_a_day_where_an_earlier_entered_sell_waits_asks_first(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    ids: dict[str, int] = {}

    def seed(conn: sqlite3.Connection) -> None:
        _base(conn)
        ids["sell"] = insert_transaction(
            conn, account_id="tw_broker", symbol="2330", side=Side.SELL,
            quantity=Decimal("500"), price=Decimal("600"), fees=Decimal("0"),
            tax=Decimal("0"), trade_date=date(2026, 3, 10))
        ids["buy"] = insert_transaction(
            conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
            quantity=Decimal("1000"), price=Decimal("500"), fees=Decimal("0"),
            tax=Decimal("0"), trade_date=date(2026, 3, 1))
        holder.append(conn)

    holder: list[sqlite3.Connection] = []
    client = dashboard_client_factory(seed)
    body = {"account_id": "tw_broker", "symbol": "2330", "side": "BUY", "date": "2026-03-10",
            "shares": "1000", "price": "500", "fee": "0", "tax": "0",
            "fee_overridden": True, "tax_overridden": True}
    r = client.put(f"/api/ledgers/transactions/{ids['buy']}", json=body)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "oversell"
    assert "2026-03-10 的 2330 賣出 500 股，超過當日持股 0 股" in err["message"], err
    conn = holder[0]
    assert next(t for t in list_transactions(conn) if t.id == ids["buy"]).trade_date == date(
        2026, 3, 1)                                             # refused = nothing written

    ok = client.put(f"/api/ledgers/transactions/{ids['buy']}",
                    json={**body, "ack_oversell": True})
    assert ok.status_code == 200, ok.text
    # …and what was acknowledged is what the ledger now holds: the sell books first.
    book = build_book(load_ledger_bundle(conn), allow_oversell=True)
    (held,) = book.holdings
    assert held.oversold is True and held.oversold_on == date(2026, 3, 10)


def test_moving_the_buy_where_it_still_books_first_does_not_ask(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The control: the buy has the LOWER id here, so on a shared day it still books first."""
    ids: dict[str, int] = {}

    def seed(conn: sqlite3.Connection) -> None:
        _base(conn)
        ids["buy"] = insert_transaction(
            conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
            quantity=Decimal("1000"), price=Decimal("500"), fees=Decimal("0"),
            tax=Decimal("0"), trade_date=date(2026, 3, 1))
        insert_transaction(
            conn, account_id="tw_broker", symbol="2330", side=Side.SELL,
            quantity=Decimal("500"), price=Decimal("600"), fees=Decimal("0"),
            tax=Decimal("0"), trade_date=date(2026, 3, 10))

    client = dashboard_client_factory(seed)
    r = client.put(f"/api/ledgers/transactions/{ids['buy']}", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "BUY", "date": "2026-03-10",
        "shares": "1000", "price": "500", "fee": "0", "tax": "0",
        "fee_overridden": True, "tax_overridden": True})
    assert r.status_code == 200, r.text


def test_the_would_be_bundle_is_in_store_order_whatever_order_it_was_handed() -> None:
    """``_to_models`` re-sorts every would-be list it is handed, so no door can replay a
    different order than the one ``load_ledger_bundle`` reads back after the write."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_golden_base(conn)
    _base(conn)
    for day, side, qty in ((date(2026, 3, 10), Side.SELL, "500"),
                           (date(2026, 3, 1), Side.BUY, "1000"),
                           (date(2026, 3, 10), Side.BUY, "10")):
        insert_transaction(conn, account_id="tw_broker", symbol="2330", side=side,
                           quantity=Decimal(qty), price=Decimal("500"), fees=Decimal("0"),
                           tax=Decimal("0"), trade_date=day)
    stored = list_transactions(conn)
    shuffled = list(reversed(stored))
    assert _to_models(conn, txs=shuffled).transactions == load_ledger_bundle(conn).transactions
    conn.close()


def test_each_order_key_is_the_twin_of_its_list_order_by() -> None:
    """The four keys ``_to_models`` sorts with must agree with the SQL that lists the rows —
    rows inserted out of date order, so the id and the date disagree."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_golden_base(conn)
    _base(conn)
    upsert_instrument(conn, Instrument(symbol="2454", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Semiconductors", name="MediaTek", board="TWSE"))
    rng = random.Random(20261004)
    days = [date(2026, 3, d) for d in (9, 1, 9, 5, 1, 9)]
    for day in days:
        insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                           quantity=Decimal("1"), price=Decimal("1"), fees=Decimal("0"),
                           tax=Decimal("0"), trade_date=day)
        insert_dividend(conn, account_id="tw_broker", symbol="2330", div_date=day,
                        div_type="CASH", gross=Decimal("1"), withholding=Decimal("0"),
                        net=Decimal("1"))
    for acct, sym in (("tw_broker", "2454"), ("tw_broker", "2330")):
        upsert_opening(conn, account_id=acct, symbol=sym, shares=Decimal("1"),
                       original_cost_total=Decimal("1"), build_date=date(2026, 1, 2))
    for day in (date(2026, 4, 9), date(2026, 4, 1)):
        insert_corporate_action(
            conn, account_id="tw_broker", action_date=day, kind=CorporateActionKind.SPLIT,
            from_symbol="2330", to_symbol="2330", ratio_to=Decimal("2"),
            ratio_from=Decimal("1"))
    for rows, key in ((list_transactions(conn), transaction_order_key),
                      (list_dividends(conn), dividend_order_key),
                      (list_opening(conn), opening_order_key),
                      (list_corporate_actions(conn), corporate_action_order_key)):
        jumbled = list(rows)
        rng.shuffle(jumbled)
        assert sorted(jumbled, key=key) == rows      # type: ignore[arg-type]
    conn.close()
