"""DEF-055 (functional test manual B-16, owner ruling 2026-09-24): the 更正 modal's preview
forecasts the refund of the row it is EDITING under that row's own settlement regime.

Measured on demo (R3 verification): opening 編輯 on #29 — a 2026 TW trade booked at
``discount 0.23`` (fee 285, already the discounted fee) — posted ``/api/input/manual/preview``
with ``replaces_txn_id 29, fee_override 285`` and got ``rebate_estimate "219"`` (285 × 0.77):
the double benefit DEF-010 removed from every forecaster. The preview read the CURRENT rule set
(charge-first, ``discount 1``) instead of the snapshot of the fee it was showing.

Why DEF-010's guard missed it: ``test_def010_…::test_the_manual_draft_hint_is_null_when_the_
rule_discounts_at_settlement`` only put a NEW draft in front of the forecaster, under a
discounting RULE. No test ever previewed a REPLACEMENT whose own snapshot is discounted while
the rule is charge-first — exactly the demo's state after the rule was fixed.

Every test here is a MIRROR: the preview's answer must equal what ``GET /api/rebates``
forecasts for the same row once the edit is saved (``domain-ledger.md``: a preview must
mirror what will be written).
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import insert_transaction
from portfolio_dash.shared.models.enums import Side

_DAY = date(2026, 5, 4)                    # a pending month at GOLDEN_NOW (2026-06-11)
_DISCOUNTED_CLEANED = {                    # #29 after scripts/clean_rebate_snapshots.py
    "engine": "v2", "brokerage": "0.001425", "discount": "0.23", "min_fee": "20",
    "rebate_rate": "0", "rebate_rate_was": "0.77", "rounding": "floor",
    "cleaned": "DEF-010（owner 裁定 2026-09-24）"}
_DISCOUNTED_DOUBLE = {**_DISCOUNTED_CLEANED, "rebate_rate": "0.77"}   # #33 / #44 as found
_CHARGE_FIRST = {**_DISCOUNTED_DOUBLE, "discount": "1"}
_SUPPLIED = {"source": "supplied", "fee": "285", "tax": "0"}          # a broker statement's


def _row(conn: sqlite3.Connection, snapshot: dict[str, str]) -> int:
    return insert_transaction(
        conn, account_id="tw_broker", symbol="2330", side=Side.BUY, quantity=Decimal("870"),
        price=Decimal("1000"), fees=Decimal("285"), tax=Decimal("0"), trade_date=_DAY,
        fee_rule_snapshot=snapshot)


def _edit_body(txn_id: int, **over: Any) -> dict[str, Any]:
    """What the 更正 modal posts on OPEN: the stored values, fee/tax as overrides."""
    body = {"account_id": "tw_broker", "symbol": "2330", "side": "buy",
            "date": _DAY.isoformat(), "shares": "870", "price": "1000",
            "fee_override": "285", "tax_override": "0", "replaces_txn_id": txn_id}
    body.update(over)
    return body


def _inbox_expected(client: TestClient) -> str | None:
    """What /api/rebates forecasts for the month — None when the month is not forecast."""
    rows = client.get("/api/rebates").json()["rows"]
    may = next((r for r in rows if r["account_id"] == "tw_broker" and r["month"] == "2026-05"),
               None)
    return None if may is None else str(may["expected"])


@pytest.mark.parametrize(("snapshot", "expected"), [
    pytest.param(_DISCOUNTED_CLEANED, None, id="discounted-cleaned(#29)"),
    pytest.param(_DISCOUNTED_DOUBLE, None, id="discounted-uncleaned(#33,#44)"),
    pytest.param(_CHARGE_FIRST, "219", id="charge-first"),
    pytest.param(_SUPPLIED, "219", id="supplied(no discount recorded -> the rule set)"),
])
def test_the_edit_preview_forecasts_what_the_inbox_will_forecast_for_that_row(
    api_client: TestClient, golden_db: sqlite3.Connection,
    snapshot: dict[str, str], expected: str | None,
) -> None:
    txn_id = _row(golden_db, snapshot)
    preview = api_client.post("/api/input/manual/preview", json=_edit_body(txn_id)).json()
    assert preview["rebate_estimate"] == expected, preview
    # Save the edit exactly as the modal does when nothing money-bearing moved (a note).
    r = api_client.put(f"/api/ledgers/transactions/{txn_id}", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy", "date": _DAY.isoformat(),
        "shares": "870", "price": "1000", "fee": "285", "tax": "0", "note": "更正備註"})
    assert r.status_code == 200, r.text
    assert _inbox_expected(api_client) == expected


def test_a_recomputed_fee_is_charged_under_todays_rule_and_says_so(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """A core field moved and the fee is NOT carried over: the engine charges it under the
    rule in force (charge-first here) and the PUT stores THAT snapshot — so a discounted
    original does not suppress the forecast of the fee that replaces it."""
    txn_id = _row(golden_db, _DISCOUNTED_CLEANED)
    body = _edit_body(txn_id, shares="1000")
    del body["fee_override"], body["tax_override"]
    preview = api_client.post("/api/input/manual/preview", json=body).json()
    assert preview["fee"] == "1425"                     # floor(1,000,000 × 0.1425%)
    assert preview["rebate_estimate"] == "1097"         # floor(1425 × 0.77)
    r = api_client.put(f"/api/ledgers/transactions/{txn_id}", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy", "date": _DAY.isoformat(),
        "shares": "1000", "price": "1000", "fee": "285", "tax": "0"})
    assert r.status_code == 200, r.text
    assert _inbox_expected(api_client) == "1097"


def test_a_new_draft_is_unchanged(api_client: TestClient) -> None:
    """No row is replaced: the fee is charged under the rule set in force, as before."""
    body = _edit_body(0)
    del body["replaces_txn_id"]
    assert api_client.post("/api/input/manual/preview", json=body).json()[
        "rebate_estimate"] == "219"
