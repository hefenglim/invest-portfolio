"""W7 — the corporate-action ledger API: the 5th tab, the form preview, and the CRUD.

The obligation this file exists for (audit F-40): before W7 nothing in production called
``validate_corporate_action`` / ``validate_corporate_action_change``, and
``insert_corporate_action`` was a plain INSERT with no coupling to any rule. Every test
below drives the HTTP surface, because "the rule exists in validate.py" was already true
and was exactly the problem.

Two structural obligations are asserted here rather than assumed:

* **F-32 — delete and update RE-VALIDATE.** Deleting one row of an N-account set leaves
  ``split_factor`` (whose dedup key is per-symbol, not per-account) applying the global
  price correction while that account's share count goes uncorrected — and the drawer
  footer prints ✓ 對帳一致 over it.
* **§5.1(c) — the price reconcile runs on SPLIT CRUD**, including the symbol-change edit,
  and a delete returns every stored close **byte-identical** (D38 invariant 3).
"""

import sqlite3
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import (
    insert_corporate_action,
    insert_transaction,
    list_corporate_actions,
    upsert_instrument,
)
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW

D = Decimal
# Strictly AFTER the golden price date (2026-06-09) and on/before its fetched_at
# (GOLDEN_NOW = 2026-06-11), so `split_factor`'s (after, through] window covers it and the
# stored close visibly moves. Outside that window the reconcile is a legitimate no-op and
# every price assertion here would pass vacuously.
SPLIT_DAY = date(2026, 6, 10)
_BASE = "/api/ledgers/corporate-actions"


def _body(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "account_id": "tw_broker", "date": SPLIT_DAY.isoformat(), "kind": "SPLIT",
        "from_symbol": "2330", "to_symbol": "2330",
        "ratio_to": "10", "ratio_from": "1",
    }
    base.update(over)
    return base


def _closes(conn: sqlite3.Connection, symbol: str) -> list[tuple[str, str]]:
    """Stored (close, split_basis) TEXT — compared as TEXT because ``Decimal('1.5') ==
    Decimal('1.50')`` is True and D38 invariant 3 is about the stored bytes."""
    return [
        (r["close"], r["split_basis"])
        for r in conn.execute(
            "SELECT close, split_basis FROM prices WHERE instrument=? ORDER BY as_of_date",
            (symbol,),
        ).fetchall()
    ]


@pytest.fixture
def dual_aapl(golden_db: sqlite3.Connection) -> sqlite3.Connection:
    """AAPL held in schwab AND moomoo_my — the two-account set E13 and F-32 need."""
    insert_transaction(golden_db, account_id="moomoo_my", symbol="AAPL", side=Side.BUY,
                       quantity=D("20"), price=D("90"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 1, 12))
    golden_db.commit()
    return golden_db


# ------------------------------------------------------------------- list (the 5th tab)


def test_list_returns_rows_with_display_fields(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    insert_corporate_action(
        golden_db, account_id="tw_broker", action_date=SPLIT_DAY,
        kind=CorporateActionKind.SPLIT, from_symbol="2330", to_symbol="2330",
        ratio_to=D("10"), ratio_from=D("1"), note="十股換一股")
    r = api_client.get(_BASE)
    assert r.status_code == 200
    (row,) = r.json()["rows"]
    assert row["symbol"] == "2330" and row["to_symbol"] == "2330"
    assert row["kind"] == "SPLIT" and row["kind_label"] == "分割"
    assert row["ratio_to"] == "10" and row["ratio_from"] == "1"
    assert row["ratio_label"] == "每 1 股 → 10 股"
    assert row["account_id"] == "tw_broker"


# ----------------------------------------------------------- create: the N-row batch


def test_create_writes_one_row_per_holding_account(
    api_client: TestClient, dual_aapl: sqlite3.Connection
) -> None:
    """D13/D28: the owner submits ONE action; E13's N rows are written for them.

    The alternative — asking the owner to submit N rows — is the partial state D13 exists
    to forbid, arriving through the door built to prevent it.
    """
    r = api_client.post(_BASE, json=_body(
        account_id="schwab", from_symbol="AAPL", to_symbol="AAPL",
        ratio_to="4", ratio_from="1"))
    assert r.status_code == 201, r.text
    assert r.json()["written"] == 2
    assert {a.account_id for a in list_corporate_actions(dual_aapl)} == {
        "schwab", "moomoo_my"}


def test_create_rejects_a_hard_issue_with_a_zh_message(api_client: TestClient) -> None:
    r = api_client.post(_BASE, json=_body(ratio_to="0.2857"))
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "validation_error"
    assert err["issues"][0]["code"] == "ratio_not_positive_integer"
    assert "正整數" in err["issues"][0]["text"]
    assert list_corporate_actions_via(api_client) == 0


def list_corporate_actions_via(client: TestClient) -> int:
    return int(client.get(_BASE).json()["total_count"])


def test_create_needs_the_warning_acknowledged(api_client: TestClient) -> None:
    """A soft finding blocks until acked, then commits — the 賣超 tier, unchanged."""
    r = api_client.post(_BASE, json=_body(ratio_to="1", ratio_from="1"))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "warnings_unacknowledged"
    assert list_corporate_actions_via(api_client) == 0
    ok = api_client.post(_BASE, json=_body(ratio_to="1", ratio_from="1",
                                           ack_warnings=True))
    assert ok.status_code == 201
    assert list_corporate_actions_via(api_client) == 1


# ------------------------------------------------------- §5.1(c): the price reconcile


def test_create_split_restates_the_stored_closes(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    before = _closes(golden_db, "2330")
    assert before == [("600", "1")]
    assert api_client.post(_BASE, json=_body()).status_code == 201
    assert _closes(golden_db, "2330") == [("6000", "10")]


def test_delete_restores_every_close_byte_identically(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """D38 invariant 3 — ``prices`` is the only thing this feature writes outside the
    ledgers, so 重算 does not cover it and the reconcile must be reversible."""
    before = _closes(golden_db, "2330")
    api_client.post(_BASE, json=_body())
    assert _closes(golden_db, "2330") != before
    (action_id,) = [a.id for a in list_corporate_actions(golden_db)]
    assert api_client.delete(f"{_BASE}/{action_id}").status_code == 200
    assert _closes(golden_db, "2330") == before


def test_edit_that_moves_the_symbol_reconciles_BOTH_ends(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """The old symbol keeps a basis from an action that no longer references it, unless
    the edit passes both ends to the reconcile."""
    upsert_instrument(golden_db, Instrument(
        symbol="2454", market=Market.TW, quote_ccy=Currency.TWD, sector="Semis",
        name="MediaTek", board="TWSE"))
    insert_transaction(golden_db, account_id="tw_broker", symbol="2454", side=Side.BUY,
                       quantity=D("1000"), price=D("900"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 1, 5))
    upsert_prices(golden_db, [PriceRow(
        instrument="2454", market=Market.TW, as_of=date(2026, 6, 9),
        close=D("900"), source="test")], fetched_at=GOLDEN_NOW)
    golden_db.commit()
    api_client.post(_BASE, json=_body())
    assert _closes(golden_db, "2330") == [("6000", "10")]
    (action_id,) = [a.id for a in list_corporate_actions(golden_db)]
    r = api_client.put(f"{_BASE}/{action_id}", json=_body(
        from_symbol="2454", to_symbol="2454"))
    assert r.status_code == 200, r.text
    # BOTH ends restated: the abandoned symbol back to its raw close, the new one scaled.
    assert _closes(golden_db, "2330") == [("600", "1")]
    assert _closes(golden_db, "2454") == [("9000", "10")]


# --------------------------------------------------------- F-32: delete/update revalidate


def test_deleting_one_row_of_a_two_account_set_is_refused(
    api_client: TestClient, dual_aapl: sqlite3.Connection
) -> None:
    """F-32, the headline. Deleting one row leaves the GLOBAL price correction standing
    while that account's shares go uncorrected — and the drawer prints ✓ 對帳一致."""
    api_client.post(_BASE, json=_body(
        account_id="schwab", from_symbol="AAPL", to_symbol="AAPL",
        ratio_to="4", ratio_from="1"))
    ids = [a.id for a in list_corporate_actions(dual_aapl)]
    assert len(ids) == 2
    r = api_client.delete(f"{_BASE}/{ids[0]}")
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "partial_action_set_change"
    assert "整組一起處理" in err["message"]
    assert len(list_corporate_actions(dual_aapl)) == 2   # nothing was removed


def test_deleting_the_whole_set_is_allowed(
    api_client: TestClient, dual_aapl: sqlite3.Connection
) -> None:
    api_client.post(_BASE, json=_body(
        account_id="schwab", from_symbol="AAPL", to_symbol="AAPL",
        ratio_to="4", ratio_from="1"))
    r = api_client.delete(f"{_BASE}/set", params={
        "from_symbol": "AAPL", "date": SPLIT_DAY.isoformat(), "kind": "SPLIT"})
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == 2
    assert list_corporate_actions(dual_aapl) == []


def test_editing_one_rows_ratio_out_of_step_is_refused(
    api_client: TestClient, dual_aapl: sqlite3.Connection
) -> None:
    """Staying in the set means keeping its ratio — F-06's conflict from the inside."""
    api_client.post(_BASE, json=_body(
        account_id="schwab", from_symbol="AAPL", to_symbol="AAPL",
        ratio_to="4", ratio_from="1"))
    ids = [a.id for a in list_corporate_actions(dual_aapl)]
    r = api_client.put(f"{_BASE}/{ids[0]}", json=_body(
        account_id="schwab", from_symbol="AAPL", to_symbol="AAPL",
        ratio_to="2", ratio_from="1"))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "conflicting_ratio"
    stored = {(a.ratio_to, a.ratio_from) for a in list_corporate_actions(dual_aapl)}
    assert stored == {(D("4"), D("1"))}


def test_editing_a_row_onto_a_multi_account_symbol_is_refused(
    api_client: TestClient, dual_aapl: sqlite3.Connection
) -> None:
    """E13 on the UPDATE path — the question the change guard never asks.

    ``validate_corporate_action_change`` protects an existing SET; a single-row action
    edited onto a DIFFERENT ``from_symbol`` belongs to no set, so nothing there fires —
    and the stored row still carries the OLD symbol, so it cannot answer the all-accounts
    question about the new one either. Without E13 kept on this path, the symbol-change
    edit is an unguarded door onto exactly the partial state D13 forbids.
    """
    api_client.post(_BASE, json=_body())          # 2330, tw_broker only -> one row
    (action_id,) = [a.id for a in list_corporate_actions(dual_aapl)]
    r = api_client.put(f"{_BASE}/{action_id}", json=_body(
        account_id="schwab", from_symbol="AAPL", to_symbol="AAPL"))
    assert r.status_code == 400
    assert r.json()["error"]["issues"][0]["code"] == "incomplete_account_coverage"
    assert [a.from_symbol for a in list_corporate_actions(dual_aapl)] == ["2330"]


def test_restating_a_ratio_in_the_same_terms_is_allowed(
    api_client: TestClient, dual_aapl: sqlite3.Connection
) -> None:
    """「4 比 1」 re-entered as 「40 比 10」 is the SAME ratio, not a conflict."""
    api_client.post(_BASE, json=_body(
        account_id="schwab", from_symbol="AAPL", to_symbol="AAPL",
        ratio_to="4", ratio_from="1"))
    ids = [a.id for a in list_corporate_actions(dual_aapl)]
    r = api_client.put(f"{_BASE}/{ids[0]}", json=_body(
        account_id="schwab", from_symbol="AAPL", to_symbol="AAPL",
        ratio_to="40", ratio_from="10"))
    assert r.status_code == 200, r.text


# --------------------------------------------------------------- the always-on preview


def test_preview_shows_before_after_and_states_cost_unchanged(
    api_client: TestClient
) -> None:
    r = api_client.post(f"{_BASE}/preview", json=_body())
    assert r.status_code == 200, r.text
    body = r.json()
    (acct,) = body["accounts"]
    assert acct["account_id"] == "tw_broker"
    (before,) = acct["before"]
    (after,) = acct["after"]
    assert (before["shares"], before["cost_total"]) == ("1000", "500000")
    assert (after["shares"], after["cost_total"]) == ("10000", "500000")
    assert after["avg"] == "50"          # 500 -> 50, the average correcting itself
    assert body["conserved"] is True
    assert body["cost_before_total"] == body["cost_after_total"] == "500000"
    assert body["rows_to_write"] == 1
    assert body["blocking"] is False


def test_preview_expands_to_every_affected_account(
    api_client: TestClient, dual_aapl: sqlite3.Connection
) -> None:
    """D28: the owner arrives to fix ONE account and E13 writes N rows — never a surprise.

    The account list is derived server-side and is read-only by construction: the request
    carries one account and the response names all of them.
    """
    r = api_client.post(f"{_BASE}/preview", json=_body(
        account_id="schwab", from_symbol="AAPL", to_symbol="AAPL",
        ratio_to="4", ratio_from="1"))
    body = r.json()
    assert {a["account_id"] for a in body["accounts"]} == {"schwab", "moomoo_my"}
    assert body["rows_to_write"] == 2
    assert body["conserved"] is True


def test_preview_names_the_account_the_action_does_not_reach(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """A position opened AFTER the action date is listed by name as 不受影響 (§6.7).

    Showing the untouched account is how the owner can tell the system understood their
    ledger rather than merely applied a rule.
    """
    insert_transaction(golden_db, account_id="moomoo_my", symbol="AAPL", side=Side.BUY,
                       quantity=D("20"), price=D("130"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 6, 11))     # AFTER SPLIT_DAY
    golden_db.commit()
    body = api_client.post(f"{_BASE}/preview", json=_body(
        account_id="schwab", from_symbol="AAPL", to_symbol="AAPL",
        ratio_to="4", ratio_from="1")).json()
    assert [a["account_id"] for a in body["accounts"]] == ["schwab"]
    assert [a["account_id"] for a in body["not_affected"]] == ["moomoo_my"]
    assert body["rows_to_write"] == 1


def test_preview_says_when_the_action_unblocks_a_failing_sell(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """§6.7's ✓ line — said BEFORE saving, at the moment the owner can still check it."""
    insert_transaction(golden_db, account_id="tw_broker", symbol="2330", side=Side.SELL,
                       quantity=D("5000"), price=D("60"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 6, 12))
    golden_db.commit()
    body = api_client.post(f"{_BASE}/preview", json=_body()).json()
    (unblocked,) = body["unblocks"]
    assert unblocked["symbol"] == "2330"
    assert unblocked["shares"] == "5000"
    assert unblocked["date"] == "2026-06-12"


def test_preview_reports_a_spinoffs_two_rows(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    upsert_instrument(golden_db, Instrument(
        symbol="2330B", market=Market.TW, quote_ccy=Currency.TWD, sector="Semis",
        name="TSMC Spin", board="TWSE"))
    golden_db.commit()
    body = api_client.post(f"{_BASE}/preview", json=_body(
        kind="SPINOFF", to_symbol="2330B", ratio_to="1", ratio_from="2",
        cost_carry="0.25")).json()
    (acct,) = body["accounts"]
    assert [a["symbol"] for a in acct["after"]] == ["2330", "2330B"]
    assert acct["after"][1]["shares"] == "500"
    assert body["conserved"] is True     # 成本合計不變 across parent + child


def test_preview_reports_the_cash_in_lieu_fraction(
    api_client: TestClient
) -> None:
    """§3.2 / follow-up 1: a fractional result is surfaced so the owner can book it."""
    body = api_client.post(f"{_BASE}/preview", json=_body(
        ratio_to="1", ratio_from="3")).json()
    assert body["fractions"], body
    assert body["fractions"][0]["symbol"] == "2330"


# ------------------------------------------------------------- the CSV import surface


def test_the_template_endpoint_serves_the_new_kind(api_client: TestClient) -> None:
    r = api_client.get("/api/import/template", params={"kind": "corporate_actions"})
    assert r.status_code == 200
    header = r.content.decode("utf-8").lstrip("﻿").split("\r\n")[0]
    assert header.split(",")[:5] == [
        "account", "date(YYYY-MM-DD)", "kind", "from_symbol", "to_symbol"]
    assert "import_template_corporate_actions.csv" in r.headers["content-disposition"]


def test_the_import_route_rejects_a_partial_multi_account_batch(
    api_client: TestClient, dual_aapl: sqlite3.Connection
) -> None:
    """E13 through the CSV door — the one path where a genuinely partial batch can arrive
    (the form builds the complete set for the owner)."""
    csv_text = ("account,date,kind,from_symbol,to_symbol,ratio_to,ratio_from\n"
                f"schwab,{SPLIT_DAY.isoformat()},SPLIT,AAPL,AAPL,4,1\n")
    r = api_client.post("/api/import/preview",
                        json={"kind": "corporate_actions", "csv_text": csv_text})
    assert r.status_code == 200
    (row,) = r.json()["rows"]
    assert row["status"] == "error"
    assert "moomoo_my" in row["reason"]


def test_the_import_route_commits_a_complete_batch_and_reconciles(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    csv_text = ("account,date,kind,from_symbol,to_symbol,ratio_to,ratio_from\n"
                f"tw_broker,{SPLIT_DAY.isoformat()},SPLIT,2330,2330,10,1\n")
    r = api_client.post("/api/import/commit",
                        json={"kind": "corporate_actions", "csv_text": csv_text})
    assert r.status_code == 200, r.text
    assert r.json()["written"] == 1
    assert r.json()["prices_restated"] == 1
    assert _closes(golden_db, "2330") == [("6000", "10")]
