"""L-1 at the real doors: what the owner reads in the import preview's 原因 column.

The seam is pinned in ``tests/data_ingestion/test_m2_unknown_account_message.py``; this file
asserts the same sentence through ``POST /api/import/preview`` for **all five** import kinds,
because the door is not the seam:

* ``import_preview`` runs FU-D19's ``normalize_import_csv`` first (header canonicalisation +
  date-column rewrite) — neither step touches the ``account`` cell, which is asserted here
  rather than assumed;
* ``_preview_wire`` puts ``issues[0].message`` — and only that one — into the row's ``reason``,
  so a correct finding that is not FIRST is a finding the owner never sees.

Before the repair the owner read 「帳戶不可空白」 from 現金 and 「帳戶  不存在」 — two spaces, no
name — from 公司行動 / 換匯 / 股利 / 期初庫存, for the identical blank cell in the identical
column. The last case drives ``POST /api/input/manual/preview``: the manual 手動輸入 form runs
``validate_transaction``, whose own copy of the sentence had the same defect and is not
protected by the transaction CSV door's earlier blank-cell rejection.
"""

import re
import sqlite3
from typing import Any

import pytest

from portfolio_dash.data_ingestion.cash_import import CASH_MOVEMENT_COLUMNS
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.corporate_action_import import CORPORATE_ACTION_COLUMNS
from portfolio_dash.data_ingestion.dividend_import import DIVIDEND_COLUMNS
from portfolio_dash.data_ingestion.fx_import import FX_COLUMNS
from portfolio_dash.data_ingestion.opening_import import OPENING_COLUMNS
from tests.conftest import DashboardClientFactory

_CJK = re.compile(r"[一-鿿]")

#: Python / pydantic internals that must never reach the 原因 column.
_LEAKS = ("KeyError", "is not a valid", "<class", "Traceback", "ValueError")

_BLANK_SENTENCE = "帳戶不可空白"
_NAMED_SENTENCE = "帳戶 zz_unknown 不存在"

#: kind -> (canonical header, one clean row minus its ``account`` cell)
_KINDS: dict[str, tuple[list[str], dict[str, str]]] = {
    "cash": (CASH_MOVEMENT_COLUMNS,
             {"date": "2026-01-05", "kind": "DEPOSIT", "ccy": "TWD", "amount": "400000"}),
    "corporate_actions": (CORPORATE_ACTION_COLUMNS,
                          {"date": "2026-01-05", "kind": "SPLIT", "from_symbol": "AAPL",
                           "to_symbol": "AAPL", "ratio_to": "2", "ratio_from": "1"}),
    "fx": (FX_COLUMNS,
           {"date": "2026-01-05", "from_ccy": "TWD", "from_amount": "100000",
            "to_ccy": "USD", "to_amount": "3000"}),
    "dividends": (DIVIDEND_COLUMNS,
                  {"symbol": "AAPL", "date": "2026-01-05", "type": "CASH", "gross": "100"}),
    "openings": (OPENING_COLUMNS,
                 {"symbol": "AAPL", "shares": "10", "original_cost_total": "1000",
                  "build_date": "2026-01-05"}),
}

#: The row's machine ``code`` (FU-D33) for each kind's fixture — measured BEFORE the repair
#: and asserted after, because this item moves a message and must move nothing else. It is
#: ``_row_code``'s only value, ``unregistered_symbol``, wherever the fixture's symbol is not
#: in the registry, and null otherwise; it is NOT the issue kind (that is pinned at the seam).
_ROW_CODE: dict[str, str | None] = {
    "cash": None, "corporate_actions": None, "fx": None,
    "dividends": "unregistered_symbol", "openings": "unregistered_symbol",
}


def _seed(conn: sqlite3.Connection) -> None:
    """Accounts only. Every row below is refused on its ``account`` cell, so nothing further
    needs to exist for the verdict under test to be reached."""
    seed_accounts(conn)
    conn.commit()


def _csv(kind: str, account: str) -> str:
    columns, cells = _KINDS[kind]
    values = dict(cells)
    values["account"] = account
    return (",".join(columns) + "\n"
            + ",".join(values.get(column, "") for column in columns) + "\n")


def _preview(client: Any, kind: str, csv_text: str) -> Any:
    response = client.post(
        "/api/import/preview", json={"kind": kind, "csv_text": csv_text})
    assert response.status_code == 200, response.text
    return response.json()


def _assert_readable(reason: str) -> None:
    assert _CJK.search(reason), reason
    assert not any(leak in reason for leak in _LEAKS), reason


@pytest.mark.parametrize("kind", sorted(_KINDS))
def test_a_blank_account_cell_names_the_blank_at_the_door(
    dashboard_client_factory: DashboardClientFactory, kind: str
) -> None:
    """The user-visible half of L-1: 「帳戶  不存在」 in the 原因 column told the owner that an
    account they never typed does not exist, in the one column whose job is to name the cell
    to go and fix."""
    client = dashboard_client_factory(_seed)
    body = _preview(client, kind, _csv(kind, ""))
    row = body["rows"][0]
    assert row["status"] == "error", (kind, row)
    assert row["code"] == _ROW_CODE[kind], (kind, row)
    _assert_readable(row["reason"])
    assert row["reason"] == _BLANK_SENTENCE, (kind, row["reason"])
    assert "  " not in row["reason"], (kind, repr(row["reason"]))


@pytest.mark.parametrize("kind", sorted(_KINDS))
def test_a_named_unknown_account_is_unchanged_at_the_door(
    dashboard_client_factory: DashboardClientFactory, kind: str
) -> None:
    """Counter-evidence, per kind: the sentence that was already right is byte-identical."""
    client = dashboard_client_factory(_seed)
    body = _preview(client, kind, _csv(kind, "zz_unknown"))
    row = body["rows"][0]
    assert row["status"] == "error", (kind, row)
    assert row["code"] == _ROW_CODE[kind], (kind, row)
    assert row["reason"] == _NAMED_SENTENCE, (kind, row["reason"])


def test_a_clean_row_beside_a_blank_one_still_imports(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """A blank account cell is a ROW-level mistake, so the file is not condemned with it: the
    sibling still previews clean and the summary keeps the two apart."""
    client = dashboard_client_factory(_seed)
    header, cells = _KINDS["cash"][0], dict(_KINDS["cash"][1])
    good = dict(cells, account="schwab")
    bad = dict(cells, account="")
    text = (",".join(header) + "\n"
            + ",".join(bad.get(c, "") for c in header) + "\n"
            + ",".join(good.get(c, "") for c in header) + "\n")
    body = _preview(client, "cash", text)
    first, second = body["rows"]
    assert first["status"] == "error" and first["reason"] == _BLANK_SENTENCE, first
    assert second["status"] == "ok", second
    assert body["summary"] == {"total": 2, "ok": 1, "warn": 0, "error": 1}, body["summary"]


# --- the MANUAL transaction form: validate_transaction's own copy of the sentence --------


@pytest.mark.parametrize(
    ("account_id", "expected"),
    [("", _BLANK_SENTENCE), ("   ", _BLANK_SENTENCE), ("zz_unknown", _NAMED_SENTENCE)],
)
def test_the_manual_transaction_form_says_the_same_thing(
    dashboard_client_factory: DashboardClientFactory, account_id: str, expected: str
) -> None:
    """``ManualBody.account_id`` carries no length constraint, so a blank reaches
    ``validate_transaction`` — the site the transaction CSV door never exposes, because
    ``csv_import._cell`` refuses a blank required cell first. The wire ``field`` stays
    ``account_id`` (``api/wire.py::_ISSUE_FIELD``), which is why the message itself must not
    name a CSV column."""
    client = dashboard_client_factory(_seed)
    response = client.post("/api/input/manual/preview", json={
        "account_id": account_id, "symbol": "AAPL", "side": "BUY",
        "date": "2026-01-05", "shares": "10", "price": "100"})
    assert response.status_code == 200, response.text
    found = [i for i in response.json()["issues"] if i["code"] == "unknown_account"]
    assert [i["text"] for i in found] == [expected], response.json()["issues"]
    assert found[0]["sev"] == "error" and found[0]["field"] == "account_id", found
    assert "  " not in found[0]["text"], repr(found[0]["text"])


# --- R-2: the auto-register refusal, the ONE site wave 8 did not register ----------------
#
# ``manual_commit`` builds its own ``error_body`` sentence before the auto-register branch:
# ``f"帳戶 {body.account_id} 不存在，無法自動註冊標的"``. With a blank id that renders
# 「帳戶␣␣不存在，無法自動註冊標的」 — the exact two-space, no-name defect the five importer
# comments each name as the thing they removed, still reachable from the 手動輸入 form the
# moment the symbol is ALSO unregistered (which on day one it always is). It is an
# ``error_body`` envelope rather than an :class:`Issue`, which is precisely why the seam's
# five registrations missed it — the copy is one layer up.

_COMMIT_SUFFIX = "，無法自動註冊標的"


@pytest.mark.parametrize(
    ("account_id", "expected"),
    [("", _BLANK_SENTENCE + _COMMIT_SUFFIX),
     ("   ", _BLANK_SENTENCE + _COMMIT_SUFFIX),
     ("zz_unknown", _NAMED_SENTENCE + _COMMIT_SUFFIX)],
)
def test_the_auto_register_refusal_says_the_same_thing(
    dashboard_client_factory: DashboardClientFactory, account_id: str, expected: str
) -> None:
    """★ R-2. One rule, one sentence, plus this door's own reason for refusing.

    The named-account variant is byte-unchanged: this item moves a message and must move
    nothing else — not the 400, not the ``validation_error`` code, not the ``account_id``
    field the form highlights."""
    client = dashboard_client_factory(_seed)
    response = client.post("/api/input/manual/commit", json={
        "account_id": account_id, "symbol": "ZZTOP", "side": "BUY",
        "date": "2026-01-05", "shares": "10", "price": "100"})
    assert response.status_code == 400, response.text
    err = response.json()["error"]
    assert err["code"] == "validation_error", err
    assert err["field"] == "account_id", err
    assert err["message"] == expected, err["message"]
    assert "  " not in err["message"], repr(err["message"])
    _assert_readable(err["message"])


def test_the_auto_register_refusal_wrote_nothing(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """Control: the refusal is still a refusal. A message repair must not turn a rejected
    entry into a written one — nor register the symbol it declined to book."""
    client = dashboard_client_factory(_seed)
    response = client.post("/api/input/manual/commit", json={
        "account_id": "", "symbol": "ZZTOP", "side": "BUY",
        "date": "2026-01-05", "shares": "10", "price": "100"})
    assert response.status_code == 400, response.text
    rows = client.get("/api/ledgers/transactions").json()["rows"]
    assert not [r for r in rows if r["symbol"] == "ZZTOP"], rows
