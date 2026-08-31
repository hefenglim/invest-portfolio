"""Contract tests for POST /api/import/preview (spec 12.3, Task 1).

The point of these tests is the wire shape + status derivation, not a specific
error classification.  Row classification is asserted against the ACTUAL builder
behavior (see the 23300 note below).
"""

from fastapi.testclient import TestClient

_TXN_CSV = (
    "account,symbol,side,date,shares,price\n"
    "tw_broker,2330,buy,2026-06-02,100,600\n"        # ok
    "tw_broker,2330,sell,2026-06-03,5000,600\n"      # warn: oversell
    "tw_broker,23300,buy,2026-06-02,100,600\n"       # error: 23300 is an unregistered code
)
# R6-A: "23300" is a code-SHAPED symbol that is not registered. Resolution is exact-only for
# codes (no fuzzy coercion to 2330), so it is a HARD `symbol_unresolved` issue -> status
# "error", and the payload keeps the RAW "23300" (never silently rewritten to a near neighbour).


def test_import_preview_counts_and_status(api_client: TestClient) -> None:
    r = api_client.post("/api/import/preview", json={"kind": "transactions", "csv_text": _TXN_CSV})
    assert r.status_code == 200
    b = r.json()
    assert b["summary"] == {"total": 3, "ok": 1, "warn": 1, "error": 1}
    by_n = {row["n"]: row for row in b["rows"]}
    assert by_n[0]["status"] == "ok"
    assert by_n[1]["status"] == "warn"
    assert by_n[2]["status"] == "error"
    # the unregistered code keeps its RAW symbol — never coerced to 2330
    assert by_n[2]["data"]["symbol"] == "23300"
    assert by_n[2]["code"] == "unregistered_symbol"


def test_import_preview_bad_kind_400(api_client: TestClient) -> None:
    r = api_client.post("/api/import/preview", json={"kind": "nope", "csv_text": "a,b\n1,2\n"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "validation_error"


def test_import_commit_writes_ok_rows(api_client: TestClient) -> None:
    csv = ("account,symbol,side,date,shares,price\n"
           "tw_broker,2330,buy,2026-06-02,100,600\n")
    r = api_client.post("/api/import/commit",
                        json={"kind": "transactions", "csv_text": csv, "ack_warnings": False})
    assert r.status_code == 200
    # ``import_batch_id`` joined the shape with provenance (2026-08-13): it is the handle
    # to the undo, and an undo whose id must be looked up afterwards is one nobody reaches
    # for at the moment they need it.
    assert r.json() == {"written": 1, "skipped": 0, "import_batch_id": 1}


def test_import_commit_warn_requires_ack_422(api_client: TestClient) -> None:
    r = api_client.post("/api/import/commit",
                        json={"kind": "transactions", "csv_text": _TXN_CSV, "ack_warnings": False})
    assert r.status_code == 422 and r.json()["error"]["code"] == "warnings_unacknowledged"


def test_import_commit_acked_writes_nonhard_rejects_hard(api_client: TestClient) -> None:
    # _TXN_CSV rows: ok (write), warn-oversell (write on ack), 23300 unregistered code
    # (HARD -> REJECTED, never coerced) => 2 written, 1 rejected, 0 skipped.
    #
    # `rejected`, not `skipped` (2026-08-14): the row was REFUSED, and 「跳過」 reads as
    # "the caller didn't tick it". Asserting `skipped == 0` here is the load-bearing half —
    # without it the old bucket could quietly keep receiving hard rows as well.
    r = api_client.post("/api/import/commit",
                        json={"kind": "transactions", "csv_text": _TXN_CSV, "ack_warnings": True})
    assert r.status_code == 200
    body = r.json()
    assert body["written"] == 2 and body["rejected"] == 1 and body["skipped"] == 0
    assert body["rejected_rows"][0]["kind"] == "symbol_unresolved"


def test_import_commit_hard_row_rejected_with_its_reason(api_client: TestClient) -> None:
    # a malformed row (bad number) -> parse_error (hard) -> REJECTED; the ok row writes
    csv = ("account,symbol,side,date,shares,price\n"
           "tw_broker,2330,buy,2026-06-02,100,600\n"
           "tw_broker,2330,buy,2026-06-02,notanumber,600\n")
    r = api_client.post("/api/import/commit",
                        json={"kind": "transactions", "csv_text": csv, "ack_warnings": True})
    assert r.status_code == 200
    body = r.json()
    assert body["written"] == 1 and body["skipped"] == 0 and body["rejected"] == 1
    # 1-BASED, and it carries the reason: a count alone tells the owner something went
    # wrong without telling them which row or what to do about it.
    assert body["rejected_rows"] == [
        {"row": 2, "kind": "parse_error", "message": body["rejected_rows"][0]["message"]}]
    assert body["import_batch_id"] == 1


_SPLIT_CSV = (
    "account,date,kind,from_symbol,to_symbol,ratio_to,ratio_from,cost_carry,note\n"
    "tw_broker,2026-06-05,SPLIT,2330,2330,4,1,,\n"
)
#: The golden ledger holds 1,000 shares of 2330. After a 4-for-1 that is 4,000; this sells
#: 3,600 of them, which is 賣超 unless the split is known about.
_POST_SPLIT_SELL = (
    "account,symbol,side,date,shares,price\n"
    "tw_broker,2330,sell,2026-06-10,3600,150\n"
)


def _oversold_rows(payload: dict[str, object]) -> list[object]:
    """Preview rows carrying the 賣超 warning.

    Matched on ``reason``, because ``code`` is deliberately narrow — it exists for the one
    issue the frontend takes an ACTION on (``unregistered_symbol``) and is ``None`` for
    everything else. The wire simply does not carry an issue kind for a warning.

    ⚠ The marker was ``"held"`` until 2026-08-29 (QA-24). That was the English headline
    ``sell 150 > held 100``, which the UI rendered verbatim as the FIRST line of the 賣超
    confirmation dialog — above the Chinese sentence explaining that acking permanently
    discards the cost basis — so one dialog read half in each language. The message is now
    「賣出 3600 股，超過持有的 1000 股」, and 「超過」 + 「持有」 is the substring both branches
    share (the date-aware one reads 「超過 {date} 當日持有的 …」). This helper failed CLOSED
    when the text moved: it returned ``[]`` and the assertion read "no oversell warning",
    which is the reassuring direction to be wrong in — worth remembering if it is ever
    rewritten.
    """
    rows = payload["rows"]
    assert isinstance(rows, list)
    return [r for r in rows
            if r["status"] == "warn"
            and "超過" in (r["reason"] or "") and "持有" in (r["reason"] or "")]


def test_pending_actions_csv_clears_a_post_split_sell_on_PREVIEW(
    api_client: TestClient,
) -> None:
    """The broker one-click flow imports trades BEFORE the corporate actions (an action's own
    guards need the position to exist), so a sell that is only legal after a split meets a
    pre-split count. Telling the preview which actions are coming is what stops it demanding
    an ack on 賣超 — the one confirmation that permanently discards a cost basis."""
    without = api_client.post("/api/import/preview", json={
        "kind": "transactions", "csv_text": _POST_SPLIT_SELL}).json()
    assert len(_oversold_rows(without)) == 1

    with_split = api_client.post("/api/import/preview", json={
        "kind": "transactions", "csv_text": _POST_SPLIT_SELL,
        "pending_actions_csv": _SPLIT_CSV}).json()
    assert _oversold_rows(with_split) == []


def test_pending_actions_csv_is_re_applied_on_COMMIT(api_client: TestClient) -> None:
    """The commit re-derives the preview, so the field has to ride along or the write path
    reaches a different verdict than the screen showed — and refuses with
    ``warnings_unacknowledged`` for a warning the owner was never shown."""
    refused = api_client.post("/api/import/commit", json={
        "kind": "transactions", "csv_text": _POST_SPLIT_SELL})
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "warnings_unacknowledged"

    ok = api_client.post("/api/import/commit", json={
        "kind": "transactions", "csv_text": _POST_SPLIT_SELL,
        "pending_actions_csv": _SPLIT_CSV})
    assert ok.status_code == 200 and ok.json()["written"] == 1


def test_pending_actions_csv_composes_with_a_selection(api_client: TestClient) -> None:
    """FIX-A1's re-derivation must not cost the widening: a ``select`` makes the commit
    re-derive the preview over the narrowed batch (QA-01, extended to transactions), and
    if that second derivation dropped ``pending_actions`` the sell would surface a 賣超
    KIND the full-file preview never showed — and be skipped under the shrink-only rule.
    Selecting the row explicitly must book it exactly as omitting the selection does."""
    ok = api_client.post("/api/import/commit", json={
        "kind": "transactions", "csv_text": _POST_SPLIT_SELL,
        "pending_actions_csv": _SPLIT_CSV, "select": [0]})
    assert ok.status_code == 200, ok.text
    assert ok.json()["written"] == 1


def test_pending_actions_csv_is_ignored_by_every_OTHER_kind(api_client: TestClient) -> None:
    """Scoped to transactions on purpose. The other five kinds validate against a replayed
    book, not against this walker, and quietly widening their inputs would be a change
    nobody asked for hiding inside a field named for one flow."""
    r = api_client.post("/api/import/preview", json={
        "kind": "fx", "csv_text": "account,date,from_ccy,from_amount,to_ccy,to_amount\n",
        "pending_actions_csv": _SPLIT_CSV})
    assert r.status_code == 200 and r.json()["rows"] == []


def test_a_clean_commit_carries_NO_rejected_key(api_client: TestClient) -> None:
    """Additive only when non-zero — the ``duplicates`` convention. A response shape that
    grows for every caller when one case gains a field is how a contract drifts."""
    csv = ("account,symbol,side,date,shares,price\n"
           "tw_broker,2330,buy,2026-06-02,100,600\n")
    r = api_client.post("/api/import/commit",
                        json={"kind": "transactions", "csv_text": csv})
    assert r.json() == {"written": 1, "skipped": 0, "import_batch_id": 1}


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
    assert r.json() == {"written": 2, "skipped": 0, "import_batch_id": 1}


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
