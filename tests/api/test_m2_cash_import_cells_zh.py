"""J-2 / J-3 at the real door: ``POST /api/import/preview`` with ``kind="cash"``.

The seam is pinned in ``tests/data_ingestion/test_m2_cash_import_cells_zh.py``; this file
asserts what the owner actually sees. ``_preview_wire`` puts ``r.issues[0].message`` into the
row's ``reason`` field and ``web/input.js`` renders that verbatim into the 原因 cell, so
「金額（amount）不可空白」 on a file that has no ``amount`` column, and 「無法辨識：GBP」 on a
file that says ``gbp``, are user-visible defects rather than a style preference: both send the
owner looking through a spreadsheet for something that is not in it.

⚠ **The door is not the seam.** ``import_preview`` runs FU-D19's ``normalize_import_csv``
first, which canonicalizes the headers and rewrites the date column to ISO. Neither step
invents a missing column nor touches a currency cell — asserted here, because if it did, this
file would be measuring the normalizer rather than the readers.

**K-1 / K-2 (appended)** — the two J reported and did not fix: the same missing-column /
blank-cell confusion on ``ccy`` (K-1), and ``帳戶  不存在`` — two spaces and no name — for a
blank ``account`` cell (K-2). K-2's message is built in ``validate_cash_movement``, the guard
the MANUAL form runs as well, so the last two cases here drive ``POST /api/cash/movements``:
one seam, two doors, and a pin on each side of it.
"""

import re
import sqlite3
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.cash_import import CASH_MOVEMENT_COLUMNS
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.import_templates import render_import_template
from portfolio_dash.shared.enums import Currency
from tests.conftest import DashboardClientFactory

_CJK = re.compile(r"[一-鿿]")
_SUPPORTED = "／".join(c.value for c in Currency)
_LEAKS = ("KeyError", "is not a valid", "<class", "Traceback", "ValueError")


def _seed(conn: sqlite3.Connection) -> None:
    """Accounts only — every row below is a DEPOSIT, which the withdraw guard never refuses,
    so no funded pool is needed to reach the parse verdict."""
    seed_accounts(conn)
    conn.commit()


def _csv(*, columns: list[str], rows: int = 1, **cells: str) -> str:
    values = {"account": "schwab", "date": "2026-01-05", "kind": "DEPOSIT",
              "ccy": "TWD", "amount": "400000", "acq_home_amount": "", "note": ""}
    values.update(cells)
    body = ",".join(values[c] for c in columns) + "\n"
    return ",".join(columns) + "\n" + body * rows


def _preview(client: TestClient, csv_text: str) -> Any:
    response = client.post(
        "/api/import/preview", json={"kind": "cash", "csv_text": csv_text})
    assert response.status_code == 200, response.text
    return response.json()


def _assert_readable(reason: str) -> None:
    assert _CJK.search(reason), reason
    assert not any(leak in reason for leak in _LEAKS), reason


# --- J-2 -------------------------------------------------------------------------------


def test_a_missing_amount_column_names_the_column_at_the_door(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """A missing header is a FILE-level mistake, so there is no clean sibling to keep — and
    each row must say which column the file lacks, not that a cell is blank."""
    client = dashboard_client_factory(_seed)
    body = _preview(client, _csv(
        columns=[c for c in CASH_MOVEMENT_COLUMNS if c != "amount"], rows=2))
    assert [r["status"] for r in body["rows"]] == ["error", "error"], body["rows"]
    for r in body["rows"]:
        _assert_readable(r["reason"])
        assert r["reason"] == "缺少必填欄位 amount", r["reason"]
        assert r["data"] == {}, r["data"]
    assert body["summary"] == {"total": 2, "ok": 0, "warn": 0, "error": 2}, body["summary"]


def test_a_blank_amount_cell_keeps_its_own_sentence_at_the_door(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """With the column present the blank cell is a ROW-level mistake: one row is refused,
    its clean sibling is imported, and the sentence names the cell."""
    client = dashboard_client_factory(_seed)
    csv_text = (",".join(CASH_MOVEMENT_COLUMNS) + "\n"
                + "schwab,2026-01-05,DEPOSIT,TWD,400000,,\n"
                + "schwab,2026-02-05,DEPOSIT,TWD,,,\n")
    body = _preview(client, csv_text)
    good, bad = body["rows"]
    assert good["status"] == "ok" and good["reason"] is None, good
    assert bad["status"] == "error", bad
    _assert_readable(bad["reason"])
    assert bad["reason"] == "金額（amount）不可空白", bad["reason"]
    assert body["summary"] == {"total": 2, "ok": 1, "warn": 0, "error": 1}, body["summary"]


def test_an_absent_optional_column_still_imports_at_the_door(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """``acq_home_amount`` is optional (F1) and must keep tolerating an absent column — the
    ordinary hand-made cash file has neither it nor ``note``."""
    client = dashboard_client_factory(_seed)
    body = _preview(client, _csv(columns=["account", "date", "kind", "ccy", "amount"]))
    row = body["rows"][0]
    assert row["status"] == "ok", row
    assert row["reason"] is None, row
    assert "acq_home_amount" not in row["data"], row["data"]


# --- J-3 -------------------------------------------------------------------------------


def test_an_unknown_currency_is_quoted_as_typed_at_the_door(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed)
    csv_text = (",".join(CASH_MOVEMENT_COLUMNS) + "\n"
                + "schwab,2026-01-05,DEPOSIT,TWD,400000,,\n"
                + "schwab,2026-02-05,DEPOSIT,gbp,400000,,\n")
    body = _preview(client, csv_text)
    good, bad = body["rows"]
    assert good["status"] == "ok", good
    assert bad["status"] == "error", bad
    _assert_readable(bad["reason"])
    assert bad["reason"] == f"幣別（ccy）無法辨識：gbp（僅支援 {_SUPPORTED}）", bad["reason"]
    assert "GBP" not in bad["reason"], bad["reason"]


@pytest.mark.parametrize("text", ["usd", "USD", "Usd"])
def test_a_valid_currency_is_still_imported_in_any_case_at_the_door(
    dashboard_client_factory: DashboardClientFactory, text: str
) -> None:
    """Counter-evidence: J-3 changes the rejection's wording, never what the door accepts.
    The stored payload keeps the canonical upper-case code either way."""
    client = dashboard_client_factory(_seed)
    body = _preview(client, _csv(
        columns=CASH_MOVEMENT_COLUMNS, ccy=text, amount="1000"))
    row = body["rows"][0]
    assert row["status"] == "ok", row
    assert row["data"]["ccy"] == "USD", row["data"]


# --- the normalizer is not the thing under test ----------------------------------------


def test_the_header_normalizer_neither_invents_a_column_nor_rewrites_a_currency(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """FU-D19 canonicalizes ANNOTATED headers (``acq_home_amount(選填)`` -> the bare name),
    which is the one way a file could carry a column while looking as though it does not. The
    DOWNLOADED template is annotated, so it must still preview CLEAN — otherwise the missing-
    column case above would be measuring the normalizer instead of the reader. The header is
    taken from ``render_import_template`` rather than retyped, so a template change cannot
    leave this pin certifying a shape the app no longer emits.

    The same row proves the second half: the normalizer touches headers and the date column
    only, so a lower-case ``usd`` reaches ``_currency`` exactly as typed (and is accepted —
    J-3 moves the rejection's wording, not the door's acceptance)."""
    client = dashboard_client_factory(_seed)
    annotated = render_import_template("cash").splitlines()[0]
    assert "(" in annotated, annotated  # the annotation is the point of the case
    body = _preview(client, annotated + "\nschwab,2026-01-05,DEPOSIT,usd,1000,,\n")
    row = body["rows"][0]
    assert row["status"] == "ok", row
    assert row["data"]["amount"] == "1000" and row["data"]["ccy"] == "USD", row["data"]


# --- K-1: the ccy column, at the door --------------------------------------------------


def test_a_missing_ccy_column_names_the_column_at_the_door(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """J-2's defect one column over: 「幣別（ccy）不可空白」 on a file with no ``ccy`` column
    sends the owner looking for an empty cell that cannot exist. A missing header is a
    FILE-level mistake, so every row says so."""
    client = dashboard_client_factory(_seed)
    body = _preview(client, _csv(
        columns=[c for c in CASH_MOVEMENT_COLUMNS if c != "ccy"], rows=2))
    assert [r["status"] for r in body["rows"]] == ["error", "error"], body["rows"]
    for r in body["rows"]:
        _assert_readable(r["reason"])
        assert r["reason"] == "缺少必填欄位 ccy", r["reason"]
        assert r["data"] == {}, r["data"]
    assert body["summary"] == {"total": 2, "ok": 0, "warn": 0, "error": 2}, body["summary"]


def test_a_blank_ccy_cell_keeps_its_own_sentence_at_the_door(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The row-level half, and the counter-evidence: with the column present the sentence is
    unchanged, ONE row is rejected, and its clean sibling still imports."""
    client = dashboard_client_factory(_seed)
    body = _preview(client, ",".join(CASH_MOVEMENT_COLUMNS) + "\n"
                    + "schwab,2026-01-05,DEPOSIT,,400000,,\n"
                    + "schwab,2026-01-06,DEPOSIT,TWD,400000,,\n")
    first, second = body["rows"]
    _assert_readable(first["reason"])
    assert first["status"] == "error" and first["reason"] == "幣別（ccy）不可空白", first
    assert second["status"] == "ok" and second["data"]["ccy"] == "TWD", second
    assert body["summary"] == {"total": 2, "ok": 1, "warn": 0, "error": 1}, body["summary"]


# --- K-2: a blank account cell, at the door --------------------------------------------


def test_a_blank_account_cell_names_the_blank_at_the_door(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """What the owner actually saw: ``帳戶  不存在`` — two spaces where the name should be,
    asserting that an account they never typed does not exist."""
    client = dashboard_client_factory(_seed)
    body = _preview(client, _csv(columns=CASH_MOVEMENT_COLUMNS, account=""))
    row = body["rows"][0]
    assert row["status"] == "error", row
    _assert_readable(row["reason"])
    assert row["reason"] == "帳戶不可空白", row["reason"]
    assert "  " not in row["reason"], repr(row["reason"])


def test_an_unknown_account_id_is_unchanged_at_the_door(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The named half stays byte-identical — K-2 splits one sentence in two, it does not
    reword the one that was already right."""
    client = dashboard_client_factory(_seed)
    body = _preview(client, _csv(columns=CASH_MOVEMENT_COLUMNS, account="zz_unknown"))
    row = body["rows"][0]
    assert row["status"] == "error", row
    assert row["reason"] == "帳戶 zz_unknown 不存在", row["reason"]


# --- K-2 at the MANUAL door: one seam, two doors (architecture.md C3) ------------------


def test_the_manual_cash_door_says_the_same_thing_about_a_blank_account(
    api_client: TestClient,
) -> None:
    """``validate_cash_movement`` is the guard BOTH cash doors run, so the form gets the
    repair for free — and must be pinned, or the two doors can drift apart again. The wire
    ``field`` is presentation and stays ``account_id`` (``_MOVEMENT_ISSUE_FIELD``), which is
    also why the message itself must not name a CSV column."""
    response = api_client.post("/api/cash/movements", json={
        "account_id": "", "date": "2026-07-01", "kind": "deposit",
        "ccy": "TWD", "amount": "100"})
    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["code"] == "validation_error" and error["field"] == "account_id", error
    assert error["message"] == "帳戶不可空白", error["message"]
    assert "  " not in error["message"], repr(error["message"])


def test_the_manual_cash_door_still_names_an_unknown_account(
    api_client: TestClient,
) -> None:
    """The pin ``tests/contract/test_cash_movement_guard_contract.py`` already holds for
    ``ghost``, restated here beside its blank sibling so the pair is visible in one file."""
    response = api_client.post("/api/cash/movements", json={
        "account_id": "zz_unknown", "date": "2026-07-01", "kind": "deposit",
        "ccy": "TWD", "amount": "100"})
    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["code"] == "validation_error" and error["field"] == "account_id", error
    assert error["message"] == "帳戶 zz_unknown 不存在", error["message"]
