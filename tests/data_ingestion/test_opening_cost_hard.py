"""F-13 (D37): an opening's ``original_cost_total`` must be > 0, HARD, at every write door.

The trap §10.5 states plainly: ``opening_import.py`` validated only ``shares > 0``, so an
opening with a cost total of **0** imported cleanly and permanently zeroed the position's
basis **with no 待釐清 flag** — strictly worse than the oversell it appears to fix, because
the oversell at least announces itself. The shortcut is attractive precisely when D37's
owner-supplied cost totals are hard to find, which is the moment it must not be available.

It is an asymmetry repair, not a new mechanism: the same file already hard-validates the
share count, and the correction route already refused a NEGATIVE total. Zero sat in the gap
between them.

Both doors are covered here, because a rule enforced at one of several write doors is how
E13 came to be insert-only (see ``validate_corporate_action_change``):

* the CSV importer — which is also the single-row manual 期初 form, since ``web/input.js``
  builds a one-row CSV and posts it to ``/api/import/commit``;
* ``PUT /api/ledgers/openings/{account}/{symbol}`` — the correction door.

Both derivation paths are covered too. A legacy CSV supplying only ``original_avg_cost`` has
its total derived as ``avg × shares``, so ``avg = 0`` produces a zero total without ever
naming one — the check therefore sits AFTER the total is resolved, not next to the parse.
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.opening_import import (
    build_opening_preview,
    write_opening_row,
)
from portfolio_dash.data_ingestion.preview import ImportPreview, commit_preview
from portfolio_dash.data_ingestion.store import list_opening, upsert_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument

_HEADER = "account,symbol,shares,original_cost_total,build_date\n"
_LEGACY_HEADER = "account,symbol,shares,original_cost_total,build_date,original_avg_cost\n"


def _setup(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(
        conn,
        Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                   sector="Tech", name="台積電"),
    )


def _kinds(preview: ImportPreview) -> set[str]:
    return {i.kind for row in preview.rows for i in row.issues}


@pytest.mark.parametrize("total", ["0", "0.00", "-1", "-250000"])
def test_csv_non_positive_total_is_hard(conn: sqlite3.Connection, total: str) -> None:
    """A zero or negative total is a HARD issue and the row is never written."""
    _setup(conn)
    preview = build_opening_preview(conn, _HEADER + f"tw_broker,2330,1000,{total},2025-01-01\n")
    assert "non_positive_opening_cost" in _kinds(preview)
    assert preview.rows[0].has_hard_issue
    summary = commit_preview(conn, preview, accept={0}, writer=write_opening_row)
    assert summary.written == [] and summary.skipped == [0]
    assert list_opening(conn) == []


def test_csv_zero_legacy_avg_derives_zero_total_and_is_hard(conn: sqlite3.Connection) -> None:
    """The derivation path: only ``original_avg_cost`` supplied, and it is 0.

    ``avg × shares`` is 0, so the row would have imported with nothing but the SOFT
    ``opening_total_derived`` notice — a zero basis let in by a column that never mentions a
    total. The check runs after the total is resolved, so it catches this too.
    """
    _setup(conn)
    preview = build_opening_preview(
        conn, _LEGACY_HEADER + "tw_broker,2330,1000,,2025-01-01,0\n")
    assert _kinds(preview) >= {"opening_total_derived", "non_positive_opening_cost"}
    assert preview.rows[0].has_hard_issue


def test_csv_message_says_what_to_do(conn: sqlite3.Connection) -> None:
    """The message names the consequence and the next action, not just the rule.

    'must be > 0' tells the owner they are blocked; it does not tell them that a 0 they were
    about to type would have been silently permanent, which is the whole reason this is hard.
    """
    _setup(conn)
    preview = build_opening_preview(conn, _HEADER + "tw_broker,2330,1000,0,2025-01-01\n")
    message = next(i.message for row in preview.rows for i in row.issues
                   if i.kind == "non_positive_opening_cost")
    assert "原始總成本" in message
    assert "0" in message
    assert "對帳單" in message  # the next action: go and find the real figure


def test_csv_positive_total_unchanged(conn: sqlite3.Connection) -> None:
    """The pre-existing happy path is untouched — no new issue, still writes."""
    _setup(conn)
    preview = build_opening_preview(conn, _HEADER + "tw_broker,2330,1000,500000,2025-01-01\n")
    assert preview.rows[0].issues == []
    summary = commit_preview(conn, preview, accept={0}, writer=write_opening_row)
    assert len(summary.written) == 1


# --- door 2: the correction route --------------------------------------------------------


def _seed_one(api_client: TestClient) -> None:
    """Seed a valid opening through the real import seam (the only creation door)."""
    response = api_client.post(
        "/api/import/commit",
        json={"kind": "openings", "ack_warnings": True,
              "csv_text": _HEADER + "tw_broker,2330,500,225000,2026-01-02\n"},
    )
    assert response.status_code == 200, response.text


def _stored_total(api_client: TestClient) -> str:
    rows = api_client.get("/api/ledgers/openings", params={"limit": 500}).json()["rows"]
    row = next(r for r in rows if r["account_id"] == "tw_broker" and r["symbol"] == "2330")
    total: str = row["total"]
    return total


@pytest.mark.parametrize("body", [
    {"shares": "500", "total": "0", "date": "2026-01-02"},
    {"shares": "500", "avg": "0", "date": "2026-01-02"},        # the legacy derivation
])
def test_edit_route_rejects_non_positive_total(
    api_client: TestClient, body: dict[str, str]
) -> None:
    """The correction door refuses a zero total and leaves the stored row alone.

    It already refused a NEGATIVE one, which is what made zero look deliberate rather than
    missed — an edit to 0 is exactly the shortcut D37 forbids, arriving through the door the
    owner is most likely to use when the real figure cannot be found.
    """
    _seed_one(api_client)
    response = api_client.put("/api/ledgers/openings/tw_broker/2330", json=body)
    assert response.status_code == 400
    assert "原始總成本" in response.json()["error"]["message"]
    assert _stored_total(api_client) == "225000"
