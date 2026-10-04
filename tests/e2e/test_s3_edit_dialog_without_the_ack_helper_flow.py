"""E2E (Playwright, real server + real frontend) — S3 of the round-3 review: the corporate-action
EDIT dialog survives a page whose ack helper is missing.

``web/ledger.js`` ``warnAck`` called ``window.pdActionAckKeys`` (R12's ONE helper, defined by
``web/corp-action-form.js``) unguarded — and only AFTER ``before()`` had closed the edit modal.
A browser holding a cached pre-R12 corp-action-form.js beside the new ledger.js (asset URLs keep
``?v=0.1.28`` until a version bump) therefore threw inside the save's error handler: no dialog,
no toast, and the modal with the owner's typed values already gone.

Now the helper is checked BEFORE anything is closed; without it the dialog says to reload and
the modal stays, values intact. Simulated by deleting the global — exactly what an old
corp-action-form.js leaves undefined.
"""

from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from tests.e2e.conftest import FlowServerFactory
from tests.e2e.test_r1_r2_action_ack_account_warning_flow import _is_action_put, _seed_with_split


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


@pytest.mark.e2e
def test_s3_a_missing_ack_helper_keeps_the_modal_and_asks_for_a_reload(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_with_split)
    page = fresh_page
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_function("() => !!window.pdApi && !!window.pdAckConfirm")
    page.evaluate("() => { delete window.pdActionAckKeys; }")   # an old corp-action-form.js
    page.click("#tab-laction")
    row = page.locator("#action-body tr", has_text="2330")
    expect(row).to_have_count(1)
    row.locator("button", has_text="編輯").click()
    modal = page.locator(".modal-backdrop .modal", has_text="編輯公司行動")
    ratio = modal.locator(".field", has_text="變成／換得（股）").locator("input")
    ratio.fill("3")
    with page.expect_response(_is_action_put) as first:
        modal.locator("button", has_text="儲存").click()
    assert first.value.status == 422
    assert first.value.json()["error"]["code"] == "warnings_unacknowledged"

    expect(page.locator(".toast").filter(has_text="重新整理")).to_be_visible()
    expect(modal).to_be_visible()                  # pre-fix: closed by before(), then a throw
    expect(ratio).to_have_value("3")               # the owner's typed value is still there
    expect(page.locator(".modal-backdrop .modal", has_text="公司行動警告確認")).to_have_count(0)
    assert page_errors == [], page_errors          # pre-fix: TypeError pdActionAckKeys
