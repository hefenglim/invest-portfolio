"""DEF-045 (functional test manual J-04 / J-06, 2026-09-24): no export prints an account by its
English API label or its bare id where a person reads it.

The printable 帳本報告 read 「TW Broker」「Charles Schwab」 in every 帳戶 column while the screen
says 台灣券商 / 嘉信 Schwab, and the 現金收支明細 report headed itself 「帳戶 TW Broker
（tw_broker）」. Both built ``{a.account_id: a.name for a in list_accounts(conn)}`` and looked
the name up — no f-string interpolated ``a.name``, so the AST guard in
``test_account_ref_seam.py`` (which matched ``{a.name}`` / ``{x.account_id}`` inside an
f-string, and only when nothing wrapped them — ``{_esc(account_id)}`` slipped through too) had
nothing to see. That scanner is widened in the same change; THIS file asks the behavioural
question instead: build every export the app offers, over a ledger that touches every section
and all three accounts, and read what comes out.

* **Every file:** no ``accounts.name`` label, anywhere. A label reaches a person only as a
  ``{account:<id>}`` token, which ``web/api.js::_resolveBlobRefs`` resolves at download.
* **HTML reports (print):** no bare account id in the visible text either (tokens removed
  first — they are the correct spelling).
* **CSV (reconciliation / re-import):** a bare id is allowed ONLY in a column whose header
  is ``account_id`` / ``account`` — ids by design, a CSV that re-imports needs the key, not a
  display name — and in the provenance footer line ``account=<id>, …``. Nowhere else.
"""

from __future__ import annotations

import csv
import io
import re
import sqlite3
import zipfile
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.export.ledgers import LEDGER_KINDS
from portfolio_dash.shared.account_ref import ACCOUNT_REF_RE

_Export = tuple[str, dict[str, object]]
_PER_LEDGER: list[_Export] = [("/api/export/ledger", {"kind": k}) for k in sorted(LEDGER_KINDS)]
_PER_ACCOUNT: list[_Export] = [
    (path, {"account": acct}) for acct in ("tw_broker", "schwab", "moomoo_my")
    for path in ("/api/export/cash-statement", "/api/export/cash-statement-report")]

#: Every export route the pages call, with a body that makes it produce content.
_EXPORTS: list[_Export] = [
    ("/api/export/holdings", {}),
    ("/api/export/ledgers", {}),
    *_PER_LEDGER,
    ("/api/export/realized", {}),
    ("/api/export/ai-predictions", {}),
    ("/api/export/symbol-detail", {"symbol": "2330"}),
    ("/api/export/symbol-detail", {"symbol": "AAPL"}),
    ("/api/export/holdings-report", {}),
    ("/api/export/ledgers-report", {}),
    ("/api/export/llm-usage", {}),
    ("/api/export/job-runs", {}),
    ("/api/export/tax-package", {"year": 2026}),
    *_PER_ACCOUNT,
    ("/api/export/rebalance-report", {"targets": {"2330": "0.6", "AAPL": "0.4"}}),
]

#: CSV columns that carry the account KEY by design (machine-readable; a re-importable ledger
#: CSV needs the id, and the reconciliation CSVs join on it).
_ID_COLUMNS = frozenset({"account_id", "account"})
#: The provenance footer a reconciliation CSV ends with (`account=<id>, ccy=…, as_of=…`).
_FOOTER = re.compile(r"^#? ?account=[^,]+, ")


def _write(client: TestClient, path: str, body: dict[str, object]) -> None:
    r = client.post(path, json=body)
    assert r.status_code in (200, 201), f"{path}: {r.status_code} {r.text}"


@pytest.fixture
def rich_client(api_client: TestClient, golden_db: sqlite3.Connection) -> Iterator[TestClient]:
    """The golden ledger plus one row in every ledger the exports read, on all 3 accounts."""
    c = api_client
    _write(c, "/api/cash/movements", {"account_id": "schwab", "date": "2026-02-01",
                                      "kind": "deposit", "ccy": "USD", "amount": "5000"})
    _write(c, "/api/cash/movements", {"account_id": "moomoo_my", "date": "2026-02-01",
                                      "kind": "deposit", "ccy": "MYR", "amount": "9000"})
    _write(c, "/api/import/commit", {
        "kind": "transactions", "ack_warnings": True, "csv_text":
        "account,symbol,side,date,shares,price\n"
        "tw_broker,2330,SELL,2026-04-01,100,600\n"
        "moomoo_my,AAPL,BUY,2026-02-05,5,110\n"})
    _write(c, "/api/import/commit", {
        "kind": "openings", "ack_warnings": True, "csv_text":
        "account,symbol,shares,cost,build_date,note\n"
        "schwab,AAPL,3,270,2025-12-01,\n"})
    _write(c, "/api/ledgers/corporate-actions", {
        "account_id": "tw_broker", "date": "2026-06-10", "kind": "SPLIT",
        "from_symbol": "2330", "to_symbol": "2330", "ratio_to": "2", "ratio_from": "1"})
    labels = {str(r["name"]) for r in golden_db.execute("SELECT name FROM accounts")}
    assert {"TW Broker", "Charles Schwab"} <= labels   # the detector has something to find
    yield c


def _files(client: TestClient) -> Iterator[tuple[str, str, str]]:
    """(label, content-type, text) for every file every export produces (zips unpacked)."""
    for path, body in _EXPORTS:
        r = client.post(path, json=body)
        assert r.status_code == 200, f"{path} {body}: {r.status_code} {r.text[:300]}"
        ctype = r.headers.get("content-type", "")
        label = f"{path} {body}"
        if "zip" in ctype:
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                for name in z.namelist():
                    yield (f"{label} :: {name}", "text/csv" if name.endswith(".csv")
                           else "text/plain", z.read(name).decode("utf-8-sig"))
        else:
            yield label, ctype, r.content.decode("utf-8-sig")


def _visible_html(html: str) -> str:
    html = re.sub(r"<style.*?</style>", " ", html, flags=re.S)
    return re.sub(r"<[^>]+>", " ", html)


def _account_ids(conn: sqlite3.Connection) -> list[str]:
    return [str(r["account_id"]) for r in conn.execute("SELECT account_id FROM accounts")]


def _bare_id_hits(text: str, ids: list[str]) -> list[str]:
    text = ACCOUNT_REF_RE.sub(" ", text)
    return [i for i in ids if re.search(rf"(?<![\w]){re.escape(i)}(?![\w])", text)]


def _csv_offenders(text: str, ids: list[str]) -> list[str]:
    """Cells naming an account by id OUTSIDE an id column (and outside the footer)."""
    out: list[str] = []
    rows = list(csv.reader(io.StringIO(text)))
    header = rows[0] if rows else []
    for n, row in enumerate(rows[1:], start=2):
        if row and _FOOTER.match(",".join(row)):
            continue
        for k, cell in enumerate(row):
            col = header[k] if k < len(header) else ""
            if col in _ID_COLUMNS:
                continue
            hits = _bare_id_hits(cell, ids)
            if hits:
                out.append(f"row {n} col {col or k}: {cell!r}")
    return out


def test_no_export_prints_an_account_label_or_a_stray_id(
    rich_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    labels = sorted(str(r["name"]) for r in golden_db.execute("SELECT name FROM accounts"))
    ids = _account_ids(golden_db)
    problems: list[str] = []
    seen_html = seen_csv = 0
    for label, ctype, text in _files(rich_client):
        for lab in labels:
            if lab in ACCOUNT_REF_RE.sub(" ", text):
                problems.append(f"{label}: prints the API label {lab!r}")
        if "html" in ctype:
            seen_html += 1
            for i in _bare_id_hits(_visible_html(text), ids):
                problems.append(f"{label}: prints the bare id {i!r}")
        elif "csv" in ctype:
            seen_csv += 1
            problems += [f"{label}: {o}" for o in _csv_offenders(text, ids)]
    assert seen_html >= 6 and seen_csv >= 10, (seen_html, seen_csv)
    assert not problems, "\n".join(problems)


def test_the_printed_reports_name_every_account_through_a_token(
    rich_client: TestClient,
) -> None:
    """Detection power, the positive half: the reports DO name the accounts — as tokens the
    download seam resolves — so the test above is not passing on reports that name nobody."""
    ledger = rich_client.post("/api/export/ledgers-report", json={}).text
    for acct in ("tw_broker", "schwab", "moomoo_my"):
        assert f"{{account:{acct}}}" in ledger, acct
    stmt = rich_client.post("/api/export/cash-statement-report",
                            json={"account": "schwab"}).text
    assert "帳戶 {account:schwab}" in _visible_html(stmt)


def test_the_detectors_bite() -> None:
    ids = ["tw_broker", "schwab", "moomoo_my"]
    assert _bare_id_hits("帳戶 TW Broker（tw_broker）", ids) == ["tw_broker"]
    assert _bare_id_hits("帳戶 {account:tw_broker}", ids) == []
    assert _bare_id_hits("嘉信 Schwab", ids) == []            # the zh name is not the id
    bad = "date,account_id,note\n2026-01-01,schwab,moved from schwab\n"
    assert _csv_offenders(bad, ids) == ["row 2 col note: 'moved from schwab'"]
    ok = "date,account_id\n2026-01-01,schwab\naccount=schwab, ccy=USD, as_of=x\n"
    assert _csv_offenders(ok, ids) == []
