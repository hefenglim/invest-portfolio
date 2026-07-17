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
    "tw_broker,23300,buy,2026-06-02,100,600\n"       # warn: 23300 fuzzy-matches 2330
)
# 23300 fuzzy-matches "2330" (SequenceMatcher ratio ~0.889 >= 0.6 threshold) ->
# resolution is FUZZY (not NEEDS_AI). The importer now surfaces a soft
# `fuzzy_resolved` (needs_confirm) issue -> status "warn", and the persisted
# payload symbol is the RESOLVED "2330" (no silent phantom-symbol write).


def test_import_preview_counts_and_status(api_client: TestClient) -> None:
    r = api_client.post("/api/import/preview", json={"kind": "transactions", "csv_text": _TXN_CSV})
    assert r.status_code == 200
    b = r.json()
    assert b["summary"] == {"total": 3, "ok": 1, "warn": 2, "error": 0}
    by_n = {row["n"]: row for row in b["rows"]}
    assert by_n[0]["status"] == "ok"
    assert by_n[1]["status"] == "warn"
    assert by_n[2]["status"] == "warn"
    # the fuzzy row persists the RESOLVED symbol, not the raw typo
    assert by_n[2]["data"]["symbol"] == "2330"


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
    # _TXN_CSV rows: ok (write), warn-oversell (write on ack), 23300 fuzzy-warn
    # (write on ack, persisting the RESOLVED 2330) => 3 written, 0 skipped
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


_DIV_CSV = (
    "account,symbol,date,type,gross\n"
    "tw_broker,2330,2026-06-02,cash,5000\n"
)
# build_dividend_preview required columns: account, symbol, date, type, gross.


def test_import_dividends_preview_and_commit_roundtrip(api_client: TestClient) -> None:
    r = api_client.post("/api/import/preview",
                        json={"kind": "dividends", "csv_text": _DIV_CSV})
    assert r.status_code == 200
    b = r.json()
    assert b["summary"]["total"] == 1
    assert len(b["rows"]) == 1
    c = api_client.post("/api/import/commit",
                        json={"kind": "dividends", "csv_text": _DIV_CSV, "ack_warnings": True})
    assert c.status_code == 200 and c.json()["written"] == 1


# --- FU-D19: date-format hardening (annotated headers + ambiguity chooser + pinned parse) ---

# 3/4 and 5/6 both parse as M/D and as D/M but read to DIFFERENT dates -> genuinely ambiguous.
_AMBIG_CSV = (
    "account,symbol,side,date,shares,price\n"
    "tw_broker,2330,buy,3/4/2026,100,600\n"
    "tw_broker,2330,buy,5/6/2026,100,600\n"
)


def test_preview_reports_date_ambiguity_and_holds_rows_as_errors(api_client: TestClient) -> None:
    r = api_client.post("/api/import/preview",
                        json={"kind": "transactions", "csv_text": _AMBIG_CSV})
    assert r.status_code == 200
    b = r.json()
    amb = b["date_ambiguity"]
    assert amb["column"] == "date"
    assert {c["id"] for c in amb["candidates"]} == {"mdy", "dmy"}
    # every candidate carries an example in/out; the two ISO readings differ (proof of conflict)
    assert {c["example_out"] for c in amb["candidates"]} != {""}
    assert len({c["example_out"] for c in amb["candidates"]}) == 2
    # no guess: with no format pinned, the date rows are errors
    assert b["summary"]["error"] == 2


def test_preview_with_pinned_date_format_resolves(api_client: TestClient) -> None:
    r = api_client.post("/api/import/preview",
                        json={"kind": "transactions", "csv_text": _AMBIG_CSV,
                              "date_format": "mdy"})
    assert r.status_code == 200
    b = r.json()
    assert "date_ambiguity" not in b
    assert b["summary"]["error"] == 0
    # M/D reading: 3/4 -> 2026-03-04, 5/6 -> 2026-05-06
    assert b["rows"][0]["data"]["trade_date"] == "2026-03-04"
    assert b["rows"][1]["data"]["trade_date"] == "2026-05-06"


def test_commit_without_pin_under_ambiguity_is_422_and_writes_nothing(
    api_client: TestClient,
) -> None:
    r = api_client.post("/api/import/commit",
                        json={"kind": "transactions", "csv_text": _AMBIG_CSV,
                              "ack_warnings": True})
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "date_ambiguity_unresolved"
    assert body["date_ambiguity"]["column"] == "date"


def test_commit_with_pinned_date_format_writes(api_client: TestClient) -> None:
    r = api_client.post("/api/import/commit",
                        json={"kind": "transactions", "csv_text": _AMBIG_CSV,
                              "date_format": "dmy", "ack_warnings": False})
    assert r.status_code == 200
    assert r.json() == {"written": 2, "skipped": 0}


def test_unknown_date_format_is_400(api_client: TestClient) -> None:
    r = api_client.post("/api/import/preview",
                        json={"kind": "transactions", "csv_text": _AMBIG_CSV,
                              "date_format": "bogus"})
    assert r.status_code == 400 and r.json()["error"]["field"] == "date_format"


def test_slash_date_column_auto_resolves_without_chooser(api_client: TestClient) -> None:
    # YYYY/M/D is unambiguous (year-first) -> auto-resolved, no chooser, dates normalized to ISO.
    csv = ("account,symbol,side,date,shares,price\n"
           "tw_broker,2330,buy,2026/7/10,100,600\n")
    r = api_client.post("/api/import/preview", json={"kind": "transactions", "csv_text": csv})
    assert r.status_code == 200
    b = r.json()
    assert "date_ambiguity" not in b
    assert b["rows"][0]["data"]["trade_date"] == "2026-07-10"


def test_annotated_header_csv_previews_clean(api_client: TestClient) -> None:
    # a pasted CSV with the template's annotated header must canonicalize + parse cleanly.
    csv = ("account,symbol,side,date(YYYY-MM-DD),shares,price,fee(選填)\n"
           "tw_broker,2330,buy,2026-07-10,100,600,\n")
    r = api_client.post("/api/import/preview", json={"kind": "transactions", "csv_text": csv})
    assert r.status_code == 200
    b = r.json()
    assert b["summary"]["error"] == 0
    assert b["rows"][0]["data"]["symbol"] == "2330"
