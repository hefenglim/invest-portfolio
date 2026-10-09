"""S2 (round-3 review, 2026-10-04): a tab still running PRE-R1 JavaScript cannot bypass the
CR-07 binding.

R1 renamed the acknowledgement list ``acked_warnings`` ((code, text) pairs) to ``acked_keys``
(the server's opaque per-issue key). ``ActionBody`` ignores unknown keys (Pydantic's default),
so a long-open tab — asset URLs keep ``?v=0.1.28`` until a version bump, and an open tab never
reloads anyway — posting ``{ack_warnings: true, acked_warnings: [...stale...]}`` lost the list
on the way in, fell to the bare-flag path, and acknowledged warnings it had never displayed.

The legacy key is now refused with ``client_outdated`` and a zh sentence asking for a reload —
a code of its own rather than ``warnings_changed``, because both pre-R1 clients TOAST an
unrecognised code's message (the round-1 form toasts every error; the pre-R1 edit dialog's
``replayAck`` falls through to ``mutationFail``), while the pre-R1 edit dialog answered
``warnings_changed`` by reopening its warnings list without the message — a loop with no way
out. The body stays permissive otherwise (no ``extra='forbid'``).
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


def _seed(conn: sqlite3.Connection, *, band: bool = True) -> None:
    seed_accounts(conn)
    upsert_instrument(
        conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                         sector="Semiconductors", name="TSMC", board="TWSE",
                         target_low=D("600") if band else None,
                         target_high=D("900") if band else None),
        today=date(2026, 2, 1))
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=D("1000"), price=D("500"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 1, 5))
    upsert_prices(conn, [PriceRow(instrument="2330", market=Market.TW,
                                  as_of=date(2026, 6, 9), close=D("600"), source="test")],
                  fetched_at=GOLDEN_NOW)


def _split(ratio_to: str) -> dict[str, Any]:
    return {"account_id": "tw_broker", "date": "2026-03-16", "kind": "SPLIT",
            "from_symbol": "2330", "to_symbol": "2330", "ratio_to": ratio_to, "ratio_from": "1"}


#: What the round-1 form / the pre-R1 edit dialog sent: (code, text) pairs — here, stale ones.
_STALE = [{"code": "target_band_predates_split", "text": "（某個舊的警告句子）"}]


def _rows(client: TestClient) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = client.get("/api/ledgers/corporate-actions").json()["rows"]
    return rows


def test_the_pre_r1_acknowledgement_is_refused_with_a_reload_sentence(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed)
    r = client.post("/api/ledgers/corporate-actions",
                    json={**_split("4"), "ack_warnings": True, "acked_warnings": _STALE})
    assert r.status_code == 422, r.json()       # pre-fix: 201, the stale ack accepted
    err = r.json()["error"]
    assert err["code"] == "client_outdated"
    assert "重新整理" in err["message"]
    assert _rows(client) == []                   # nothing written


def test_the_edit_door_refuses_it_too(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed)
    made = client.post("/api/ledgers/corporate-actions",
                       json={**_split("2"), "ack_warnings": True})
    assert made.status_code == 201, made.json()
    action_id = made.json()["ids"][0]
    r = client.put(f"/api/ledgers/corporate-actions/{action_id}",
                   json={**_split("4"), "ack_warnings": True, "acked_warnings": _STALE})
    assert r.status_code == 422 and r.json()["error"]["code"] == "client_outdated", r.json()
    assert [x["ratio_to"] for x in _rows(client)] == ["2"]


def test_nothing_to_acknowledge_nothing_refused(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """Scope: with no warning on the save there is nothing a stale list could acknowledge, so
    the save goes through — the refusal guards the binding, not the tab."""
    client = dashboard_client_factory(lambda c: _seed(c, band=False))
    r = client.post("/api/ledgers/corporate-actions",
                    json={**_split("4"), "ack_warnings": True, "acked_warnings": _STALE})
    assert r.status_code == 201, r.json()


def test_the_current_wire_and_the_bare_flag_are_unchanged(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed)
    preview = client.post("/api/ledgers/corporate-actions/preview", json=_split("4")).json()
    keys = [i["ack_key"] for i in preview["issues"] if i["sev"] != "error"]
    ok = client.post("/api/ledgers/corporate-actions",
                     json={**_split("4"), "ack_warnings": True, "acked_keys": keys})
    assert ok.status_code == 201, ok.json()
    client2 = dashboard_client_factory(_seed)
    bare = client2.post("/api/ledgers/corporate-actions",
                        json={**_split("4"), "ack_warnings": True})
    assert bare.status_code == 201, bare.json()
