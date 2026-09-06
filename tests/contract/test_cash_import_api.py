"""Contract: the 6th CSV import kind (``cash``) through the real /api/import seam.

The kind's whole justification is that the BULK door must not be weaker than the single-row
form, so the load-bearing test here is the PARITY one: the same withdrawal, against the same
ledger, gets the same verdict and the same zh message through both doors.

The other half is registration. A kind can be fully implemented and completely unreachable
(audit F-28), so every route that keys off the kind string is exercised: template download,
preview, commit, and the unknown-kind 400 envelope.
"""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from portfolio_dash.api.deps import get_now
from tests.conftest import GOLDEN_NOW

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


#: M5-06: ``GET /api/cash`` balances are AS OF the request clock, and this file's rows (and
#: the shipped template's example rows) are dated 2026-07 — after the frozen GOLDEN_NOW of
#: 2026-06-11. The balance helper therefore reads the ledger as of a day after every row it
#: writes, so each assertion keeps checking what was WRITTEN, which is what this file is about.
_AFTER_EVERY_ROW = datetime(2026, 12, 31, 12, 0, tzinfo=ZoneInfo("Asia/Taipei"))


def _balance(client: TestClient, account_id: str, ccy: str) -> str | None:
    overrides = client.app.dependency_overrides  # type: ignore[attr-defined]
    overrides[get_now] = lambda: _AFTER_EVERY_ROW
    try:
        rows = client.get("/api/cash").json()["balances"]
    finally:
        overrides[get_now] = lambda: GOLDEN_NOW
    for row in rows:
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
    the same file, which is also the thing the example is teaching.

    The row count is derived from the template rather than written as a literal: the
    assertion that matters is "EVERY example row previews clean and writes", and a literal
    turns adding an example into an unrelated-looking test failure.
    """
    text = api_client.get(
        "/api/import/template", params={"kind": "cash"}).content.decode("utf-8")
    rows = len([ln for ln in text.strip().split("\r\n")[1:] if ln.strip()])
    preview = _preview(api_client, text)
    assert preview["summary"] == {"total": rows, "ok": rows, "warn": 0, "error": 0}
    response = _commit(api_client, text)
    assert response.status_code == 200, response.text
    assert response.json() == {"written": rows, "skipped": 0, "import_batch_id": 1}
    # 600,000 deposit − 50,000 withdrawal on top of the golden −495,000
    assert _balance(api_client, "tw_broker", "TWD") == "55000"
    # The schwab pool is where the three broker-statement kinds land, so this figure is an
    # end-to-end SIGN check for them: 100,000 opening + 12.34 interest − 8.01 margin
    # interest − 9.79 broker fee. Under the pre-2026-08-13 "WITHDRAW vs everything else"
    # predicate all three credited and this read 100,030.14.
    assert _balance(api_client, "schwab", "USD") == "99994.54"


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
    assert response.status_code == 200
    body = response.json()
    # REJECTED, not skipped (2026-08-14): the importer refused it. No `import_batch_id`,
    # because an import that wrote nothing has nothing to undo.
    assert body["written"] == 0 and body["skipped"] == 0 and body["rejected"] == 1
    assert body["rejected_rows"][0]["kind"] == "withdraw_insufficient_balance"
    assert _balance(api_client, "moomoo_my", "MYR") == "1000"  # untouched


def test_a_bulk_overdraft_cannot_ride_in_behind_a_clean_row(
    api_client: TestClient,
) -> None:
    """Partial success is the contract: the fundable row writes, the overdraft is refused."""
    csv_text = _HEADER + (
        "moomoo_my,2026-01-01,DEPOSIT,MYR,1000,,\n"
        "moomoo_my,2026-02-01,WITHDRAW,MYR,5000,,\n")
    response = _commit(api_client, csv_text)
    assert response.status_code == 200
    body = response.json()
    assert body["written"] == 1 and body["skipped"] == 0 and body["rejected"] == 1
    assert body["rejected_rows"][0]["row"] == 2 and body["import_batch_id"] == 1
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
    assert acked.status_code == 200
    # batch 1, not 2: the REFUSED commit above never created a batch record — an
    # import that wrote nothing has nothing to undo.
    assert acked.json() == {"written": 1, "skipped": 0, "import_batch_id": 1}
    assert _balance(api_client, "moomoo_my", "USD") == "500"


def test_the_response_shape_is_the_other_kinds_shape(api_client: TestClient) -> None:
    """No ``prices_restated``: that key belongs to the kind that moves the price basis
    (corporate_actions). A response shape that grows for every caller when one kind gains a
    behaviour is how a contract drifts."""
    response = _commit(api_client, _HEADER + "tw_broker,2026-07-01,DEPOSIT,TWD,1000,,\n")
    assert response.json() == {"written": 1, "skipped": 0, "import_batch_id": 1}


def test_the_date_column_is_normalized_like_every_other_kind(
    api_client: TestClient,
) -> None:
    """DATE_COLUMN_BY_KIND is registered, so the seam resolves ``2026/7/1`` to ISO instead
    of raising a KeyError on a date column that is not there (audit F-28's quiet failure)."""
    preview = _preview(api_client, _HEADER + "tw_broker,2026/7/1,DEPOSIT,TWD,1000,,\n")
    assert preview["summary"]["error"] == 0
    assert preview["rows"][0]["data"]["date"] == "2026-07-01"


# --- provenance: the same file twice, and the undo ------------------------------------


def test_re_importing_the_same_file_through_the_api_writes_nothing(
    api_client: TestClient,
) -> None:
    """Before provenance (2026-08-13) this doubled the ledger and reported success both
    times. The second call must write nothing and SAY it found the rows already here —
    "written 0, skipped 0" on its own reads like the file failed to parse."""
    csv_text = _HEADER + "tw_broker,2026-07-01,DEPOSIT,TWD,1000,,\n"
    first = _commit(api_client, csv_text)
    assert first.json() == {"written": 1, "skipped": 0, "import_batch_id": 1}

    # The cash kind ALSO carries a heuristic ``duplicate_movement`` advisory, so the
    # re-import stops at the warning gate first — which is the human-facing half. Ack it and
    # the exact, source-hash half still refuses to double-write.
    blocked = _commit(api_client, csv_text)
    assert blocked.status_code == 422
    assert blocked.json()["error"]["code"] == "warnings_unacknowledged"

    second = _commit(api_client, csv_text, ack=True)
    assert second.status_code == 200
    body = second.json()
    assert body["written"] == 0 and body["duplicates"] == 1
    # No SECOND batch: a commit that wrote nothing owns no rows, so a batch record for it
    # would claim in the history that an import happened.
    assert "import_batch_id" not in body
    assert len(api_client.get("/api/import/batches").json()["batches"]) == 1
    assert _balance(api_client, "tw_broker", "TWD") == "-494000"


def test_a_file_that_legitimately_repeats_a_row_writes_both(
    api_client: TestClient,
) -> None:
    """The adversarial half. Two identical deposits on one date is a real statement pair;
    a content-only idempotency key collapses them and drops a real movement."""
    row = "tw_broker,2026-07-01,DEPOSIT,TWD,1000,,\n"
    assert _commit(api_client, _HEADER + row + row).json()["written"] == 2
    assert _balance(api_client, "tw_broker", "TWD") == "-493000"


def test_deleting_a_batch_undoes_exactly_that_import(api_client: TestClient) -> None:
    """The undo is what makes an import safe to ATTEMPT on a real ledger: the alternative,
    restoring a pre-import backup, also discards everything entered since."""
    api_client.post("/api/cash/movements", json={
        "account_id": "tw_broker", "date": "2026-06-01", "kind": "deposit",
        "ccy": "TWD", "amount": "7"})
    batch = _commit(
        api_client, _HEADER + "tw_broker,2026-07-01,DEPOSIT,TWD,1000,,\n"
    ).json()["import_batch_id"]

    undo = api_client.delete(f"/api/import/batches/{batch}")
    assert undo.status_code == 200
    assert undo.json() == {"deleted": 1, "import_batch_id": batch}
    # The hand-entered row is untouched; only the imported one is gone.
    assert _balance(api_client, "tw_broker", "TWD") == "-494993"
    assert api_client.get("/api/import/batches").json()["batches"] == []
    # ...and the undo is COMPLETE: the file imports again rather than reporting duplicates.
    again = _commit(api_client, _HEADER + "tw_broker,2026-07-01,DEPOSIT,TWD,1000,,\n")
    assert again.json()["written"] == 1


def test_deleting_an_unknown_batch_404s(api_client: TestClient) -> None:
    response = api_client.delete("/api/import/batches/999")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
