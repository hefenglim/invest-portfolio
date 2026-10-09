"""GET /api/ledgers/cash — the 6th ledger's page view, and the tab/export pairing.

Cash movements were writable (`POST /api/cash/movements`, the CSV kind) from 2026-08-13 and
readable on the ledger page from **nowhere**: the tab bar listed five of six. This is the
sixth tab's source, deliberately in the same shape as its neighbours so the pager, the
account/date filters and the CSV export button need no special case.

The second test is the one with teeth. `web/trades.html`'s export glue maps a tab id to an
`export_kind`, and its own comment already warned what an omission costs:

    a 5th tab that is not here silently exports the transactions ledger under the
    corporate-action tab

That is a wrong FILE under a right-looking button — no error, no empty result, just the
wrong ledger. A 6th tab has exactly the same failure available to it, so the mapping is
checked against the registry from the outside.
"""

import re
from pathlib import Path

from fastapi.testclient import TestClient

from portfolio_dash.shared.ledger_registry import EXPORT_KINDS

_TRADES = Path(__file__).resolve().parents[2] / "web" / "trades.html"


def test_the_cash_ledger_route_returns_the_page_envelope(api_client: TestClient) -> None:
    r = api_client.get("/api/ledgers/cash")
    assert r.status_code == 200
    body = r.json()
    assert "rows" in body and "total_count" in body


def test_a_debit_kind_is_sent_negative_and_a_credit_positive(
    api_client: TestClient,
) -> None:
    """★ The sign is the server's, because the sign is not in the amount.

    Amounts are stored UNSIGNED with the direction in the kind, so a page that printed
    `amount` would show a broker fee as money arriving. The frontend may not compute money,
    which leaves exactly one correct place for the multiplication.
    """
    api_client.post("/api/cash/movements", json={
        "account_id": "schwab", "date": "2026-06-02", "kind": "DEPOSIT",
        "ccy": "USD", "amount": "1000", "note": "t-credit"})
    api_client.post("/api/cash/movements", json={
        "account_id": "schwab", "date": "2026-06-03", "kind": "BROKER_FEE",
        "ccy": "USD", "amount": "12.50", "note": "t-debit"})

    rows = api_client.get("/api/ledgers/cash").json()["rows"]
    by_note = {r["note"]: r for r in rows}
    credit, debit = by_note["t-credit"], by_note["t-debit"]

    assert credit["amount"] == "1000" and credit["signed_amount"] == "1000"
    # The stored amount is positive; only the signed figure carries the direction.
    assert debit["amount"] == "12.50" and debit["signed_amount"] == "-12.50"
    assert debit["kind_label"] == "券商費用"


def test_the_date_filter_and_account_filter_reach_the_route(
    api_client: TestClient,
) -> None:
    api_client.post("/api/cash/movements", json={
        "account_id": "schwab", "date": "2026-06-10", "kind": "INTEREST",
        "ccy": "USD", "amount": "3", "note": "t-in-range"})
    rows = api_client.get(
        "/api/ledgers/cash", params={"from": "2026-06-09", "to": "2026-06-11"}
    ).json()["rows"]
    assert [r["note"] for r in rows] == ["t-in-range"]

    other = api_client.get(
        "/api/ledgers/cash", params={"account_id": "tw"}
    ).json()["rows"]
    assert all(r["account_id"] == "tw" for r in other)


def test_every_ledger_tab_maps_to_its_own_export_kind() -> None:
    """★ Read out of the page source, because the bug is a wrong VALUE, not a crash.

    Parsed rather than imported: the map is an object literal inside trades.html's inline
    glue, and the alternative — trusting a reviewer to notice a missing pair — is what let
    the same class of omission through in eight other places this week.
    """
    src = _TRADES.read_text(encoding="utf-8")

    tabs = re.search(r"const LEDGER_TABS = \[([^\]]*)\]", src)
    assert tabs is not None, "LEDGER_TABS not found — did the glue move?"
    tab_ids = re.findall(r"'([a-z]+)'", tabs.group(1))

    kinds = re.search(r"var KIND = \{(.*?)\};", src, re.S)
    assert kinds is not None, "the export KIND map not found — did the glue move?"
    mapping = dict(re.findall(r"(\w+):\s*'([a-z_]+)'", kinds.group(1)))

    assert set(mapping) == set(tab_ids), "every tab needs an export kind, and no extras"
    assert set(mapping.values()) == set(EXPORT_KINDS), (
        "the tabs and the registry's exportable set must name the same ledgers"
    )
    assert len(set(mapping.values())) == len(mapping), "two tabs share one export kind"
