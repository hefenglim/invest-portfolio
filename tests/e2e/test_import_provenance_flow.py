"""E2E (Playwright, real server + real frontend): import provenance — re-import + undo (B3).

Two properties that only exist together, driven through the REAL upload control:

**Re-importing the same file writes nothing.** Provenance hashes each row's own content, so a
second upload of an export matches and skips instead of doubling the ledger. This is the
property that makes "import the statement again to be sure" a safe thing to do, and it is
worth exactly nothing if it is only true of the API: the browser is the door the owner uses,
and it uploads through ``#csv-file-input`` → ``FileReader`` → ``/api/import/preview`` →
``/api/import/commit``, a chain in which the preview cannot see duplicates at all (they are
detected at commit). So the preview happily offers a row that will not be written, the confirm
enables, and only the commit's own answer distinguishes "wrote it" from "already had it".

**And the whole import can be taken back.** ``POST /api/import/commit`` now returns an
``import_batch_id``; ``DELETE /api/import/batches/{id}`` removes exactly the rows that batch
wrote. Without it the only undo is restoring a pre-import backup, which also discards
everything entered since — so the safe move would be never to attempt the import at all.

⚠ The delete has **no UI control anywhere in ``web/``** (searched: no page references
``/api/import/batches``). It is exercised here through the page's own fetch layer
(``window.pdApi``) rather than a click, so the assertion is honest about what is covered: the
endpoint and its effect on the ledger, not a button that does not exist yet.

ZERO console / page errors throughout; the browser context comes from the shared ``fresh_page``
fixture (issue #67's third-party stub).
"""

import json
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest
from playwright.sync_api import FilePayload, Page, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory

#: Two rows, so "wrote nothing the second time" is a statement about a FILE and not about the
#: one row that happened to be first. Both land on 2330, which the golden ledger already holds.
UPLOAD = (
    b"account,symbol,side,date,shares,price\r\n"
    b"tw_broker,2330,buy,2026-07-10,100,600\r\n"
    b"tw_broker,2330,buy,2026-07-11,50,610\r\n"
)


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Re-enable loopback sockets PER TEST (pytest-socket re-bans before every test); each
    flow spawns a fresh isolated uvicorn (free-port probe + readiness poll need loopback)."""
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _get_json(base_url: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(base_url + path, timeout=10) as r:  # noqa: S310 (loopback)
        data: dict[str, Any] = json.loads(r.read().decode("utf-8"))
        return data


def _sink(page: Page) -> tuple[list[str], list[str]]:
    """Console-error + pageerror sinks. Chromium logs every 4xx as a console error, and this
    flow EXPECTS one (the 422 that raises the warning dialog), so the list is filtered by
    substring rather than dropped — a dropped assertion would cover every OTHER error too."""
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(getattr(m, "text", ""))
            if getattr(m, "type", None) == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    return console_errors, page_errors


def _unexpected(console_errors: list[str], *allowed: str) -> list[str]:
    return [e for e in console_errors if not any(a in e for a in allowed)]


def _ledger_state(base: str) -> tuple[int, str]:
    """(transaction-row count, 2330 share count) — the two numbers an undo must restore."""
    total = int(_get_json(base, "/api/ledgers/transactions")["total_count"])
    shares = next(h["shares"] for h in _get_json(base, "/api/dashboard")["holdings"]
                  if h["symbol"] == "2330")
    return total, str(shares)


def _open_csv_tab(page: Page, base: str) -> None:
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#csv-kinds .chip", state="attached")
    page.click("#tab-csv")
    page.wait_for_selector("#csv-dropzone", state="visible")


def _upload_and_commit(page: Page, name: str, *, expect_warning: bool) -> dict[str, Any]:
    """Drive the real upload control once and return the commit response body.

    ``expect_warning`` covers the SECOND upload: an identical row already in the ledger is a
    soft ``duplicate_trade`` (audit M7), so the preview marks it ⚠ and the commit answers 422
    ``warnings_unacknowledged`` until the owner confirms the dialog. That confirmation is a
    real part of this flow — it is where a user says "yes, import it anyway" and the ledger
    must STILL not double.
    """
    with page.expect_response("**/api/import/preview") as pv:
        page.set_input_files("#csv-file-input", files=[
            FilePayload(name=name, mimeType="text/csv", buffer=UPLOAD)])
    assert pv.value.status == 200
    warn_rows = sum(1 for r in pv.value.json()["rows"] if r["status"] == "warn")
    assert warn_rows == (2 if expect_warning else 0), pv.value.json()["rows"]
    page.wait_for_function(
        "() => { const b = document.querySelector('#csv-confirm'); return b && !b.disabled; }")
    with page.expect_response("**/api/import/commit") as cm:
        page.click("#csv-confirm")
    if not expect_warning:
        assert cm.value.status == 200, f"commit status {cm.value.status}"
        body: dict[str, Any] = cm.value.json()
        return body
    assert cm.value.status == 422
    assert cm.value.json()["error"]["code"] == "warnings_unacknowledged"
    dialog = page.locator(".modal-backdrop .modal", has_text="匯入警告確認")
    expect(dialog).to_be_visible()
    with page.expect_response("**/api/import/commit") as acked:
        dialog.locator("button", has_text="確認寫入").click()
    assert acked.value.status == 200, f"acked commit status {acked.value.status}"
    acked_body: dict[str, Any] = acked.value.json()
    return acked_body


@pytest.mark.e2e
def test_reimporting_the_same_file_writes_nothing_and_the_batch_can_be_undone(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """Upload → commit → upload the SAME file → nothing written → delete the batch → back.

    Breaks if: the row hashes stop being derived from row CONTENT (the second upload doubles
    the ledger — the exact defect provenance exists to prevent); the batch record stops being
    written or its id stops riding the commit response (the undo becomes unreachable at the
    only moment anyone wants it); or the delete stops being keyed on ``import_batch_id`` and
    takes the hand-entered golden rows with it.
    """
    base = flow_server(_seed_golden)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    before = _ledger_state(base)
    assert before == (2, "1000"), before          # the golden ledger, untouched
    assert _get_json(base, "/api/import/batches")["batches"] == []

    _open_csv_tab(page, base)

    # --- first upload: the rows land, and the response carries the undo handle ------------
    first = _upload_and_commit(page, "schwab_export.csv", expect_warning=False)
    assert first["written"] == 2 and first["skipped"] == 0
    assert "duplicates" not in first              # nothing was already held
    batch_id = first["import_batch_id"]
    page.wait_for_selector("#csv-result", state="visible")

    after_first = _ledger_state(base)
    assert after_first == (4, "1150")             # 1000 + 100 + 50

    batches = _get_json(base, "/api/import/batches")["batches"]
    assert len(batches) == 1
    assert batches[0]["id"] == batch_id
    assert batches[0]["kind"] == "transactions" and batches[0]["row_count"] == 2

    # --- second upload of the SAME file: ZERO new rows ------------------------------------
    second = _upload_and_commit(page, "schwab_export.csv", expect_warning=True)
    assert second["written"] == 0
    assert second["duplicates"] == 2              # the ledger already held every row
    assert "import_batch_id" not in second        # no batch owning no data
    assert _ledger_state(base) == after_first     # the ledger did not move

    # The banner must SAY the rows were already held. Without this the screen's whole account
    # of a full re-import is 「成功 0 筆・跳過 0 筆」 — true, uninformative, and the reading a
    # user takes from it is "the upload failed, try again". A duplicate is neither written nor
    # skipped (skipped means the user deselected it), so the count needs its own words.
    expect(page.locator("#csv-result")).to_contain_text("成功 0 筆")
    expect(page.locator("#csv-result")).to_contain_text("已匯入過 2 筆")
    expect(page.locator("#csv-result")).to_contain_text("帳本沒有變成兩筆")
    assert len(_get_json(base, "/api/import/batches")["batches"]) == 1   # still just the one

    # --- undo: the batch's rows go, the hand-entered ones stay ----------------------------
    undone = page.evaluate(
        "(id) => window.pdApi.del('/api/import/batches/' + id)", batch_id)
    assert undone == {"deleted": 2, "import_batch_id": batch_id}
    assert _ledger_state(base) == before          # exactly the pre-import ledger
    assert _get_json(base, "/api/import/batches")["batches"] == []

    # The one expected console error is the 422 chromium logs for the warning dialog.
    assert _unexpected(console_errors, "status of 422") == []
    assert page_errors == []
