"""M5-05 (owner ruling 2026-09-06): every money door is bounded at 1e12, like shares/price.

``validate._MAX_MAGNITUDE`` has bounded a transaction's shares and price since audit M4, so
the fee quantize downstream cannot overflow the Decimal context. The three money doors had no
bound, and it showed three ways (measured 2026-09-02 / 2026-09-06):

* a 27/28-digit cash amount was WRITTEN (201) and the pool balance then lost its last digit in
  silence — ``Decimal``'s default context is 28 significant digits, so the sum was rounded;
* a 29-digit one raised ``InvalidOperation`` inside the quantize and the ArithmeticError net
  answered 422 「請於交易帳本修正該列」 — pointing at a ledger row that was never written;
* the 換匯 door and the dividend door had no bound at all: a 31-digit dividend previewed ``ok``.

The bound is applied BEFORE the precision check, because the precision check is the quantize
that raises. Counter-evidence is pinned beside every door: legal amounts still write.
"""

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

_29 = "1" + "0" * 28          # 1e28 — the value that used to raise inside the quantize
_31 = "1" + "0" * 30
_JUST_OVER = "1000000000000.5"  # 1e12 + 0.5: over the bound AND a TWD sub-unit


def _err(r: Any) -> dict[str, Any]:
    body: dict[str, Any] = r.json()["error"]
    return body


def _inert_pool(*_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover - never called
    raise AssertionError("the magnitude guard must reject before the pool is read")


# --- cash movements -----------------------------------------------------------------------


@pytest.mark.parametrize("amount", [_29, _31, _JUST_OVER, "1" + "0" * 29 + ".5"])
def test_cash_door_refuses_an_oversized_amount_with_the_right_message(
    api_client: TestClient, amount: str
) -> None:
    r = api_client.post("/api/cash/movements", json={
        "account_id": "tw_broker", "date": "2026-06-01", "kind": "deposit",
        "ccy": "TWD", "amount": amount})
    assert r.status_code == 400, r.json()
    err = _err(r)
    assert err["code"] == "validation_error" and err["field"] == "amount", err
    assert "過大" in err["message"], err["message"]
    assert "交易帳本" not in err["message"], "must not point at a ledger row that was never written"


@pytest.mark.parametrize(("ccy", "amount", "account"), [
    ("TWD", "123", "tw_broker"),
    ("TWD", "1000000000000", "tw_broker"),   # exactly 1e12 is still inside the bound
    ("USD", "1.23", "schwab"),
    ("MYR", "1234.56", "moomoo_my"),
])
def test_cash_door_still_accepts_a_legal_amount(
    api_client: TestClient, ccy: str, amount: str, account: str
) -> None:
    r = api_client.post("/api/cash/movements", json={
        "account_id": account, "date": "2026-06-01", "kind": "deposit",
        "ccy": ccy, "amount": amount})
    assert r.status_code == 201, r.json()


def test_validator_rejects_before_precision_and_before_the_pool(golden_db: Any) -> None:
    issues = validate_cash_movement(
        golden_db,
        CashMovementInput(account_id="tw_broker", date=date(2026, 6, 1), kind="withdraw",
                          ccy=Currency.TWD, amount=Decimal("1" + "0" * 29 + ".5")),
        pool=_inert_pool)
    assert [i.kind for i in issues] == ["amount_too_large"], issues
    assert not issues[0].needs_confirm


def test_cash_csv_door_refuses_the_same_amount(api_client: TestClient) -> None:
    csv_text = ("account,date,kind,ccy,amount,acq_home_amount,note\n"
                f"tw_broker,2026-06-01,DEPOSIT,TWD,{_29},,\n")
    body = api_client.post(
        "/api/import/preview", json={"kind": "cash", "csv_text": csv_text}).json()
    assert body["summary"]["error"] == 1, body
    assert "過大" in body["rows"][0]["reason"], body["rows"][0]


@pytest.mark.parametrize(("extra", "field"), [
    ({"acq_home_amount": _31}, "acq_home_amount"),
    ({"acq_rate": _31}, "acq_rate"),
])
def test_acquisition_cost_is_bounded_on_the_same_door(
    api_client: TestClient, extra: dict[str, str], field: str
) -> None:
    """Both spellings of the acquisition cost reach the same quantize; both used to answer
    the misdirected 422."""
    r = api_client.post("/api/cash/movements", json={
        "account_id": "schwab", "date": "2026-06-01", "kind": "deposit",
        "ccy": "USD", "amount": "1", **extra})
    assert r.status_code == 400, r.json()
    err = _err(r)
    assert err["field"] == field and "過大" in err["message"], err


# --- 換匯 ---------------------------------------------------------------------------------


@pytest.mark.parametrize(("body", "field"), [
    ({"from_amt": _29, "to_amt": "1"}, "from_amt"),
    ({"from_amt": "1", "to_amt": _29}, "to_amt"),
])
def test_fx_manual_door_refuses_an_oversized_leg(
    api_client: TestClient, body: dict[str, str], field: str
) -> None:
    r = api_client.post("/api/cash/fx", json={
        "account_id": "schwab", "date": "2026-06-01", "from_ccy": "TWD", "to_ccy": "USD",
        **body})
    assert r.status_code == 400, r.json()
    err = _err(r)
    assert err["code"] == "validation_error" and err["field"] == field, err
    assert "過大" in err["message"], err["message"]


def test_fx_edit_door_refuses_an_oversized_leg(api_client: TestClient) -> None:
    """Golden fx #1 is schwab 32,000 TWD -> 1,000 USD."""
    r = api_client.put("/api/ledgers/fx/1", json={
        "account_id": "schwab", "date": "2026-01-08", "from_ccy": "TWD",
        "from_amt": _29, "to_ccy": "USD", "to_amt": "1000"})
    assert r.status_code == 400, r.json()
    assert "過大" in _err(r)["message"], _err(r)


def test_fx_csv_door_refuses_an_oversized_leg(api_client: TestClient) -> None:
    csv_text = ("account,date,from_ccy,from_amount,to_ccy,to_amount\n"
                f"schwab,2026-06-01,TWD,{_29},USD,1\n")
    body = api_client.post(
        "/api/import/preview", json={"kind": "fx", "csv_text": csv_text}).json()
    assert body["summary"]["error"] == 1, body
    assert "過大" in body["rows"][0]["reason"], body["rows"][0]


def test_fx_manual_door_still_accepts_a_legal_conversion(api_client: TestClient) -> None:
    assert api_client.post("/api/cash/movements", json={
        "account_id": "schwab", "date": "2026-01-01", "kind": "deposit",
        "ccy": "TWD", "amount": "100000"}).status_code == 201
    r = api_client.post("/api/cash/fx", json={
        "account_id": "schwab", "date": "2026-06-01", "from_ccy": "TWD", "from_amt": "32000",
        "to_ccy": "USD", "to_amt": "1000"})
    assert r.status_code == 201, r.json()


# --- dividends ----------------------------------------------------------------------------


def test_dividend_csv_door_refuses_an_oversized_amount(api_client: TestClient) -> None:
    csv_text = ("account,symbol,date,type,gross,withholding,net,reinvest_shares,reinvest_price\n"
                f"tw_broker,2330,2026-05-05,CASH,{_31},0,{_31},,\n")
    body = api_client.post(
        "/api/import/preview", json={"kind": "dividends", "csv_text": csv_text}).json()
    assert body["summary"]["error"] == 1, body
    assert "過大" in body["rows"][0]["reason"], body["rows"][0]


def test_dividend_edit_door_refuses_an_oversized_amount(api_client: TestClient) -> None:
    """Golden dividend #1 is tw_broker 2330 CASH 5,000 on 2026-03-01."""
    r = api_client.put("/api/ledgers/dividends/1", json={
        "account_id": "tw_broker", "symbol": "2330", "date": "2026-03-01", "type": "CASH",
        "gross": _29, "withhold": "0", "net": _29})
    assert r.status_code == 400, r.json()
    assert "過大" in _err(r)["message"], _err(r)


def test_dividend_edit_door_still_accepts_a_legal_amount(api_client: TestClient) -> None:
    r = api_client.put("/api/ledgers/dividends/1", json={
        "account_id": "tw_broker", "symbol": "2330", "date": "2026-03-01", "type": "CASH",
        "gross": "5500", "withhold": "0", "net": "5500"})
    assert r.status_code == 200, r.json()
