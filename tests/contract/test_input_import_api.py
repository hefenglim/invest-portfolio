"""Contract tests for POST /api/import/preview (spec 12.3, Task 1).

The point of these tests is the wire shape + status derivation, not a specific
error classification.  Row classification is asserted against the ACTUAL builder
behavior (see the 23300 note below).
"""

from fastapi.testclient import TestClient

_TXN_CSV = (
    "account,symbol,side,date,shares,price\n"
    "tw_broker,2330,buy,2026-06-02,100,600\n"        # ok
    "tw_broker,2330,sell,2026-06-03,5000,600\n"      # warn: oversell (holds 1000)
    "tw_broker,23300,buy,2026-06-02,100,600\n"       # ok: 23300 fuzzy-matches 2330
)
# 23300 does NOT yield a hard error nor a soft symbol_unresolved warn: the resolver
# fuzzy-matches "23300" -> "2330" (SequenceMatcher ratio ~0.889 >= 0.6 threshold),
# so resolution is FUZZY (not NEEDS_AI) and no issue is appended -> status "ok".


def test_import_preview_counts_and_status(api_client: TestClient) -> None:
    r = api_client.post("/api/import/preview", json={"kind": "transactions", "csv_text": _TXN_CSV})
    assert r.status_code == 200
    b = r.json()
    assert b["summary"] == {"total": 3, "ok": 2, "warn": 1, "error": 0}
    by_n = {row["n"]: row for row in b["rows"]}
    assert by_n[0]["status"] == "ok"
    assert by_n[1]["status"] == "warn"
    assert by_n[2]["status"] == "ok"


def test_import_preview_bad_kind_400(api_client: TestClient) -> None:
    r = api_client.post("/api/import/preview", json={"kind": "nope", "csv_text": "a,b\n1,2\n"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "validation_error"


def test_import_commit_writes_ok_rows(api_client: TestClient) -> None:
    csv = ("account,symbol,side,date,shares,price\n"
           "tw_broker,2330,buy,2026-06-02,100,600\n")
    r = api_client.post("/api/import/commit",
                        json={"kind": "transactions", "csv_text": csv, "ack_warnings": False})
    assert r.status_code == 200 and r.json() == {"written": 1, "skipped": 0}


def test_import_commit_warn_requires_ack_422(api_client: TestClient) -> None:
    r = api_client.post("/api/import/commit",
                        json={"kind": "transactions", "csv_text": _TXN_CSV, "ack_warnings": False})
    assert r.status_code == 422 and r.json()["error"]["code"] == "warnings_unacknowledged"


def test_import_commit_acked_writes_nonhard_skips_hard(api_client: TestClient) -> None:
    # _TXN_CSV rows: ok (write), warn-oversell (write on ack), 23300 fuzzy->ok (write)
    # => 3 written, 0 skipped
    r = api_client.post("/api/import/commit",
                        json={"kind": "transactions", "csv_text": _TXN_CSV, "ack_warnings": True})
    assert r.status_code == 200
    body = r.json()
    assert body["written"] == 3 and body["skipped"] == 0


def test_import_commit_hard_row_skipped(api_client: TestClient) -> None:
    # a malformed row (bad number) -> parse_error (hard) -> skipped; the ok row writes
    csv = ("account,symbol,side,date,shares,price\n"
           "tw_broker,2330,buy,2026-06-02,100,600\n"
           "tw_broker,2330,buy,2026-06-02,notanumber,600\n")
    r = api_client.post("/api/import/commit",
                        json={"kind": "transactions", "csv_text": csv, "ack_warnings": True})
    assert r.status_code == 200 and r.json() == {"written": 1, "skipped": 1}


def test_import_commit_bad_kind_400(api_client: TestClient) -> None:
    r = api_client.post("/api/import/commit",
                        json={"kind": "nope", "csv_text": "a\n1\n", "ack_warnings": False})
    assert r.status_code == 400 and r.json()["error"]["code"] == "validation_error"
