"""M5-02: the cash door must refuse an amount its own minor unit cannot record.

``data-and-pricing.md`` fixes the settlement precision per currency — TWD 0 dp (whole NT$),
USD/MYR 2 dp. Every display seam obeys it: ``f.money`` renders a TWD figure with no decimals,
the statement prints ``+123`` and the running balance ``57,875``. The WRITE door did not: a
``123.45`` TWD interest movement was stored verbatim, so the ledger held ``123.45`` while the
page could only ever show ``123`` — 0.45 that exists in the database and nowhere the owner can
see it, and a running balance off by the accumulated remainder.

``architecture.md`` says this door 「rejects bad input loudly; never silently coerces」, so the
fix is a rejection at the door, NOT a quantize on the way in: rounding it would move the
owner's money by 0.45 without saying so — the same class of error from the other side.

The counter-evidence matters as much as the finding: a legal TWD integer (including one
written ``123.00`` — Decimal equality ignores the exponent, and a trailing zero is not a
sub-unit) and a legal USD/MYR 2-dp amount must still write. A guard that also blocks those has
replaced a display bug with an entry bug.

The guard lives in ``validate_cash_movement``, so BOTH cash doors get it — the manual form and
the CSV importer — which is the property that function was extracted to hold.
"""

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.validate import (
    CashMovementInput,
    validate_cash_movement,
)
from portfolio_dash.shared.enums import Currency

# (ccy, amount, account) — amounts the currency's minor unit cannot represent.
_UNREPRESENTABLE = [
    ("TWD", "123.45", "tw_broker"),
    ("TWD", "0.5", "tw_broker"),
    ("TWD", "57875.45", "tw_broker"),
    ("USD", "1.234", "schwab"),
    ("MYR", "10.005", "moomoo_my"),
]

# (ccy, amount, account) — legal amounts that must STILL be accepted (counter-evidence).
_LEGAL = [
    ("TWD", "123", "tw_broker"),
    ("TWD", "123.00", "tw_broker"),      # trailing zeros are not a sub-unit
    ("TWD", "50000", "tw_broker"),
    ("USD", "1.23", "schwab"),
    ("USD", "1000", "schwab"),
    ("MYR", "1234.56", "moomoo_my"),
]


def _inert_pool(*_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover - never called
    raise AssertionError("the precision guard must reject before the pool is read")


@pytest.mark.parametrize(("ccy", "amount", "account"), _UNREPRESENTABLE)
def test_manual_door_rejects_an_unrepresentable_amount(
    api_client: TestClient, ccy: str, amount: str, account: str
) -> None:
    r = api_client.post("/api/cash/movements", json={
        "account_id": account, "date": "2026-07-01", "kind": "interest",
        "ccy": ccy, "amount": amount})
    assert r.status_code == 400, r.json()
    err = r.json()["error"]
    assert err["code"] == "validation_error"
    # The message must NAME the currency and the offending figure — a bare 「金額格式錯誤」
    # sends the owner back to a field with no idea what is wrong with it.
    assert ccy in err["message"] and amount in err["message"], err["message"]


@pytest.mark.parametrize(("ccy", "amount", "account"), _LEGAL)
def test_manual_door_still_accepts_a_legal_amount(
    api_client: TestClient, ccy: str, amount: str, account: str
) -> None:
    r = api_client.post("/api/cash/movements", json={
        "account_id": account, "date": "2026-07-01", "kind": "deposit",
        "ccy": ccy, "amount": amount})
    assert r.status_code == 201, r.json()


def test_validator_rejects_before_it_reads_the_pool(golden_db: sqlite3.Connection) -> None:
    """The precision failure is structural, so it precedes the balance arithmetic."""
    issues = validate_cash_movement(
        golden_db,
        CashMovementInput(account_id="tw_broker", date=date(2026, 7, 1),
                          kind="withdraw", ccy=Currency.TWD, amount=Decimal("100.25")),
        pool=_inert_pool)
    assert [i.kind for i in issues] == ["amount_precision"], issues
    assert not issues[0].needs_confirm, "a precision failure is hard, never an ack"


def test_csv_import_door_rejects_the_same_amount(api_client: TestClient) -> None:
    """The bulk door must not be the weaker one — same guard, same rejection."""
    csv_text = ("account,date,kind,ccy,amount,acq_home_amount,note\n"
                "tw_broker,2026-07-01,INTEREST,TWD,123.45,,\n")
    preview = api_client.post(
        "/api/import/preview", json={"kind": "cash", "csv_text": csv_text})
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["summary"]["error"] == 1, body
    assert "TWD" in body["rows"][0]["reason"], body["rows"][0]
