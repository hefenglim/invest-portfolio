"""CR-07 (pre-merge review, 2026-10-04): a corporate-action acknowledgement is bound to the
warnings it acknowledged.

The form (``web/corp-action-form.js``) kept its 「我已確認上述警告」 tick across every later edit
of symbol, ratio, date or cost carry, re-rendered the next preview pre-ticked and saved with
``ack_warnings=true`` — for warnings the owner never read. The browser half is fixed in the
form (and pinned by ``tests/e2e/test_cr07_corporate_action_ack_flow.py``); this is the half a
browser fix cannot give: the form now sends the ``(code, text)`` pairs it showed
(``acked_warnings``), and a save whose CURRENT warnings — recomputed from the body actually
posted — include one the owner did not acknowledge is refused with ``warnings_changed``.

The warning used is D44's (a target band set before the split): its sentence carries the
ratio, so a 1→2 split and a 1→4 split are two different warnings.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW, DashboardClientFactory

D = Decimal


def _seed(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(
        conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                         sector="Semiconductors", name="TSMC", board="TWSE",
                         target_low=D("600"), target_high=D("900")),
        today=date(2026, 2, 1))
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=D("1000"), price=D("500"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 1, 5))
    upsert_prices(conn, [PriceRow(instrument="2330", market=Market.TW,
                                  as_of=date(2026, 6, 9), close=D("600"), source="test")],
                  fetched_at=GOLDEN_NOW)


def _split(ratio_to: str, **over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"account_id": "tw_broker", "date": "2026-03-16", "kind": "SPLIT",
                            "from_symbol": "2330", "to_symbol": "2330",
                            "ratio_to": ratio_to, "ratio_from": "1"}
    body.update(over)
    return body


def _shown(client: TestClient, body: dict[str, Any]) -> list[dict[str, str]]:
    """What the form rendered for *body* and would send as ``acked_warnings``."""
    p = client.post("/api/ledgers/corporate-actions/preview", json=body)
    assert p.status_code == 200, p.json()
    assert p.json()["needs_confirm"] is True and p.json()["blocking"] is False
    return [{"code": i["code"], "text": i["text"]} for i in p.json()["issues"]
            if i["sev"] != "error"]


def test_an_ack_for_other_warnings_is_refused_and_the_current_ones_are_named(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed)
    seen_for_2 = _shown(client, _split("2"))
    seen_for_4 = _shown(client, _split("4"))
    assert seen_for_2 != seen_for_4                      # the sentence carries the ratio

    stale = client.post("/api/ledgers/corporate-actions", json={
        **_split("4"), "ack_warnings": True, "acked_warnings": seen_for_2})
    assert stale.status_code == 422, stale.json()
    err = stale.json()["error"]
    assert err["code"] == "warnings_changed"
    assert [(i["code"], i["text"]) for i in err["issues"]] == [
        (w["code"], w["text"]) for w in seen_for_4]
    rows = client.get("/api/ledgers/corporate-actions").json()["rows"]
    assert rows == []                                    # nothing written

    ok = client.post("/api/ledgers/corporate-actions", json={
        **_split("4"), "ack_warnings": True, "acked_warnings": seen_for_4})
    assert ok.status_code == 201, ok.json()


def test_an_empty_ack_list_acknowledges_nothing(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed)
    r = client.post("/api/ledgers/corporate-actions", json={
        **_split("2"), "ack_warnings": True, "acked_warnings": []})
    assert r.status_code == 422 and r.json()["error"]["code"] == "warnings_changed"


def test_the_bare_flag_keeps_its_meaning_for_callers_without_a_preview(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """Scripts, the stress audit and every existing API caller post ``ack_warnings`` alone;
    their contract is unchanged, and the unacknowledged 422 is unchanged too."""
    client = dashboard_client_factory(_seed)
    bare = client.post("/api/ledgers/corporate-actions", json=_split("2"))
    assert bare.status_code == 422
    assert bare.json()["error"]["code"] == "warnings_unacknowledged"
    r = client.post("/api/ledgers/corporate-actions",
                    json={**_split("2"), "ack_warnings": True})
    assert r.status_code == 201, r.json()


def test_the_edit_door_binds_the_same_way(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed)
    seen_for_2 = _shown(client, _split("2"))
    made = client.post("/api/ledgers/corporate-actions", json={
        **_split("2"), "ack_warnings": True, "acked_warnings": seen_for_2})
    assert made.status_code == 201, made.json()
    action_id = made.json()["ids"][0]
    stale = client.put(f"/api/ledgers/corporate-actions/{action_id}", json={
        **_split("4"), "ack_warnings": True, "acked_warnings": seen_for_2})
    assert stale.status_code == 422, stale.json()
    assert stale.json()["error"]["code"] == "warnings_changed"
    rows = client.get("/api/ledgers/corporate-actions").json()["rows"]
    assert rows[0]["ratio_to"] == "2"                    # nothing written
