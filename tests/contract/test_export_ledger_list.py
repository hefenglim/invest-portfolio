"""GET /api/export/ledgers, and the two things downstream of it that must agree.

The export centre used to name the zip's contents from a sentence typed into
`web/settings-alerts.js`. When corporate actions joined the zip, the sentence did not — the
card told the owner they were about to download four CSVs while the route built five. This
endpoint replaces that sentence with the registry, and the tests below pin the three places
the same set now has to appear:

* the endpoint's list,
* the zip's actual CSV members,
* the printable 帳本報告's section count.

⚠ The pre-existing zip test asserts its members with ``<=`` (subset), which is exactly why
nothing went red when the 5th ledger arrived — a subset assertion cannot see an addition.
The equality below is the point of this file.
"""

import io
import zipfile

from fastapi.testclient import TestClient

from portfolio_dash.shared.ledger_registry import EXPORT_KINDS, LEDGER_TABLES

_NON_LEDGER_MEMBERS = {"fee_rules_snapshot.json", "manifest.json"}


def test_the_endpoint_lists_exactly_the_exportable_ledgers(api_client: TestClient) -> None:
    r = api_client.get("/api/export/ledgers")
    assert r.status_code == 200
    got = r.json()["ledgers"]
    assert [row["kind"] for row in got] == list(EXPORT_KINDS)
    assert [row["label"] for row in got] == [t.label for t in EXPORT_KINDS.values()]


def test_every_listed_ledger_is_a_zip_member_and_nothing_else_is(
    api_client: TestClient,
) -> None:
    """★ Equality, not subset. The claim on screen is 'these are the files you will get'."""
    listed = {row["kind"] for row in api_client.get("/api/export/ledgers").json()["ledgers"]}
    r = api_client.post("/api/export/ledgers")
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        csvs = {n for n in zf.namelist() if n not in _NON_LEDGER_MEMBERS}
    assert csvs == {f"{EXPORT_KINDS[k].table}.csv" for k in listed}


def test_cash_movements_is_a_ledger_but_not_an_export(api_client: TestClient) -> None:
    """The known gap, asserted so it stays deliberate.

    ``cash_movements`` imports and does not export. That asymmetry is recorded in the
    backlog spec and in the report's own header note 「本報告不含資金收支帳本」. When it is
    closed, this test fails and points at the sentence that has to change with it — which is
    the whole reason to assert an absence rather than to leave one.
    """
    tables = {t.table for t in LEDGER_TABLES}
    assert "cash_movements" in tables
    listed = {row["kind"] for row in api_client.get("/api/export/ledgers").json()["ledgers"]}
    assert not any(EXPORT_KINDS[k].table == "cash_movements" for k in listed)

    doc = api_client.post("/api/export/ledgers-report", json={}).content.decode("utf-8")
    assert "本報告不含資金收支帳本" in doc


def test_the_printable_report_has_one_section_per_exportable_ledger(
    api_client: TestClient,
) -> None:
    """The report showed four sections while the zip held five, and its button called them
    「四帳本＋期初庫存」 — five names over four sections, counting 期初庫存 twice.

    Counted structurally rather than by label, deliberately: the registry's labels
    (交易帳本) and the report's headings (交易紀錄) are different words for the same ledger,
    and mapping them here would create the second enumeration this whole change removes.
    """
    doc = api_client.post("/api/export/ledgers-report", json={}).content.decode("utf-8")
    assert doc.count("<section><h2>") == len(EXPORT_KINDS)
    assert "公司行動" in doc
