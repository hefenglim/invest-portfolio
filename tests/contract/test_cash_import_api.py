"""Contract: the 6th CSV import kind (``cash``) through the real /api/import seam.

The kind's whole justification is that the BULK door must not be weaker than the single-row
form, so the load-bearing test here is the PARITY one: the same withdrawal, against the same
ledger, gets the same verdict and the same zh message through both doors.

The other half is registration. A kind can be fully implemented and completely unreachable
(audit F-28), so every route that keys off the kind string is exercised: template download,
preview, commit, and the unknown-kind 400 envelope.
"""

from typing import Any

from fastapi.testclient import TestClient

_HEADER = "account,date,kind,ccy,amount,acq_home_amount,note\n"


def _preview(client: TestClient, csv_text: str) -> dict[str, Any]:
    response = client.post(
        "/api/import/preview", json={"kind": "cash", "csv_text": csv_text})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _commit(client: TestClient, csv_text: str, *, ack: bool = False) -> Any:
    return client.post("/api/import/commit", json={
        "kind": "cash", "csv_text": csv_text, "ack_warnings": ack})


def _balance(client: TestClient, account_id: str, ccy: str) -> str | None:
    for row in client.get("/api/cash").json()["balances"]:
        if row["account_id"] == account_id and row["ccy"] == ccy:
            amount: str = row["amount"]
            return amount
    return None


# --- the kind is reachable end to end -------------------------------------------------


def test_template_download_serves_the_cash_kind(api_client: TestClient) -> None:
    response = api_client.get("/api/import/template", params={"kind": "cash"})
    assert response.status_code == 200
    text = response.content.decode("utf-8").lstrip("﻿")
    assert text.split("\r\n")[0] == (
        "account,date(YYYY-MM-DD),kind,ccy,amount,acq_home_amount(選填),note(選填)")
    assert "import_template_cash.csv" in response.headers["content-disposition"]


def test_the_downloaded_template_commits_as_it_stands(api_client: TestClient) -> None:
    """A template that previews clean but cannot be written is a template that lies. The
    example rows are self-funding on purpose — the WITHDRAW is covered by the DEPOSIT in
    the same file, which is also the thing the example is teaching."""
    text = api_client.get(
        "/api/import/template", params={"kind": "cash"}).content.decode("utf-8")
    preview = _preview(api_client, text)
    assert preview["summary"] == {"total": 4, "ok": 4, "warn": 0, "error": 0}
    response = _commit(api_client, text)
    assert response.status_code == 200, response.text
    assert response.json() == {"written": 4, "skipped": 0}
    # 600,000 deposit − 50,000 withdrawal on top of the golden −495,000
    assert _balance(api_client, "tw_broker", "TWD") == "55000"
    assert _balance(api_client, "schwab", "USD") == "100000"


def test_preview_and_commit_write_the_acquisition_cost(api_client: TestClient) -> None:
    """F1 end to end: the AMOUNT is stored and the rate is DERIVED on read, so a bulk-funded
    foreign pool carries a cost basis instead of degrading covered_ratio for the whole
    foreign exposure (F3)."""
    csv_text = _HEADER + "schwab,2026-07-05,OPENING,USD,100000,3135870,期初外幣\n"
    assert _commit(api_client, csv_text).status_code == 200
    row = next(m for m in api_client.get("/api/cash").json()["movements"]["rows"]
               if m["kind"] == "opening")
    assert row["acq_home_amount"] == "3135870"
    assert row["acq_home_ccy"] == "TWD"
    assert row["acq_rate"] == "31.3587"  # derived, never stored


def test_unknown_kind_still_400s(api_client: TestClient) -> None:
    """The kind string is the router's only key; a typo must not silently pick another."""
    for path in ("/api/import/preview", "/api/import/commit"):
        response = api_client.post(path, json={"kind": "cash_movements", "csv_text": _HEADER})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "validation_error"


# --- the guard: identical verdict through both doors ----------------------------------


def test_the_withdraw_guard_gives_THE_SAME_verdict_on_both_doors(
    api_client: TestClient,
) -> None:
    """The reason this kind waited for the extraction.

    Same ledger, same withdrawal, two doors. The form answers 422
    ``withdraw_insufficient_balance``; the import must refuse the row with the SAME message,
    not merely refuse it — a differently-worded refusal is a second guard, and a second guard
    is one that can be changed on one door only.
    """
    api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my", "date": "2026-01-01", "kind": "deposit",
        "ccy": "MYR", "amount": "1000"})

    manual = api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my", "date": "2026-02-01", "kind": "withdraw",
        "ccy": "MYR", "amount": "1500"})
    assert manual.status_code == 422
    assert manual.json()["error"]["code"] == "withdraw_insufficient_balance"

    preview = _preview(api_client, _HEADER + "moomoo_my,2026-02-01,WITHDRAW,MYR,1500,,\n")
    assert preview["summary"]["error"] == 1
    assert preview["rows"][0]["reason"] == manual.json()["error"]["message"]

    # ...and acknowledging warnings does NOT write it: the row is HARD, not a warning.
    response = _commit(api_client, _HEADER + "moomoo_my,2026-02-01,WITHDRAW,MYR,1500,,\n",
                       ack=True)
    assert response.status_code == 200 and response.json() == {"written": 0, "skipped": 1}
    assert _balance(api_client, "moomoo_my", "MYR") == "1000"  # untouched


def test_a_bulk_overdraft_cannot_ride_in_behind_a_clean_row(
    api_client: TestClient,
) -> None:
    """Partial success is the contract: the fundable row writes, the overdraft is skipped."""
    csv_text = _HEADER + (
        "moomoo_my,2026-01-01,DEPOSIT,MYR,1000,,\n"
        "moomoo_my,2026-02-01,WITHDRAW,MYR,5000,,\n")
    response = _commit(api_client, csv_text)
    assert response.status_code == 200
    assert response.json() == {"written": 1, "skipped": 1}
    assert _balance(api_client, "moomoo_my", "MYR") == "1000"


def test_a_soft_advisory_blocks_until_acknowledged(api_client: TestClient) -> None:
    """The N1 foreign-withdrawal advisory rides the existing ack_warnings flow: it warns,
    it does not coerce, and it does not silently write."""
    api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my", "date": "2026-01-01", "kind": "deposit",
        "ccy": "USD", "amount": "1000"})
    csv_text = _HEADER + "moomoo_my,2026-02-01,WITHDRAW,USD,500,,\n"
    preview = _preview(api_client, csv_text)
    assert preview["summary"] == {"total": 1, "ok": 0, "warn": 1, "error": 0}
    assert "換匯" in preview["rows"][0]["reason"]

    blocked = _commit(api_client, csv_text)
    assert blocked.status_code == 422
    assert blocked.json()["error"]["code"] == "warnings_unacknowledged"
    assert _balance(api_client, "moomoo_my", "USD") == "1000"

    acked = _commit(api_client, csv_text, ack=True)
    assert acked.status_code == 200 and acked.json() == {"written": 1, "skipped": 0}
    assert _balance(api_client, "moomoo_my", "USD") == "500"


def test_the_response_shape_is_the_other_kinds_shape(api_client: TestClient) -> None:
    """No ``prices_restated``: that key belongs to the kind that moves the price basis
    (corporate_actions). A response shape that grows for every caller when one kind gains a
    behaviour is how a contract drifts."""
    response = _commit(api_client, _HEADER + "tw_broker,2026-07-01,DEPOSIT,TWD,1000,,\n")
    assert response.json() == {"written": 1, "skipped": 0}


def test_the_date_column_is_normalized_like_every_other_kind(
    api_client: TestClient,
) -> None:
    """DATE_COLUMN_BY_KIND is registered, so the seam resolves ``2026/7/1`` to ISO instead
    of raising a KeyError on a date column that is not there (audit F-28's quiet failure)."""
    preview = _preview(api_client, _HEADER + "tw_broker,2026/7/1,DEPOSIT,TWD,1000,,\n")
    assert preview["summary"]["error"] == 0
    assert preview["rows"][0]["data"]["date"] == "2026-07-01"
