"""E2E (Playwright, real server + real frontend) — CR-07: the corporate-action form's
「我已確認上述警告」 tick belongs to the warnings it acknowledged, and only the newest preview may
paint.

Before: ``state.acked`` was reset only by the E23 one-click, so a tick survived any later edit
of symbol, ratio, date or cost carry — the next preview rendered pre-ticked and 登錄 posted
``ack_warnings=true`` for a warning nobody read; and ``runPreview`` had no sequence guard, so
a slow response for an older value could overwrite the preview of the newer one.

The warning used is D44's (a target band set before the split): its sentence carries the
ratio, so changing 1→2 into 1→4 changes the warning.
"""

import json
from collections.abc import Iterator

import pytest
from playwright.sync_api import Locator, Page, Request, Route, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from tests.e2e.conftest import FlowServerFactory
from tests.e2e.test_band_and_spinoff_flow import (
    ACTION_DAY,
    _ack_checkbox,
    _date_box,
    _kind_button,
    _open_form_from_door3,
    _ratio_from,
    _ratio_to,
    _save_button,
    _seed_banded_position,
    _symbol_box,
)

_PREVIEW = "**/api/ledgers/corporate-actions/preview"


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _fill_split_identity(page: Page, base: str) -> Locator:
    modal = _open_form_from_door3(page, base)
    _kind_button(modal, 0).click()
    modal.locator(".ca-grid select").select_option("tw_broker")
    _symbol_box(modal).fill("2330")
    _date_box(modal).fill(ACTION_DAY.isoformat())
    _ratio_from(modal).fill("1")
    return modal


@pytest.mark.e2e
def test_cr07_a_tick_does_not_survive_a_change_of_the_warnings(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_banded_position)
    page = fresh_page
    modal = _fill_split_identity(page, base)
    with page.expect_response(_PREVIEW) as prev:
        _ratio_to(modal).fill("2")
    assert prev.value.status == 200, prev.value.text()
    ack = _ack_checkbox(modal)
    expect(ack).to_be_visible()
    expect(ack).not_to_be_checked()
    expect(_save_button(modal)).to_be_disabled()
    ack.check()
    expect(_save_button(modal)).to_be_enabled()

    # Another ratio → another warning sentence → a fresh question.
    with page.expect_response(_PREVIEW) as prev2:
        _ratio_to(modal).fill("4")
    assert prev2.value.status == 200, prev2.value.text()
    expect(modal.locator(".ca-issue").first).to_contain_text("4 比 1")
    expect(_ack_checkbox(modal)).not_to_be_checked()          # pre-fix: still ticked
    expect(_save_button(modal)).to_be_disabled()              # pre-fix: enabled

    _ack_checkbox(modal).check()
    with page.expect_request("**/api/ledgers/corporate-actions") as sent, \
            page.expect_response("**/api/ledgers/corporate-actions") as saved:
        _save_button(modal).click()
    assert saved.value.status == 201, saved.value.text()
    posted = json.loads(sent.value.post_data or "{}")
    assert posted["ack_warnings"] is True
    acked = {(w["code"], w["text"]) for w in posted["acked_warnings"]}
    shown = {(i["code"], i["text"]) for i in prev2.value.json()["issues"]
             if i["sev"] != "error"}
    assert acked == shown                                     # exactly what was on screen


@pytest.mark.e2e
def test_cr07_a_slow_older_preview_never_overwrites_a_newer_one(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_banded_position)
    page = fresh_page
    modal = _fill_split_identity(page, base)
    held: list[Route] = []

    def _hold_first(route: Route) -> None:
        body = json.loads(route.request.post_data or "{}")
        if body.get("ratio_to") == "2" and not held:
            held.append(route)                  # answered later, after the newer one
            return
        route.continue_()

    page.route(_PREVIEW, _hold_first)
    _ratio_to(modal).fill("2")
    for _ in range(100):                        # the 250 ms debounce, then the held request
        if held:
            break
        page.wait_for_timeout(50)
    assert held, "the ratio-2 preview was never sent"

    with page.expect_response(_PREVIEW) as newer:
        _ratio_to(modal).fill("4")
    assert newer.value.status == 200
    preview = modal.locator(".ca-preview")
    expect(preview).to_contain_text("4,000")                   # 1,000 × 4

    def _is_ratio_2(req: Request) -> bool:
        body: dict[str, object] = json.loads(req.post_data or "{}")
        return body.get("ratio_to") == "2"

    with page.expect_response(lambda r: "preview" in r.url and _is_ratio_2(r.request)):
        held[0].continue_()
    page.wait_for_timeout(500)                   # give the stale answer its chance to paint
    expect(preview).to_contain_text("4,000")
    expect(preview).not_to_contain_text("2,000")               # pre-fix: the ratio-2 preview
    expect(_ratio_to(modal)).to_have_value("4")
