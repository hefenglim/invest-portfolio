"""G-1 at the real door: ``POST /api/import/preview`` printed CPython's English date error.

The seam is pinned in ``tests/data_ingestion/test_m2_import_date_zh.py``; this file asserts
what the owner actually sees.  ``_preview_wire`` puts ``r.issues[0].message`` into the row's
``reason`` field and ``web/input.js`` renders that verbatim into the 原因 cell, so
``month must be in 1..12`` was a user-visible defect, not a style preference.

Both bulk doors are exercised — ``kind="cash"`` and ``kind="fx"`` — because both reach the
same ``date.fromisoformat`` inside the same shape of ``except … str(exc)`` arm, and a fix to
one is worth nothing if the other still answers in English for the identical cell.

⚠ **The door is not the seam.**  ``import_preview`` runs FU-D19's ``normalize_import_csv``
FIRST, which infers ONE format for the whole date column and rewrites it to ISO.  So a shape
the resolver recognises (``2026/07/01``, ``2026.07.01``, ``20260701``, ``2026年7月1日``) is a
VALID date at this door and must keep previewing clean — the English only ever surfaced for
what the resolver could not rescue, and for a column too mixed for it to infer anything.
Both halves are asserted below, because a fix to the message that quietly broke the resolver
would look identical in the 原因 column and cost the owner every Excel-formatted file.
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

_CASH_HEADER = "account,date,kind,ccy,amount,acq_home_amount,note\n"
_FX_HEADER = "account,date,from_ccy,from_amount,to_ccy,to_amount\n"
_CJK = re.compile(r"[一-鿿]")

_BAD_FORMAT = "格式不正確，須為 YYYY-MM-DD"
_BLANK = "不可空白"

#: Shapes FU-D19's resolver cannot rescue, so they reach the builder verbatim and this is the
#: message the owner reads.  ``2026-13-01`` is ISO syntax with an impossible month (the case
#: whose English — ``month must be in 1..12`` — did not even contain the offending text);
#: ``01-07-2026`` is year-last with DASHES, which no format id matches; ``07/01/2026`` is the
#: genuinely AMBIGUOUS M/D-vs-D/M pair, where the door offers a chooser and, until it is
#: answered, still has to explain the unresolved cell.
_DOOR_CASES: list[tuple[str, str]] = [
    ("2026-13-01", _BAD_FORMAT),
    ("01-07-2026", _BAD_FORMAT),
    ("07/01/2026", _BAD_FORMAT),
    ("", _BLANK),
]

#: The four blueprint shapes plus one ordinary ISO row, in ONE column — a realistic file, and
#: the only arrangement in which all four reach the builder.  ⚠ The ISO row is load-bearing,
#: not decoration: with the four alone the resolver sees exactly one readable value
#: (``2026/07/01``), infers ``ymd_slash`` for the column and normalises it, so that row
#: previews CLEAN.  Add a real ISO date and no single format reads the column any more, the
#: resolver declines, and the slash cell lands on ``_parse_row`` verbatim — which is the path
#: that used to answer ``Invalid isoformat string: '2026/07/01'``.
_MIXED: list[str] = ["2026-13-01", "2026/07/01", "01-07-2026", "", "2026-01-05"]

_LEAKS = (
    "month must be",
    "day must be",
    "year must be",
    "Invalid isoformat",
    "isoformat",
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


def _csv(kind: str, *dates: str) -> str:
    if kind == "cash":
        return _CASH_HEADER + "".join(f"schwab,{d},deposit,TWD,400000,,\n" for d in dates)
    return _FX_HEADER + "".join(f"schwab,{d},TWD,100000,USD,3000\n" for d in dates)


# --- the defect itself ---------------------------------------------------------------


@pytest.mark.parametrize(("text", "marker"), _DOOR_CASES)
@pytest.mark.parametrize("kind", ["cash", "fx"])
def test_a_broken_date_previews_with_a_chinese_reason(
    dashboard_client_factory: DashboardClientFactory, kind: str, text: str, marker: str
) -> None:
    client = dashboard_client_factory(_seed)
    response = _preview(client, kind, _csv(kind, text))
    assert response.status_code == 200, response.text
    row = response.json()["rows"][0]
    assert row["status"] == "error", row
    reason = row["reason"]
    assert _CJK.search(reason), reason
    assert marker in reason, reason
    assert not any(leak in reason for leak in _LEAKS), reason
    if text:
        assert f"「{text}」" in reason, reason


@pytest.mark.parametrize("kind", ["cash", "fx"])
def test_a_slash_date_in_a_mixed_column_is_explained_in_chinese(
    dashboard_client_factory: DashboardClientFactory, kind: str
) -> None:
    """``2026/07/01`` — the shape Excel writes — beside three other broken ones.

    Alone in its column FU-D19 resolves it (the test below); mixed with values no format can
    read, the resolver infers nothing and the raw cell reaches the builder.  That is the path
    that used to answer ``Invalid isoformat string: '2026/07/01'``.
    """
    client = dashboard_client_factory(_seed)
    response = _preview(client, kind, _csv(kind, *_MIXED))
    assert response.status_code == 200, response.text
    reason = response.json()["rows"][1]["reason"]
    assert _BAD_FORMAT in reason and "「2026/07/01」" in reason, reason
    assert not any(leak in reason for leak in _LEAKS), reason


@pytest.mark.parametrize("kind", ["cash", "fx"])
def test_no_reason_in_the_whole_response_is_ascii_only(
    dashboard_client_factory: DashboardClientFactory, kind: str
) -> None:
    """Every 原因 the preview hands back must be readable by the owner, not just the one
    under test — a file is imported whole, and one English cell among three Chinese ones is
    exactly how this leak survived three doors' worth of zh-TW work."""
    client = dashboard_client_factory(_seed)
    response = _preview(client, kind, _csv(kind, *_MIXED))
    assert response.status_code == 200, response.text
    reasons = [r["reason"] for r in response.json()["rows"] if r["reason"]]
    assert len(reasons) == 4, reasons
    for reason in reasons:
        assert _CJK.search(reason), reason


def test_both_bulk_doors_say_the_same_thing_about_the_same_date(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """Parity at the wire, in the same shape wave 1 pinned for the non-finite cells."""
    client = dashboard_client_factory(_seed)
    cash = _preview(client, "cash", _csv("cash", *_MIXED))
    fx = _preview(client, "fx", _csv("fx", *_MIXED))
    assert cash.status_code == fx.status_code == 200
    cash_reasons = [r["reason"] for r in cash.json()["rows"]]
    fx_reasons = [r["reason"] for r in fx.json()["rows"]]
    assert len(cash_reasons) == len(_MIXED)
    assert cash_reasons[-1] is None, cash_reasons  # the clean ISO sibling
    assert cash_reasons == fx_reasons, (cash_reasons, fx_reasons)


# --- what must NOT change ------------------------------------------------------------


@pytest.mark.parametrize("text", ["2026/07/01", "2026.07.01", "20260701", "2026年7月1日"])
@pytest.mark.parametrize("kind", ["cash", "fx"])
def test_a_resolvable_shape_is_normalised_by_the_door_not_rejected_by_it(
    dashboard_client_factory: DashboardClientFactory, kind: str, text: str
) -> None:
    """FU-D19, unchanged.

    Excel silently reformats an ISO date the moment a template is opened, so these four are
    the common case, not an edge one.  A "stricter date reader" that rejected them would read
    as a nicer error message and cost the owner every file they actually have.
    """
    client = dashboard_client_factory(_seed)
    response = _preview(client, kind, _csv(kind, text))
    assert response.status_code == 200, response.text
    row = response.json()["rows"][0]
    assert row["status"] == "ok", row
    assert row["reason"] is None, row
    assert row["data"]["date"] == "2026-07-01", row


@pytest.mark.parametrize("kind", ["cash", "fx"])
def test_a_clean_row_survives_its_broken_siblings(
    dashboard_client_factory: DashboardClientFactory, kind: str
) -> None:
    """Unchanged degradation — pinned so the wording fix cannot cost a row.

    Expected to pass BEFORE the fix as well: the 200, the per-row rejection and the surviving
    sibling were always right; only the sentence in the rejected rows was wrong.
    """
    client = dashboard_client_factory(_seed)
    response = _preview(client, kind, _csv(kind, *_MIXED))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["summary"] == {"total": 5, "ok": 1, "warn": 0, "error": 4}, body["summary"]
    assert [r["status"] for r in body["rows"]] == ["error"] * 4 + ["ok"], body["rows"]
