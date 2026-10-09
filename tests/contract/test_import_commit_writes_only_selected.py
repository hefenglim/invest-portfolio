"""F-03 counter-evidence: 「確認寫入勾選列」 must write the ticked rows and only those.

The CSV preview renders a checkbox per row and the button beside it declares its own scope
in its label. Neither was connected to anything: ``renderCsvPreview`` created the boxes with
no listener and no row index, and ``commitCsv`` posted the WHOLE pasted text. Measured by
the 2026-08-27 sweep — 3 rows pasted, 2 unticked, 3 rows written, banner 「成功 3 筆」.

This is a step worse than the export-centre buttons that reported success and did nothing:
those under-delivered, this one wrote ledger rows the user had explicitly removed.

``commit_preview`` has taken an ``accept`` set from the start, and the comment beside it
already reasons about "a row the caller deselected" — the seam was designed for this and
simply had no caller. The selection is sent as ROW INDICES rather than a re-rendered CSV:
``csv.DictReader`` row *n* is not text line *n+1* whenever a quoted field contains a newline,
and the frontend has no CSV parser (nor should it — it must not re-derive what the server
already parsed correctly).
"""

from fastapi.testclient import TestClient

_CSV = (
    "account,symbol,side,date,shares,price\n"
    "tw_broker,2330,buy,2026-02-01,100,510\n"
    "tw_broker,2330,buy,2026-02-02,100,520\n"
    "tw_broker,2330,buy,2026-02-03,100,530\n"
)


def _commit(api_client: TestClient, **extra: object) -> dict[str, object]:
    body: dict[str, object] = {"kind": "transactions", "csv_text": _CSV,
                               "ack_warnings": True}
    body.update(extra)
    r = api_client.post("/api/import/commit", json=body)
    assert r.status_code == 200, r.text
    out: dict[str, object] = r.json()
    return out


def test_only_the_selected_row_is_written(api_client: TestClient) -> None:
    out = _commit(api_client, select=[0])
    assert out["written"] == 1, f"wrote rows the user unticked: {out}"
    assert out["skipped"] == 2


def test_a_selection_in_the_middle_writes_that_row_and_no_other(
    api_client: TestClient
) -> None:
    """Indices, not a count — writing "the first N" would pass the test above by accident."""
    out = _commit(api_client, select=[1])
    assert out["written"] == 1 and out["skipped"] == 2
    rows = api_client.get("/api/ledgers/transactions").json()["rows"]
    dates = [r["date"] for r in rows if r["symbol"] == "2330"]
    assert "2026-02-02" in dates
    assert "2026-02-01" not in dates and "2026-02-03" not in dates


def test_an_empty_selection_writes_nothing(api_client: TestClient) -> None:
    out = _commit(api_client, select=[])
    assert out["written"] == 0 and out["skipped"] == 3


def test_omitting_the_selection_still_writes_every_row(api_client: TestClient) -> None:
    """Backwards compatibility: every OTHER caller of this endpoint sends no selection and
    means "all of it" — the AI door commits a CSV it already filtered, and so does undo."""
    out = _commit(api_client)
    assert out["written"] == 3 and out["skipped"] == 0


def test_selecting_a_row_that_does_not_exist_writes_nothing_rather_than_guessing(
    api_client: TestClient
) -> None:
    out = _commit(api_client, select=[7])
    assert out["written"] == 0
