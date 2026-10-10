"""Options W2 — the correction doors refuse a change that strands a close row (spec §4.4).

``api/replay_guard.py``: a correction whose would-be ledger has a refused close the current one
does not — compared per ROW (``RefusedClose.txn_id``) — answers 422
``refused_close_correction``. HARD: ``ack_oversell`` does not pass it (a close left behind is a
phantom lot, not a 賣超 the owner can acknowledge). The 422 lists every stranded close row —
``txn_id``, date, contract, reason — so the delete dialog can render the list. A refused close
that was already there never blocks an unrelated correction (audit H3 / H8's scoping).
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.register import register_option_contract
from portfolio_dash.data_ingestion.store import (
    insert_corporate_action,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import DashboardClientFactory

D = Decimal
PUT = "XYZ260515P00050000"        # XYZ 2026-05-15 50 Put
SRC = "XYZ260515P00060000"
DST = "XYZ260515P00020000"
_IDS: dict[str, int] = {}


def _trade(conn: sqlite3.Connection, name: str, symbol: str, side: Side, qty: str, day: date,
           *, short: bool = False, close: str | None = None, price: str = "2") -> None:
    _IDS[name] = insert_transaction(
        conn, account_id="schwab", symbol=symbol, side=side, quantity=D(qty),
        price=D("0") if close else D(price), fees=D("0"), tax=D("0"), trade_date=day,
        short_sale=short, close_reason=close)  # type: ignore[arg-type]


def _seed(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US,
                                       quote_ccy=Currency.USD, sector="Tech", name="Apple"))
    register_option_contract(conn, PUT)
    _trade(conn, "aapl", "AAPL", Side.BUY, "10", date(2026, 2, 2), price="100")
    _trade(conn, "sto", PUT, Side.SELL, "200", date(2026, 3, 3), short=True)
    _trade(conn, "expired", PUT, Side.BUY, "200", date(2026, 5, 15), close="expired")


def _error(resp: Any) -> dict[str, Any]:
    body: dict[str, Any] = resp.json()["error"]
    return body


def test_deleting_the_opening_trade_strands_the_close_row(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed)
    for ack in ("false", "true"):                 # hard: an oversell ack does not pass it
        resp = client.delete(f"/api/ledgers/transactions/{_IDS['sto']}?ack_oversell={ack}")
        assert resp.status_code == 422, resp.text
        err = _error(resp)
        assert err["code"] == "refused_close_correction"
        assert "XYZ 2026-05-15 50 Put" in err["message"]
        [issue] = err["issues"]
        assert issue["txn_id"] == _IDS["expired"]
        assert issue["date"] == "2026-05-15"
        assert issue["contract"] == "XYZ 2026-05-15 50 Put"
        assert issue["symbol"] == PUT and issue["account_id"] == "schwab"
        assert "到期作廢" in issue["reason"]
    rows = client.get("/api/ledgers/transactions").json()["rows"]
    assert {r["id"] for r in rows} == set(_IDS.values())          # nothing was deleted


def test_deleting_the_close_row_itself_is_allowed(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed)
    assert client.delete(f"/api/ledgers/transactions/{_IDS['expired']}").status_code == 200


def test_an_already_refused_close_never_blocks_an_unrelated_correction(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    def seed(conn: sqlite3.Connection) -> None:
        _seed(conn)
        other = "XYZ260515C00060000"
        register_option_contract(conn, other)
        _trade(conn, "stray", other, Side.SELL, "100", date(2026, 5, 15), close="expired")

    client = dashboard_client_factory(seed)
    assert client.delete(f"/api/ledgers/transactions/{_IDS['aapl']}").status_code == 200


def test_deleting_an_adjustment_strands_the_destination_close(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    def seed(conn: sqlite3.Connection) -> None:
        seed_accounts(conn)
        register_option_contract(conn, SRC)
        register_option_contract(conn, DST)
        _trade(conn, "sto", SRC, Side.SELL, "100", date(2026, 3, 3), short=True)
        _IDS["adjust"] = insert_corporate_action(
            conn, account_id="schwab", action_date=date(2026, 4, 1),
            kind=CorporateActionKind.OPTION_ADJUST, from_symbol=SRC, to_symbol=DST,
            ratio_to=D(3), ratio_from=D(1))
        _trade(conn, "expired", DST, Side.BUY, "300", date(2026, 5, 15), close="expired")

    client = dashboard_client_factory(seed)
    resp = client.delete(f"/api/ledgers/corporate-actions/{_IDS['adjust']}?ack_oversell=true")
    assert resp.status_code == 422, resp.text
    err = _error(resp)
    assert err["code"] == "refused_close_correction"
    assert [i["txn_id"] for i in err["issues"]] == [_IDS["expired"]]
