"""QA-07 at the real door: ``POST /api/import/preview`` kind="fx" answered HTTP 500.

The unit-level seam is pinned in ``tests/data_ingestion/test_m2_fx_import_non_finite.py``;
this file asserts the property the owner actually experiences — a preview is a PER-ROW
verdict, so one unparseable cell must cost exactly one row.

The cash door is asserted beside it on the identical input, because parity between the two
bulk doors is the property (``architecture.md``'s C3 seam: "the bulk door ships a weaker
guard than the single-row form" is the failure being prevented) — not the message text of
either one alone. ``cash`` already degraded cleanly; ``fx`` did not.
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


def _seed(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    insert_cash_movement(conn, account_id="schwab", move_date=date(2026, 1, 1),
                         kind="DEPOSIT", ccy=Currency.TWD, amount=Decimal("400000"))
    conn.commit()


def _preview(client: TestClient, kind: str, csv_text: str) -> Any:
    return client.post("/api/import/preview", json={"kind": kind, "csv_text": csv_text})


@pytest.mark.parametrize("text", ["NaN", "Infinity"])
def test_a_single_non_finite_fx_row_previews_as_an_error_not_a_500(
    dashboard_client_factory: DashboardClientFactory, text: str
) -> None:
    client = dashboard_client_factory(_seed)
    response = _preview(client, "fx", _FX_HEADER + f"schwab,2026-01-05,TWD,{text},USD,1000\n")
    assert response.status_code == 200, response.text
    row = response.json()["rows"][0]
    assert row["status"] == "error", row
    assert _CJK.search(row["reason"]), row["reason"]
    # Not the overdraft sentence: an unreadable cell is a broken cell, not a poor pool.
    assert "可用餘額" not in row["reason"]


def test_a_clean_conversion_survives_a_broken_sibling(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed)
    response = _preview(client, "fx", _FX_HEADER + (
        "schwab,2026-01-05,TWD,320000,USD,10000\n"
        "schwab,2026-02-05,TWD,NaN,USD,1000\n"
    ))
    assert response.status_code == 200, response.text
    body = response.json()
    good, bad = body["rows"]
    assert good["status"] in {"ok", "warn"}, good
    assert bad["status"] == "error", bad
    assert body["summary"] == {"total": 2, "ok": 1, "warn": 0, "error": 1}, body["summary"]


@pytest.mark.parametrize("text", ["NaN", "Infinity"])
def test_the_cash_door_answers_the_same_way_on_the_same_input(
    dashboard_client_factory: DashboardClientFactory, text: str
) -> None:
    """Parity: the door that already degraded cleanly is the reference, not the exception.

    ⚠ This asserted the STATUS only when it was written, because the cash door reached its
    verdict by letting pydantic reject the ``CashMovementInput`` and putting ``str(exc)`` in
    the issue — so its ``reason`` was the raw English 「Input should be a finite number …」,
    rendered verbatim in the owner's preview grid. That separate defect was outside wave 1's
    allowlist; it is fixed (E-1, ``cash_import._finite``), so the reason is asserted here too
    and the two doors are now compared on wording as well as on degradation.
    ``tests/data_ingestion/test_m2_cash_import_non_finite.py`` owns the cash-side detail.
    """
    client = dashboard_client_factory(_seed)
    response = _preview(client, "cash", _CASH_HEADER + f"schwab,2026-01-05,deposit,USD,{text},,\n")
    assert response.status_code == 200, response.text
    row = response.json()["rows"][0]
    assert row["status"] == "error", row
    assert _CJK.search(row["reason"]), row["reason"]
    assert "必須是有限數字" in row["reason"], row["reason"]
