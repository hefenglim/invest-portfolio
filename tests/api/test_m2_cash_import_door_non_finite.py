"""E-1 at the real door: ``POST /api/import/preview`` kind="cash" printed pydantic English.

The unit-level seam is pinned in ``tests/data_ingestion/test_m2_cash_import_non_finite.py``;
this file asserts what the owner actually sees — ``row.reason`` is rendered verbatim into the
import preview's 原因 cell (``web/input.js``), so a raw ``ValidationError`` report is a
user-visible defect, not a style preference.

The **status** was already right (200, one row rejected, the sibling kept), which is why this
door was the reference case when the fx door was fixed for QA-07. The wave-1 parity test
(``tests/api/test_m2_fx_import_door_non_finite.py``) therefore asserted the status only, with
a ⚠ saying the wording was a separate defect in ``cash_import.py``. This is that defect, and
the assertion the ⚠ was standing in for.
"""

import re
import sqlite3
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from tests.conftest import DashboardClientFactory

_CASH_HEADER = "account,date,kind,ccy,amount,acq_home_amount,note\n"
_FX_HEADER = "account,date,from_ccy,from_amount,to_ccy,to_amount\n"
_CJK = re.compile(r"[一-鿿]")

#: The wording both bulk doors now share for one broken numeric cell.
_MARKER = "必須是有限數字"

#: Pydantic internals that must never reach the 原因 cell.
_LEAKS = (
    "validation error",
    "Input should be",
    "finite_number",
    "input_value",
    "CashMovementInput",
    "errors.pydantic.dev",
)


def _seed(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    conn.commit()


def _preview(client: TestClient, kind: str, csv_text: str) -> Any:
    return client.post("/api/import/preview", json={"kind": kind, "csv_text": csv_text})


@pytest.mark.parametrize("text", ["NaN", "Infinity", "-Infinity", "sNaN"])
def test_a_non_finite_cash_amount_previews_with_a_chinese_reason(
    dashboard_client_factory: DashboardClientFactory, text: str
) -> None:
    client = dashboard_client_factory(_seed)
    response = _preview(
        client, "cash", _CASH_HEADER + f"schwab,2026-01-05,deposit,USD,{text},,\n")
    assert response.status_code == 200, response.text
    row = response.json()["rows"][0]
    assert row["status"] == "error", row
    reason = row["reason"]
    assert _CJK.search(reason), reason
    assert _MARKER in reason, reason
    assert not any(leak in reason for leak in _LEAKS), reason


def test_a_non_finite_acquisition_cost_previews_the_same_way(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed)
    response = _preview(
        client, "cash", _CASH_HEADER + "schwab,2026-01-05,deposit,USD,1000,NaN,\n")
    assert response.status_code == 200, response.text
    row = response.json()["rows"][0]
    assert row["status"] == "error", row
    assert _CJK.search(row["reason"]) and _MARKER in row["reason"], row["reason"]
    assert not any(leak in row["reason"] for leak in _LEAKS), row["reason"]


def test_a_clean_movement_survives_a_broken_sibling(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """Unchanged degradation — pinned so the wording fix cannot cost a row."""
    client = dashboard_client_factory(_seed)
    response = _preview(client, "cash", _CASH_HEADER + (
        "schwab,2026-01-05,deposit,TWD,400000,,\n"
        "schwab,2026-02-05,deposit,TWD,NaN,,\n"
    ))
    assert response.status_code == 200, response.text
    body = response.json()
    good, bad = body["rows"]
    assert good["status"] in {"ok", "warn"}, good
    assert bad["status"] == "error", bad
    assert body["summary"] == {"total": 2, "ok": 1, "warn": 0, "error": 1}, body["summary"]


def test_both_bulk_doors_now_say_the_same_thing_about_the_same_cell(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """Parity, closed in the direction wave 1 left open.

    QA-07 made the fx door degrade like the cash door; this makes the cash door SPEAK like
    the fx door. One broken numeric cell, one sentence shape, whichever file it arrived in.
    """
    client = dashboard_client_factory(_seed)
    cash = _preview(
        client, "cash", _CASH_HEADER + "schwab,2026-01-05,deposit,USD,NaN,,\n")
    fx = _preview(
        client, "fx", _FX_HEADER + "schwab,2026-01-05,TWD,NaN,USD,1000\n")
    assert cash.status_code == fx.status_code == 200
    cash_reason = cash.json()["rows"][0]["reason"]
    fx_reason = fx.json()["rows"][0]["reason"]
    for reason in (cash_reason, fx_reason):
        assert _MARKER in reason, reason
        assert "「NaN」" in reason, reason
