"""``POST /api/broker/convert`` — the web door to the broker converter (C5).

The offline CLI already has 31 tests pinning its output byte-for-byte. This file asserts the
things that are true of the ENDPOINT and not of the script:

* it converts the same corpus to the same rows (one conversion, two callers — a second
  implementation of "how a broker event becomes a ledger row" would disagree about money);
* a **blocking** reconcile issue withholds every CSV, so the refusal cannot be clicked past;
* an unmapped ``(action, description)`` pair is a 422 the owner can act on, not a 500;
* and the CSVs it returns re-parse through the **production** import builders — because the
  endpoint's whole design is that it writes nothing itself and hands the rows back to the
  ordinary preview/commit path.

Read-only throughout: nothing here should touch a ledger row, and
``test_converting_writes_nothing_to_the_ledger`` says so out loud.
"""

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.api.routers.broker_import import COMMIT_ORDER
from portfolio_dash.data_ingestion.csv_import import normalize_import_csv
from portfolio_dash.data_ingestion.import_templates import DATE_COLUMN_BY_KIND

_CORPUS = Path(__file__).resolve().parents[1] / "golden" / "broker"


def _files() -> list[dict[str, str]]:
    return [
        {"name": n, "text": (_CORPUS / n).read_text(encoding="utf-8")}
        for n in ("schwab_2024.csv", "schwab_2025.csv")
    ]


def _convert(client: TestClient, **over: Any) -> Any:
    body: dict[str, Any] = {"account": "schwab", "broker": "schwab",
                            "currency": "USD", "exports": _files()}
    body.update(over)
    return client.post("/api/broker/convert", json=body)


# --- the happy path -------------------------------------------------------------------


def test_a_clean_export_converts_and_returns_its_csvs(api_client: TestClient) -> None:
    r = _convert(api_client)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["blocking"] == []
    assert body["rows_in"] > 0
    # Every returned file is a kind the importer knows, and they arrive in dependency order.
    assert list(body["files"]) == body["commit_order"]
    assert set(body["files"]) <= set(COMMIT_ORDER)
    assert "transactions" in body["files"]


def test_the_returned_csvs_reparse_through_the_PRODUCTION_builders(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """The endpoint's contract is 'these are ordinary import CSVs'. Asserting it against the
    real builders is the only way to know that is still true — a converter checked against a
    parser written for it proves the two agree, not that either is right."""
    from portfolio_dash.api.routers.input_center import _BUILDERS

    files = _convert(api_client).json()["files"]
    for kind, text in files.items():
        norm = normalize_import_csv(text, DATE_COLUMN_BY_KIND[kind])
        preview = _BUILDERS[kind](golden_db, norm.text)
        parse_errors = [
            (r.index, i.message)
            for r in preview.rows for i in r.issues if i.kind == "parse_error"
        ]
        assert parse_errors == [], f"{kind}: {parse_errors}"


def test_converting_writes_nothing_to_the_ledger(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """Conversion is a pure read. If this ever fails, there are two write paths into the
    ledger and only one of them has the oversell guard, the fee snapshot and the batch."""
    def counts() -> tuple[int, ...]:
        return tuple(
            int(golden_db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
            for t in ("transactions", "dividends", "cash_movements",
                      "corporate_actions", "opening_inventory", "import_batches")
        )

    before = counts()
    assert _convert(api_client).status_code == 200
    assert counts() == before


def test_the_money_totals_are_STRINGS(api_client: TestClient) -> None:
    """Decimal over the wire, never a JSON number (CLAUDE.md invariant 3 / the front-back
    contract). A float here would be the one place in this feature money loses precision."""
    body = _convert(api_client).json()
    assert isinstance(body["cash_total"], str)
    for o in body["openings_needing_cost"]:
        assert isinstance(o["shares"], str)


# --- the worksheets are STRUCTURE, not a spreadsheet to open --------------------------


def test_actions_the_file_cannot_determine_come_back_as_fields_to_fill(
    api_client: TestClient,
) -> None:
    """A one-leg split states its share delta and never its ratio, so the converter leaves
    both terms blank rather than inventing one (D14). The endpoint returns the gap as
    ``ratio_to``/``ratio_from`` = null plus a reason, so the page can render two integer
    boxes beside it — the CLI's answer was a CSV to open in Excel."""
    body = _convert(api_client).json()
    pending = body["actions_needing_input"]
    assert pending, "the corpus is supposed to contain a one-leg split"
    for p in pending:
        assert p["ratio_to"] is None or p["ratio_from"] is None
        assert p["needs"]
        assert p["date"] and p["kind"] and p["to_symbol"]
    # …and the worksheets are NOT offered as uploadable files.
    assert "_actions_worksheet" not in body.get("files", {})
    assert "_openings_worksheet" not in body.get("files", {})


def test_the_worksheet_headers_come_from_the_TEMPLATE_not_from_a_copy(
    api_client: TestClient,
) -> None:
    """The page renders the owner's filled-in worksheet rows back into a CSV, so it needs the
    column order — and it must not keep its own copy of it. Two column lists drift, and the
    symptom is an import that rejects every row for a reason nobody can see (the
    registration-point defect ``import_templates`` names seven of)."""
    from portfolio_dash.data_ingestion.import_templates import template_columns

    headers = _convert(api_client).json()["worksheet_headers"]
    for kind in ("corporate_actions", "openings"):
        assert headers[kind] == ",".join(template_columns(kind))


# --- refusals -------------------------------------------------------------------------


def test_an_unmapped_row_is_a_422_naming_the_pair(api_client: TestClient) -> None:
    """Rule 7: no catch-all bucket. The corpus carries a deliberately unmapped pair for
    exactly this, and the answer must be an answerable refusal rather than a stack trace."""
    r = _convert(api_client, exports=[{
        "name": "schwab_unmapped.csv",
        "text": (_CORPUS / "schwab_unmapped.csv").read_text(encoding="utf-8"),
    }])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "broker_row_unmapped"


def test_an_unknown_broker_is_refused_by_NAME(api_client: TestClient) -> None:
    r = _convert(api_client, broker="definitely-not-a-broker")
    assert r.status_code == 400 and r.json()["error"]["field"] == "broker"


def test_an_unknown_account_is_refused(api_client: TestClient) -> None:
    """Checked against the accounts table, not against a list in this module — an account
    that does not exist produces rows nothing can import, and finding that out at upload
    time means re-doing the conversion."""
    r = _convert(api_client, account="nope")
    assert r.status_code == 400 and r.json()["error"]["field"] == "account"


def test_no_files_is_refused(api_client: TestClient) -> None:
    r = _convert(api_client, exports=[])
    assert r.status_code == 400 and r.json()["error"]["field"] == "exports"


def test_an_oversized_upload_is_refused_before_it_is_parsed(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bound, not a policy. Unbounded input on the 1 GB VM that serves prod is a way to
    take the site down by accident."""
    monkeypatch.setattr("portfolio_dash.api.routers.broker_import._MAX_CHARS", 10)
    r = _convert(api_client)
    assert r.status_code == 413


def test_a_blocking_issue_withholds_EVERY_csv(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ The all-or-nothing gate, driven through the endpoint.

    Forced rather than fixtured: the corpus is clean by design (it is the regression corpus),
    and a gate nobody has watched refuse is a gate nobody knows works. Returning the files
    with an ``ok: false`` flag would leave the refusal one ignored checkbox from bypass.
    """
    import portfolio_dash.api.routers.broker_import as mod
    from portfolio_dash.data_ingestion.broker.reconcile import ReconcileIssue

    real = mod.convert  # type: ignore[attr-defined]

    def poisoned(events: Any, account: str, currency: str) -> Any:
        conv, grouped, report = real(events, account, currency)
        bad = ReconcileIssue(code="cash_not_conserved", severity="blocking",
                             refs=("f.csv:1",), detail="forced")
        return conv, grouped, type(report)(
            issues=(*report.issues, bad), rows_in=report.rows_in,
            cash_total=report.cash_total, share_deltas=report.share_deltas)

    monkeypatch.setattr(mod, "convert", poisoned)
    body = _convert(api_client).json()
    assert body["ok"] is False
    assert "files" not in body
    assert [i["code"] for i in body["blocking"]] == ["cash_not_conserved"]
    # The counts still come back: the owner needs to see what WOULD have been written in
    # order to judge whether the complaint is about the file or about this code.
    assert body["counts"]["transactions"] > 0


# --- DEF-029: the statement's broker must be the account's broker ------------------------


@pytest.mark.parametrize("account", ["tw_broker", "moomoo_my"])
def test_a_statement_aimed_at_another_brokers_account_is_a_BLOCKING_refusal(
    api_client: TestClient, account: str
) -> None:
    """Measured 2026-09-23: a Schwab export converted 「into」 the Moomoo account read
    「對帳通過」 and wrote a real AAPL buy under the wrong broker; into the TW account it
    reached the market-rule refusal only at commit, after the verdict had said 通過. The
    pairing is refused BEFORE a row is parsed, in the all-or-nothing shape the page already
    renders — no files, no rows, no counts."""
    body = _convert(api_client, account=account).json()
    assert body["ok"] is False
    assert "files" not in body and body["rows"] == {} and body["counts"] == {}
    [issue] = body["blocking"]
    assert issue["code"] == "account_broker_mismatch" and issue["severity"] == "blocking"
    assert issue["account_id"] == account and issue["broker_id"] == "schwab"
    # The account is named by its MARKER (resolved to the zh name by the frontend), never
    # by the English accounts.name / accounts.broker column.
    assert f"{{account:{account}}}" in issue["detail"]
    for english in ("TW Broker", "Moomoo MY", "Charles Schwab"):
        assert english not in issue["detail"]


def test_the_matching_account_converts(api_client: TestClient) -> None:
    body = _convert(api_client, account="schwab").json()
    assert body["ok"] is True and body["blocking"] == []


def test_the_adapter_list_names_the_accounts_each_broker_serves(api_client: TestClient) -> None:
    """The page filters its account picker from THIS, so it holds no broker→account table
    of its own and cannot disagree with the server's refusal."""
    body = api_client.get("/api/broker/adapters").json()
    assert body["brokers"] == ["schwab"]
    assert body["accounts_by_broker"] == {"schwab": ["schwab"]}


# --- DEF-028: every row, and every row that went elsewhere -------------------------------


def test_every_converted_row_is_listed_and_its_cells_render_the_file_byte_for_byte(
    api_client: TestClient,
) -> None:
    """``rows[kind]`` is the file as structure. Rendering the cells through the template
    header must reproduce ``files[kind]`` exactly — one list, two views — so a page that
    commits a ticked subset by index is looking at the same rows the server will parse."""
    from portfolio_dash.data_ingestion.broker.convert import render_kind

    body = _convert(api_client).json()
    for kind, text in body["files"].items():
        rows = body["rows"][kind]
        assert len(rows) == body["counts"][kind] > 0
        assert [r["i"] for r in rows] == list(range(len(rows)))
        assert render_kind(kind, [r["cells"] for r in rows]) == text
        for r in rows:
            assert {"refs", "date", "type", "symbol", "shares", "price", "amount",
                    "currency", "note", "cells"} <= set(r)
            assert r["refs"] and r["date"]
            for money in ("shares", "price", "amount"):
                assert isinstance(r[money], str)          # Decimal strings, never numbers


def test_every_source_row_is_either_a_row_or_named_as_dropped_with_its_destination(
    api_client: TestClient,
) -> None:
    """15 in, 9 out, and the 6 that went elsewhere used to be a number to reconcile by
    hand. Conservation by REF: the rows plus the dropped list cover every line of both
    exports, and a dropped row that lives on inside another row says which one."""
    from portfolio_dash.data_ingestion.broker.registry import parse_export

    body = _convert(api_client).json()
    every_ref = {
        e.ref
        for f in _files()
        for e in parse_export("schwab", f["text"], source_file=f["name"])
    }
    in_rows = {ref for rows in body["rows"].values() for r in rows for ref in r["refs"]}
    dropped = body["dropped"]
    in_dropped = {ref for d in dropped for ref in d["refs"]}
    assert in_rows | in_dropped == every_ref
    # A ref in BOTH is a row that was merged into another (its `into` says where); a ref
    # that left the conversion for good is in the dropped list only.
    for d in dropped:
        overlap = set(d["refs"]) & in_rows
        assert bool(overlap) == bool(d["into"]) or d["why"] == "action_needs_input", d
    whys = {d["why"] for d in dropped}
    assert {"suppressed", "merged_dividend", "option_row_unsupported",
            "unconvertible", "action_needs_input"} <= whys
    [merged] = [d for d in dropped if d["why"] == "merged_dividend"]
    assert merged["into"] == "dividends:0" and len(merged["refs"]) == 3
    assert body["rows"]["dividends"][0]["refs"] == merged["refs"]
    [worksheet] = [d for d in dropped if d["why"] == "action_needs_input"]
    assert worksheet["into"] == "actions_needing_input:0"


# --- DEF-027: the opening gap is measured against the LEDGER -----------------------------

_SCHWAB_HEADER = (
    '"Date","Action","Symbol","Description","Quantity","Price","Fees & Comm","Amount"\n'
)


def _statement(*rows: str) -> list[dict[str, str]]:
    return [{"name": "aapl.csv", "text": _SCHWAB_HEADER + "".join(r + "\n" for r in rows)}]


def test_the_opening_hint_subtracts_what_the_ledger_already_holds(
    api_client: TestClient,
) -> None:
    """The golden ledger holds 10 AAPL in schwab (bought 2026-01-10). A statement that
    sells 15 on 2026-03-02 needs 15 the file never bought — but only 5 are missing. The
    file-only hint asked for 15 on top of the 10 (measured 2026-09-23 as −914.96 shares
    after a 1,000-share sell into an 85.04 position)."""
    body = _convert(api_client, exports=_statement(
        '"03/02/2026","Sell","AAPL","APPLE INC","15","$200.00","$0.01","$2,999.99"',
    )).json()
    assert body["ok"] is True
    assert body["openings_build_date"] == "2026-03-01"
    assert body["openings_needing_cost"] == [{
        "symbol": "AAPL", "shares": "15", "ledger_shares": "10", "gap": "5",
        "satisfied": False, "as_of": "2026-03-01",
    }]


def test_a_position_the_ledger_already_covers_needs_no_opening(api_client: TestClient) -> None:
    body = _convert(api_client, exports=_statement(
        '"03/02/2026","Sell","AAPL","APPLE INC","8","$200.00","$0.01","$1,599.99"',
    )).json()
    [o] = body["openings_needing_cost"]
    assert o["satisfied"] is True and o["gap"] == "0" and o["ledger_shares"] == "10"


def test_ledger_shares_are_counted_at_the_day_BEFORE_the_statement_starts(
    api_client: TestClient,
) -> None:
    """Date-aware, like the sell guard: a ledger buy dated AFTER the statement's first day
    was not held when the statement starts, so it does not shrink the gap."""
    body = _convert(api_client, exports=_statement(
        '"01/05/2026","Sell","AAPL","APPLE INC","15","$200.00","$0.01","$2,999.99"',
    )).json()
    [o] = body["openings_needing_cost"]
    assert o == {"symbol": "AAPL", "shares": "15", "ledger_shares": "0", "gap": "15",
                 "satisfied": False, "as_of": "2026-01-04"}
