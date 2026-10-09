"""E2E: a 交易帳本 correction is VISIBLE while it runs, and it runs ONCE (M3-04, 2026-09-06).

Every correction door in ``web/ledger.js`` closed its dialog FIRST and awaited the request
second — ``dismiss(); await …`` — so the moment the request started, the button that could
have carried a busy state was already out of the DOM. Measured under a 2.5 s delay: no
modal, no toast, no spinner, every row button live, and a second 儲存 or 刪除 fired a second
PUT / DELETE against the same row. The success toast also fired BEFORE the six ledger tables
were re-fetched, so 「刪除完成」 stood on screen beside the row it claimed was gone.

The fix borrows two mechanisms this page already had (owner ruling: option 1, ledger.js only):

* the edit modal STAYS OPEN with 儲存 in ``pdBusy`` until the request settles — the precedent
  is the 公司行動 form on the same page (``corp-action-form.js``); a failure restores the
  button and leaves the modal, values intact, so the user corrects instead of re-typing;
* the delete confirm closes itself before ``onConfirm`` runs (``shell.js`` — out of scope), so
  a ``toastProgress`` spinner stands in and a module-level in-flight flag makes every row
  button a no-op until the tables are rebuilt.

The in-flight window is made observable by HOLDING the mutating request in a Playwright
route — the ``Route`` is parked and released from the test body — rather than by a network
throttle, so every assertion below reads a deterministic state, not a race against latency.
Driven against the REAL stack because the defect is in the wiring between a correct server
and the page's own feedback: no contract test can see a button that is not there.
"""

import json
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest
from playwright.sync_api import Page, Route
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Re-enable loopback sockets PER TEST (pytest-socket re-bans before every test); each flow
    spawns a fresh isolated uvicorn (free-port probe + readiness poll need loopback TCP)."""
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _get_json(base_url: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(base_url + path, timeout=5) as r:  # noqa: S310 (loopback)
        data: dict[str, Any] = json.loads(r.read().decode("utf-8"))
        return data


def _post_json(base_url: str, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(  # noqa: S310 (loopback)
        base_url + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 (loopback)
        payload: dict[str, Any] = json.loads(r.read().decode("utf-8"))
        return r.status, payload


def _sink(page: Page) -> tuple[list[str], list[str]]:
    """Console/page-error sinks; the one deliberate 400 these flows provoke is filtered (the
    house pattern — see test_ledger_correction_doors_flow), everything else still fails."""
    console_errors: list[str] = []
    page_errors: list[str] = []

    def _console(m: Any) -> None:
        if getattr(m, "type", None) != "error":
            return
        text = getattr(m, "text", "")
        if "Failed to load resource" in text and ("400" in text or "422" in text):
            return
        console_errors.append(text)

    page.on("console", _console)
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    return console_errors, page_errors


class _Hold:
    """Park every request of `method` under `pattern`; the test releases them by hand."""

    def __init__(self, page: Page, pattern: str, method: str) -> None:
        self.parked: list[Route] = []
        self.seen = 0
        self.method = method
        page.route(pattern, self._handle)

    def _handle(self, route: Route) -> None:
        if route.request.method != self.method:
            route.continue_()
            return
        self.seen += 1
        self.parked.append(route)

    def wait_parked(self, page: Page, n: int) -> None:
        # wait_for_timeout pumps the sync dispatcher, which is what delivers the route
        for _ in range(200):
            if len(self.parked) >= n:
                return
            page.wait_for_timeout(25)
        raise AssertionError(f"expected {n} parked {self.method}, saw {len(self.parked)}")

    def release_all(self) -> None:
        for r in self.parked:
            r.continue_()
        self.parked.clear()


@pytest.mark.e2e
def test_edit_modal_stays_busy_until_the_put_settles_and_fires_once(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """(a) the modal is still there and 儲存 is disabled + busy while the PUT is in flight;
    (b) a second click during that window sends NO second PUT;
    (d) a refused save (400) leaves the modal open with the user's values, button restored;
    and the success toast appears only once the modal has closed."""
    base = flow_server(_seed_golden)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#tx-body tr.expandable")
    page.locator("#tx-body tr.expandable").first.locator(".wl-actions .btn").first.click()
    page.wait_for_selector(".modal-title:has-text('編輯交易')")
    sym_input = page.locator(".modal-body .field:nth-child(3) input")
    original_symbol = sym_input.input_value()
    save = page.locator(".modal-foot .btn-primary")

    # ---- (d): a refused save keeps the modal, the values and a usable button ------------
    with page.expect_response("**/api/input/manual/preview"):
        sym_input.fill("ZZZZ")
        sym_input.press("Tab")
    with page.expect_response("**/api/ledgers/transactions/**") as refused:
        save.click()
    assert refused.value.status == 400
    page.wait_for_selector(".toast-fail")
    assert page.locator(".modal-title:has-text('編輯交易')").count() == 1, (
        "a refused save must leave the edit modal open for the user to correct it")
    assert sym_input.input_value() == "ZZZZ", "the user's values must survive the refusal"
    assert not save.is_disabled() and save.inner_text().strip() == "儲存", (
        "儲存 must be restored after a failure, not left busy")
    with page.expect_response("**/api/input/manual/preview"):
        sym_input.fill(original_symbol)
        sym_input.press("Tab")

    # ---- (a) + (b): hold the PUT and look at the page while it is in flight -------------
    hold = _Hold(page, "**/api/ledgers/transactions/**", "PUT")
    save.click()
    hold.wait_parked(page, 1)
    assert page.locator(".modal-title:has-text('編輯交易')").count() == 1, (
        "the edit modal must stay open while the PUT is in flight")
    assert save.is_disabled(), "儲存 must be disabled while the PUT is in flight"
    assert save.locator(".busy-spin").count() == 1 and "儲存中" in save.inner_text(), (
        f"儲存 must show a busy state, got {save.inner_text()!r}")
    # a disabled button receives no click — the request count is the proof
    save.click(force=True, timeout=2000)
    page.wait_for_timeout(300)
    assert hold.seen == 1, f"a second click during the in-flight window sent {hold.seen} PUTs"

    # ---- release: the modal closes, the tables rebuild, THEN the toast ---------------------
    hold.release_all()
    page.wait_for_selector(".toast-ok")
    assert page.locator(".modal-backdrop").count() == 0, "the modal must close on success"
    assert hold.seen == 1

    assert not console_errors and not page_errors, (
        f"edit busy flow: console={console_errors!r} page={page_errors!r}")


@pytest.mark.e2e
def test_delete_shows_progress_and_row_buttons_are_inert_while_in_flight(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """(c) a delete in flight shows a progress toast and every row button is a no-op; the
    success toast appears only after the table no longer holds the deleted row."""
    base = flow_server(_seed_golden)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    page.goto(base + "/trades.html", wait_until="load")
    page.click("#tab-ldiv")
    page.wait_for_selector("#div-body tr")
    before = page.locator("#div-body tr").count()
    assert before >= 1, "golden must hold at least one dividend"

    hold = _Hold(page, "**/api/ledgers/dividends/**", "DELETE")
    page.locator("#div-body tr").first.locator(".btn-row-del").click()
    page.wait_for_selector(".modal-title:has-text('刪除股利')")
    page.click(".modal-foot .btn-danger")
    hold.wait_parked(page, 1)

    # ---- (c): the in-flight window is visible, and the row buttons are inert ------------
    page.wait_for_selector(".toast-progress:has-text('刪除中')", timeout=3000)
    assert page.locator(".modal-backdrop").count() == 0
    page.locator("#div-body tr").first.locator(".wl-actions .btn").first.click()
    page.locator("#div-body tr").first.locator(".btn-row-del").click()
    page.wait_for_timeout(300)
    assert page.locator(".modal-backdrop").count() == 0, (
        "row buttons must be a no-op while a mutation is in flight (no modal may open)")
    assert hold.seen == 1, f"row buttons in flight fired {hold.seen} DELETEs"

    # ---- release: the row is gone BEFORE 「刪除完成」 says so ------------------------------
    hold.release_all()
    page.wait_for_selector(".toast-ok:has-text('刪除完成')")
    assert page.locator("#div-body tr").count() == before - 1, (
        "the success toast must not appear while the deleted row is still on screen")
    assert _get_json(base, "/api/ledgers/dividends?limit=500")["total_count"] == before - 1
    assert page.locator(".toast-progress").count() == 0

    assert not console_errors and not page_errors, (
        f"delete progress flow: console={console_errors!r} page={page_errors!r}")


@pytest.mark.e2e
def test_edit_ack_dialog_opens_only_after_the_edit_modal_closed(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """The ORDER pin: on an ack-able 422 the edit modal is closed BEFORE the ack dialog
    opens, never left open beside it. ``test_ledger_correction_doors_flow`` clicks the FIRST
    ``.modal-foot .btn-danger`` it finds on the delete door; this is the same guarantee on
    the edit door, where the modal now survives the request."""
    base = flow_server(_seed_golden)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    # a later SELL of the whole AAPL lot, so shrinking the BUY strands it -> 422 oversell
    status, _ = _post_json(base, "/api/input/manual/commit", {
        "account_id": "schwab", "symbol": "AAPL", "side": "sell",
        "date": "2026-02-01", "shares": "10", "price": "110"})
    assert status == 201

    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#tx-body tr.expandable")
    buy_row = page.locator("#tx-body tr.expandable").filter(has_text="AAPL").filter(
        has_text="買")
    assert buy_row.count() == 1
    buy_row.locator(".wl-actions .btn").first.click()
    page.wait_for_selector(".modal-title:has-text('編輯交易')")
    with page.expect_response("**/api/input/manual/preview"):
        page.fill(".modal-body .field:nth-child(5) input", "5")

    with page.expect_response("**/api/ledgers/transactions/**") as first:
        page.click(".modal-foot .btn-primary")
    assert first.value.status == 422
    page.wait_for_selector(".modal-title:has-text('賣超確認')")
    assert page.locator(".modal-backdrop").count() == 1, (
        "exactly ONE dialog: the edit modal must be closed before the ack dialog opens")
    assert page.locator(".modal-title").inner_text() == "賣超確認"

    with page.expect_response("**/api/ledgers/transactions/**") as second:
        page.click(".modal-foot .btn-danger")
    assert second.value.status == 200, second.value.text()
    page.wait_for_selector(".toast-ok:has-text('編輯完成')")
    assert page.locator(".modal-backdrop").count() == 0

    assert not console_errors and not page_errors, (
        f"edit ack order flow: console={console_errors!r} page={page_errors!r}")
