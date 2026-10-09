"""DEF-023 (D-11): a refused corporate action is LINKABLE — it carries its row id.

The dashboard's only trace of an unapplied action was a sentence inside the XIRR badge's
tooltip: account, symbol, date, and why. A sentence is an explanation, not a way in; the
owner has to find the row in the ledger by hand. ``UnappliedAction.action_id`` is the
``corporate_actions.id`` the refusal came from (``None`` only for an action that was being
validated and never stored), threaded from the store through ``convert_stored`` →
``CorporateAction.id`` → ``build_book``'s refusal record, and out on ``/api/dashboard``.
"""

import sqlite3
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_corporate_action,
    insert_transaction,
    load_ledger_bundle,
    upsert_instrument,
)
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.shared.corporate_actions import CorporateAction, CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.models.ledger import LedgerBundle, Transaction
from tests.conftest import DashboardClientFactory

D = Decimal
_AAA = Instrument(symbol="AAA", market=Market.US, quote_ccy=Currency.USD, sector="Tech",
                  name="A")


def _seed(conn: sqlite3.Connection) -> int:
    """An oversold source (E3): sell before any buy, then a SPLIT the replay must refuse."""
    seed_accounts(conn)
    upsert_instrument(conn, _AAA)
    insert_transaction(conn, account_id="schwab", symbol="AAA", side=Side.SELL,
                       quantity=D("100"), price=D("40"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 1, 5))
    insert_transaction(conn, account_id="schwab", symbol="AAA", side=Side.BUY,
                       quantity=D("300"), price=D("40"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 2, 5))
    action_id: int = insert_corporate_action(
        conn, account_id="schwab", action_date=date(2026, 3, 15),
        kind=CorporateActionKind.SPLIT, from_symbol="AAA", to_symbol="AAA",
        ratio_to=D("2"), ratio_from=D("1"))
    return action_id


def test_the_refusal_record_carries_the_stored_row_id() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    bootstrap_db(conn)
    action_id = _seed(conn)
    bundle = load_ledger_bundle(conn)
    assert bundle.actions[0].id == action_id            # threaded through convert_stored
    book = build_book(bundle, allow_oversell=True)
    (unapplied,) = book.unapplied_actions
    assert unapplied.action_id == action_id
    assert "{account:schwab}" in unapplied.reason        # the token, not 「schwab」
    conn.close()


def test_an_action_that_was_never_stored_has_no_id() -> None:
    """The validation path builds ``CorporateAction`` by hand; its refusal is not linkable
    to a row because there is no row yet."""
    sell = Transaction(account_id="schwab", symbol="AAA", side=Side.SELL, quantity=D("10"),
                       price=D("1"), fees=D("0"), tax=D("0"), trade_date=date(2026, 1, 5))
    action = CorporateAction(account_id="schwab", date=date(2026, 3, 15),
                             kind=CorporateActionKind.SPLIT, from_symbol="AAA",
                             to_symbol="AAA", ratio_to=D("2"), ratio_from=D("1"))
    book = build_book(LedgerBundle([sell], actions=[action], instruments={"AAA": _AAA}),
                      allow_oversell=True)
    assert book.unapplied_actions[0].action_id is None


def test_the_dashboard_wire_carries_the_id_and_the_account_token(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    ids: list[int] = []
    client: TestClient = dashboard_client_factory(lambda c: ids.append(_seed(c)))
    body = client.get("/api/dashboard").json()
    (row,) = body["unapplied_actions"]
    assert row["action_id"] == ids[0]
    assert row["account_id"] == "schwab" and row["from_symbol"] == "AAA"
    assert row["kind"] == "SPLIT" and row["date"] == "2026-03-15"
    assert "{account:schwab}" in row["reason"]
    # The XIRR badge's reason names the same row through the same token — the backend
    # never resolves it; web/api.js does, on every response.
    reason = " ".join(str(v) for v in body["kpis"].values() if isinstance(v, str)) \
        if isinstance(body.get("kpis"), dict) else str(body)
    assert "{account:schwab}" in reason or "{account:schwab}" in str(body)
