"""H-2 at the real door: ``POST /api/import/preview`` kind="fx" printed Python's English.

The seam is pinned in ``tests/data_ingestion/test_m2_fx_import_cells_zh.py``; this file
asserts what the owner actually sees. ``_preview_wire`` puts ``r.issues[0].message`` into the
row's ``reason`` field and ``web/input.js`` renders that verbatim into the 原因 cell, so
``[<class 'decimal.ConversionSyntax'>]`` and ``'GBP' is not a valid Currency`` were
user-visible defects, not a style preference.

The cash door is asserted beside it on the equivalent input, because parity between the two
bulk doors is the property — the same posture ``test_m2_fx_import_door_non_finite.py`` and
``test_m2_import_date_zh.py`` take for the two defects already fixed in these files.

⚠ **The door is not the seam.** ``import_preview`` runs FU-D19's ``normalize_import_csv``
first, which canonicalizes the headers (so ``From_Amount（換出金額）`` still resolves) and
rewrites the date column to ISO. Neither step touches a currency or an amount cell, so the
shapes below reach ``_parse_row`` exactly as typed — asserted by the header-annotation case,
which must keep previewing CLEAN.
"""

import re
import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import insert_cash_movement
from portfolio_dash.shared.enums import Currency
from tests.conftest import DashboardClientFactory

_FX_HEADER = "account,date,from_ccy,from_amount,to_ccy,to_amount\n"
_CASH_HEADER = "account,date,kind,ccy,amount,acq_home_amount,note\n"
_CJK = re.compile(r"[一-鿿]")

#: One conversion the seeded pool covers — every cell-level case carries it as the sibling
#: that must survive, because a preview is a PER-ROW verdict.
_GOOD = "schwab,2026-01-05,TWD,100000,USD,3000\n"

_LEAKS = (
    "<class",
    "decimal.",
    "ConversionSyntax",
    "InvalidOperation",
    "is not a valid",
    "KeyError",
)


def _seed(conn: sqlite3.Connection) -> None:
    """Accounts plus a funded TWD pool.

    The door binds the REAL pool probe (the unit tests inject an unlimited one), so an
    unfunded 換匯 is refused by ``fx_balance_issues`` — a correct, Chinese, and completely
    different finding that would mask the clean row this file needs as its control.
    """
    seed_accounts(conn)
    insert_cash_movement(conn, account_id="schwab", move_date=date(2026, 1, 1),
                         kind="DEPOSIT", ccy=Currency.TWD, amount=Decimal("9000000"))
    conn.commit()


def _preview(client: TestClient, kind: str, csv_text: str) -> Any:
    return client.post("/api/import/preview", json={"kind": kind, "csv_text": csv_text})


def _assert_zh(reason: str) -> None:
    assert _CJK.search(reason), reason
    assert not any(leak in reason for leak in _LEAKS), reason


# --- one broken cell, one broken row --------------------------------------------------

#: (the broken row, the text the message must echo, the marker that identifies the finding)
_CELL_CASES: list[tuple[str, str, str]] = [
    ("schwab,2026-02-05,GBP,100000,USD,3000\n", "GBP", "無法辨識"),
    ("schwab,2026-02-05,TWD,100000,JPY,3000\n", "JPY", "無法辨識"),
    ("schwab,2026-02-05,TWD,abc,USD,3000\n", "abc", "格式不正確"),
    ("schwab,2026-02-05,TWD,100000,USD,abc\n", "abc", "格式不正確"),
    ('schwab,2026-02-05,TWD,"1,200",USD,40\n', "1,200", "格式不正確"),
    ("schwab,2026-02-05,TWD,,USD,3000\n", "from_amount", "不可空白"),
    ("schwab,2026-02-05,TWD,100000,USD,\n", "to_amount", "不可空白"),
    ("schwab,2026-02-05,,100000,USD,3000\n", "from_ccy", "不可空白"),
]


@pytest.mark.parametrize(("row", "echo", "marker"), _CELL_CASES)
def test_a_broken_cell_previews_with_a_chinese_reason_and_costs_one_row(
    dashboard_client_factory: DashboardClientFactory, row: str, echo: str, marker: str
) -> None:
    client = dashboard_client_factory(_seed)
    response = _preview(client, "fx", _FX_HEADER + _GOOD + row)
    assert response.status_code == 200, response.text
    body = response.json()
    good, bad = body["rows"]
    assert good["status"] == "ok", good
    assert good["reason"] is None, good
    assert bad["status"] == "error", bad
    reason = bad["reason"]
    _assert_zh(reason)
    assert marker in reason, reason
    assert echo in reason, reason
    assert body["summary"] == {"total": 2, "ok": 1, "warn": 0, "error": 1}, body["summary"]


@pytest.mark.parametrize("column", ["account", "from_ccy", "from_amount", "to_amount"])
def test_a_missing_required_column_names_the_column_at_the_door(
    dashboard_client_factory: DashboardClientFactory, column: str
) -> None:
    """A missing header is a FILE-level mistake, so there is no clean sibling to keep — both
    rows are rejected, and each says which column the file is missing rather than echoing
    ``KeyError``'s bare quoted word."""
    client = dashboard_client_factory(_seed)
    columns = [c for c in ("account", "date", "from_ccy", "from_amount", "to_ccy",
                           "to_amount") if c != column]
    cells = {"account": "schwab", "date": "2026-01-05", "from_ccy": "TWD",
             "from_amount": "100000", "to_ccy": "USD", "to_amount": "3000"}
    body = ",".join(cells[c] for c in columns) + "\n"
    csv_text = ",".join(columns) + "\n" + body + body
    response = _preview(client, "fx", csv_text)
    assert response.status_code == 200, response.text
    rows = response.json()["rows"]
    assert [r["status"] for r in rows] == ["error", "error"], rows
    for r in rows:
        _assert_zh(r["reason"])
        assert r["reason"] == f"缺少必填欄位 {column}", r["reason"]


def test_no_reason_in_the_whole_response_is_ascii_only(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """Every 原因 the preview hands back must be readable by the owner, not just the one
    under test — a file is imported whole, and one English cell among four Chinese ones is
    exactly how this leak survived two waves of zh-TW work on this module."""
    client = dashboard_client_factory(_seed)
    response = _preview(client, "fx", _FX_HEADER + _GOOD + (
        "schwab,2026-02-05,GBP,100000,USD,3000\n"
        "schwab,2026-03-05,TWD,abc,USD,3000\n"
        "schwab,2026-04-05,TWD,100000,USD,\n"
        "schwab,2026-05-05,TWD,NaN,USD,3000\n"
    ))
    assert response.status_code == 200, response.text
    body = response.json()
    reasons = [r["reason"] for r in body["rows"] if r["reason"]]
    assert len(reasons) == 4, reasons
    for reason in reasons:
        _assert_zh(reason)
    assert body["summary"] == {"total": 5, "ok": 1, "warn": 0, "error": 4}, body["summary"]


def test_the_cash_door_answers_the_same_way_on_the_same_cells(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """Parity, the property this repair is actually for.

    ``cash_import`` has had typed cell readers since E-1/G-1; this door had two of the four.
    The two sentences are not byte-identical (the fx door has TWO amount legs and must name
    which one), so what is asserted is that both are Chinese, name their own column, and
    echo the same offending text.
    """
    client = dashboard_client_factory(_seed)
    fx = _preview(client, "fx", _FX_HEADER + "schwab,2026-02-05,TWD,abc,USD,3000\n")
    cash = _preview(client, "cash", _CASH_HEADER + "schwab,2026-02-05,DEPOSIT,TWD,abc,,\n")
    assert fx.status_code == cash.status_code == 200
    fx_reason = fx.json()["rows"][0]["reason"]
    cash_reason = cash.json()["rows"][0]["reason"]
    for reason, column in ((fx_reason, "from_amount"), (cash_reason, "amount")):
        _assert_zh(reason)
        assert column in reason, reason
        assert "abc" in reason, reason


# --- what must NOT change --------------------------------------------------------------


def test_a_lower_case_currency_previews_clean_at_both_bulk_doors(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """⚠ The ONE acceptance change in this repair — see the seam test for the full argument.

    The cash door already accepted ``usd`` (``cash_import._currency`` upper-cases); the fx
    door answered ``'usd' is not a valid Currency``. Both now accept it, and the payload the
    door reports back is the canonical upper-case code either way.
    """
    client = dashboard_client_factory(_seed)
    fx = _preview(client, "fx", _FX_HEADER + "schwab,2026-01-05,twd,100000,usd,3000\n")
    cash = _preview(client, "cash", _CASH_HEADER + "schwab,2026-01-05,DEPOSIT,usd,100,,\n")
    assert fx.status_code == cash.status_code == 200
    fx_row = fx.json()["rows"][0]
    assert fx_row["status"] == "ok", fx_row
    assert fx_row["data"]["from_ccy"] == "TWD" and fx_row["data"]["to_ccy"] == "USD", fx_row
    assert cash.json()["rows"][0]["status"] == "ok", cash.json()["rows"][0]


def test_an_annotated_template_header_still_resolves(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """FU-D19's header canonicalization, unchanged.

    The downloadable template ships annotated headers, and the readers below now index the
    row by SUBSCRIPT — so a canonicalization regression would turn every annotated file into
    「缺少必填欄位 …」 instead of importing it.
    """
    client = dashboard_client_factory(_seed)
    header = ("account,date（YYYY-MM-DD）,from_ccy,from_amount（換出金額）,"
              "to_ccy,to_amount\n")
    response = _preview(client, "fx", header + _GOOD)
    assert response.status_code == 200, response.text
    row = response.json()["rows"][0]
    assert row["status"] == "ok", row
    assert row["data"]["from_amount"] == "100000", row


def test_a_clean_file_still_commits(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The write path, untouched: a wording fix that quietly stopped a good file from being
    written would look identical in the 原因 column."""
    client = dashboard_client_factory(_seed)
    csv_text = _FX_HEADER + _GOOD
    assert _preview(client, "fx", csv_text).json()["rows"][0]["status"] == "ok"
    response = client.post(
        "/api/import/commit", json={"kind": "fx", "csv_text": csv_text})
    assert response.status_code == 200, response.text
    assert response.json()["written"] == 1, response.text
