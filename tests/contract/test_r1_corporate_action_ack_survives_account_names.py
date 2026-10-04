"""R1 (final review of the CR-07 merge, 2026-10-04): a warning that NAMES AN ACCOUNT can be
acknowledged.

CR-07 bound the corporate-action acknowledgement to the warnings it acknowledged by comparing
the ``(code, text)`` pairs the browser sent back against the server's own. But the server's
text carries ``{account:<id>}`` tokens (``shared/account_ref.py``) and ``web/api.js``
``_resolveRefs`` rewrites every such token to the account's display name in EVERY response —
so the text the browser holds can never equal the server's, and any warning that names an
account (``opening_on_action_date``: 「{account:tw_broker} 在 2026-03-16 有一筆…」) answered
``warnings_changed`` on every save: the owner looped forever, on the create form and on the
ledger edit dialog alike.

The binding is now an OPAQUE key per issue (``ack_key``), computed by the server from the code
and the RAW sentence and sent back verbatim (``acked_keys``). The resolver only rewrites strings
containing ``{account:``, which a hex key never does, and the key still changes whenever the
sentence does. This test drives the account-bearing warning through the wire exactly as the
browser does — the text resolved, the key untouched.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import upsert_instrument, upsert_opening
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from tests.conftest import GOLDEN_NOW, DashboardClientFactory

D = Decimal
_TOKEN = re.compile(r"\{account:([^{}\s]+)\}")


def _seed(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Semiconductors", name="TSMC", board="TWSE"))
    # An opening built ON the action date — D3's account-naming warning.
    upsert_opening(conn, account_id="tw_broker", symbol="2330", shares=D("1000"),
                   original_cost_total=D("500000"), build_date=date(2026, 3, 16))
    upsert_prices(conn, [PriceRow(instrument="2330", market=Market.TW,
                                  as_of=date(2026, 6, 9), close=D("600"), source="test")],
                  fetched_at=GOLDEN_NOW)


def _split(ratio_to: str) -> dict[str, Any]:
    return {"account_id": "tw_broker", "date": "2026-03-16", "kind": "SPLIT",
            "from_symbol": "2330", "to_symbol": "2330", "ratio_to": ratio_to, "ratio_from": "1"}


def _as_the_browser_sees(value: Any) -> Any:
    """``web/api.js`` ``_resolveRefs``: every ``{account:<id>}`` inside a string becomes the
    account's display name — here a stand-in, which is all that matters: it is not the id."""
    if isinstance(value, str):
        return _TOKEN.sub(lambda m: f"〔{m.group(1)} 的顯示名稱〕", value)
    if isinstance(value, list):
        return [_as_the_browser_sees(v) for v in value]
    if isinstance(value, dict):
        return {k: _as_the_browser_sees(v) for k, v in value.items()}
    return value


def _preview(client: TestClient, ratio_to: str) -> dict[str, Any]:
    p = client.post("/api/ledgers/corporate-actions/preview", json=_split(ratio_to))
    assert p.status_code == 200, p.json()
    body: dict[str, Any] = _as_the_browser_sees(p.json())
    return body


def _keys(preview: dict[str, Any]) -> list[str]:
    return [str(i["ack_key"]) for i in preview["issues"] if i["sev"] != "error"]


def test_the_account_naming_warning_reaches_the_browser_resolved_with_an_untouched_key(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed)
    raw = client.post("/api/ledgers/corporate-actions/preview", json=_split("2")).json()
    d3 = next(i for i in raw["issues"] if i["code"] == "opening_on_action_date")
    assert "{account:tw_broker}" in d3["text"]          # the server's sentence names the account
    seen = next(i for i in _preview(client, "2")["issues"]
                if i["code"] == "opening_on_action_date")
    assert "{account:" not in seen["text"]               # …and the browser never sees the token
    assert seen["ack_key"] == d3["ack_key"]              # the key crosses the resolver intact
    assert "{account:" not in d3["ack_key"]


def test_acknowledging_an_account_naming_warning_saves(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The reviewer's repro: pre-fix this answered ``warnings_changed`` forever."""
    client = dashboard_client_factory(_seed)
    keys = _keys(_preview(client, "2"))
    r = client.post("/api/ledgers/corporate-actions",
                    json={**_split("2"), "ack_warnings": True, "acked_keys": keys})
    assert r.status_code == 201, r.json()


def _seed_with_band(conn: sqlite3.Connection) -> None:
    """D3 (names the account) beside D44 (a target band set before the split — its sentence
    carries the ratio), so a 1→2 and a 1→4 split share one warning and differ on the other."""
    _seed(conn)
    upsert_instrument(
        conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                         sector="Semiconductors", name="TSMC", board="TWSE",
                         target_low=D("600"), target_high=D("900")),
        today=date(2026, 2, 1))


def test_the_key_still_binds_the_ack_to_the_sentence_it_acknowledged(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The opaque key changes with the sentence, so keys read for one input acknowledge
    nothing about another — CR-07's guarantee, kept — and an UNCHANGED sentence keeps its key
    (D3 reads the same for both ratios), so only the new warning is asked about."""
    client = dashboard_client_factory(_seed_with_band)
    seen_2, seen_4 = _preview(client, "2"), _preview(client, "4")
    for_2, for_4 = _keys(seen_2), _keys(seen_4)
    d3 = {str(i["ack_key"]) for i in seen_4["issues"] if i["code"] == "opening_on_action_date"}
    assert d3 and d3 <= set(for_2)                # the account-naming sentence did not change
    assert not set(for_4) <= set(for_2)           # the band sentence did
    stale = client.post("/api/ledgers/corporate-actions",
                        json={**_split("4"), "ack_warnings": True, "acked_keys": for_2})
    assert stale.status_code == 422, stale.json()
    err = _as_the_browser_sees(stale.json()["error"])
    assert err["code"] == "warnings_changed"
    assert "{account:" not in err["message"]
    assert [i["ack_key"] for i in err["issues"] if i["sev"] != "error"] == for_4
    ok = client.post("/api/ledgers/corporate-actions",
                     json={**_split("4"), "ack_warnings": True, "acked_keys": for_4})
    assert ok.status_code == 201, ok.json()


def test_the_edit_door_accepts_the_same_keys(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed)
    made = client.post("/api/ledgers/corporate-actions",
                       json={**_split("2"), "ack_warnings": True})
    assert made.status_code == 201, made.json()
    action_id = made.json()["ids"][0]
    edit = {**_split("3"), "ack_warnings": False}
    first = client.put(f"/api/ledgers/corporate-actions/{action_id}", json=edit)
    assert first.status_code == 422, first.json()
    err = _as_the_browser_sees(first.json()["error"])
    assert err["code"] == "warnings_unacknowledged"
    keys = [i["ack_key"] for i in err["issues"] if i["sev"] != "error"]
    assert any(i["code"] == "opening_on_action_date" for i in err["issues"])
    again = client.put(f"/api/ledgers/corporate-actions/{action_id}",
                       json={**edit, "ack_warnings": True, "acked_keys": keys})
    assert again.status_code == 200, again.json()
