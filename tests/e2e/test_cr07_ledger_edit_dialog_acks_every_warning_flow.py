"""E2E (Playwright, real server + real frontend) — CR-07's class on the 公司行動 ledger tab's
EDIT dialog (web/ledger.js), the door the form fix (corp-action-form.js) did not cover.

Before: a 422 ``warnings_unacknowledged`` opened 「公司行動警告確認」 with ``err.message`` — the
FIRST warning only — and 「我了解，仍要儲存」 re-sent a bare ``ack_warnings=true``, so the owner
acknowledged every other warning without reading it, and the server could not tell. Now the
dialog lists every warning the server found and the re-send names the (code, text) pairs it
showed (``acked_warnings``), which ``_ack_refusal`` checks against what it recomputes.

The warning used is D44's (a target band set before the split), as in the form's CR-07 flow.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from playwright.sync_api import Page, Request, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from tests.e2e.conftest import FlowServerFactory
from tests.e2e.test_band_and_spinoff_flow import ACTION_DAY, _seed_banded_position


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _is_action_put(r: Any) -> bool:
    req = r if isinstance(r, Request) else r.request
    return req.method == "PUT" and "/api/ledgers/corporate-actions/" in req.url


@pytest.mark.e2e
def test_cr07_edit_dialog_lists_every_warning_and_acks_what_it_showed(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_banded_position)
    page = fresh_page
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_function("() => !!window.pdApi")
    page.evaluate("(b) => window.pdApi.post('/api/ledgers/corporate-actions', b)", {
        "account_id": "tw_broker", "date": ACTION_DAY.isoformat(), "kind": "SPLIT",
        "from_symbol": "2330", "to_symbol": "2330", "ratio_to": "2", "ratio_from": "1",
        "ack_warnings": True})
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_function("() => !!window.pdApi && !!window.pdAckConfirm")
    page.click("#tab-laction")
    row = page.locator("#action-body tr", has_text="2330")
    expect(row).to_have_count(1)

    sent: list[dict[str, Any]] = []
    page.on("request", lambda r: sent.append(r.post_data_json) if _is_action_put(r) else None)

    row.locator("button", has_text="編輯").click()
    modal = page.locator(".modal-backdrop .modal", has_text="編輯公司行動")
    modal.locator(".field", has_text="變成／換得（股）").locator("input").fill("4")
    with page.expect_response(_is_action_put) as first:
        modal.locator("button", has_text="儲存").click()
    refused = first.value.json()["error"]
    assert first.value.status == 422 and refused["code"] == "warnings_unacknowledged", refused
    shown = [{"code": i["code"], "text": i["text"]}
             for i in refused.get("issues") or [] if i["sev"] != "error"]
    assert shown, refused

    warn = page.locator(".modal-backdrop .modal", has_text="公司行動警告確認")
    expect(warn).to_be_visible()
    for item in shown:                               # pre-fix: only issues[0].message
        expect(warn).to_contain_text(item["text"])

    with page.expect_response(_is_action_put) as second:
        warn.locator("button", has_text="我了解，仍要儲存").click()
    assert second.value.status == 200, second.value.text()
    assert len(sent) == 2, sent
    assert sent[1]["ack_warnings"] is True
    assert sent[1].get("acked_warnings") == shown, sent[1]   # pre-fix: absent (bare flag)
    page.wait_for_function(
        "async () => (await window.pdApi.get('/api/ledgers/corporate-actions'))"
        ".rows[0].ratio_to === '4'")
