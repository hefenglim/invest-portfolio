"""I-13 (A-5 / B-2): the advisory severity is the shared mapper's own, preview rows carry their
findings' kinds, and the transactions builder no longer keeps a second membership filter.

* ``api/wire.py::issue_wire`` mapped ``needs_confirm`` → ``warn`` and nothing else, so an
  advisory (``Issue.info``, DEF-014) became a WARNING at every door except the one that
  wrapped it (``input_center._wire_issue``). The mapper now has three tiers and the wrapper is
  retired — the ledger routes' issue lists read the same mapper.
* ``/api/import/preview`` rows carried ``reason`` (a sentence) and a single ``code``; the
  broker door recognised a 賣超 row by regex on the sentence (``/^賣出 .*超過/``), which any
  rewording breaks silently. Each row now carries ``kinds`` and the page keys on
  ``sell_exceeds_holdings``.
* ``csv_import.build_transaction_preview`` pre-filtered the sibling batch on the structural
  prefix — a narrower second copy of ``validate.row_cannot_be_written``, which
  ``pending_share_flows`` already applies. Removed; the share guard's answers are unchanged
  (``tests/contract/test_def024_error_rows_never_cover.py`` is the behavioural pin).
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from portfolio_dash.api.wire import issue_wire
from portfolio_dash.data_ingestion.validate import Issue, advisory_issue

_ROOT = Path(__file__).resolve().parents[2]


def test_the_mapper_has_three_tiers() -> None:
    assert issue_wire(Issue(kind="x", message="m"))["sev"] == "error"
    assert issue_wire(Issue(kind="x", message="m", needs_confirm=True))["sev"] == "warn"
    assert issue_wire(advisory_issue("x", "m"))["sev"] == "info"


def test_the_wrapper_is_retired() -> None:
    src = (_ROOT / "portfolio_dash/api/routers/input_center.py").read_text(encoding="utf-8")
    assert "def _wire_issue(" not in src and "_wire_issue(" not in src


def test_preview_rows_carry_their_kinds(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    r = api_client.post("/api/import/preview", json={
        "kind": "transactions",
        "csv_text": "account,symbol,side,date,shares,price,memo\n"
                    "tw_broker,2330,SELL,2026-06-10,999999,600,x\n"})
    assert r.status_code == 200, r.text
    (row,) = r.json()["rows"]
    assert "sell_exceeds_holdings" in row["kinds"], row
    assert "unknown_columns_ignored" in row["kinds"], row     # advisories too, in order


def test_the_broker_door_keys_the_oversell_line_on_the_kind() -> None:
    """The 賣超 line moved with the dialog to web/import-ack.js (DEF-025, shared by the CSV,
    AI and broker doors); it still keys on the finding's KIND, never on the sentence. The
    behaviour itself is RUN in test_def025_import_ack_front.py."""
    src = (_ROOT / "web/import-ack.js").read_text(encoding="utf-8")
    assert "const OVERSELL = 'sell_exceeds_holdings';" in src
    assert "indexOf(OVERSELL)" in src
    assert "kinds: w.kinds || []" in src
    for name in ("web/import-ack.js", "web/broker-import.js"):
        body = (_ROOT / name).read_text(encoding="utf-8")
        assert not re.search(r"/\^賣出", body), "the 賣超 line still pattern-matches the sentence"


def test_the_builder_keeps_no_second_membership_filter() -> None:
    src = (_ROOT / "portfolio_dash/data_ingestion/csv_import.py").read_text(encoding="utf-8")
    assert "transaction_structural_issues" not in src
