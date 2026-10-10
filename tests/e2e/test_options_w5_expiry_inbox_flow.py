"""E2E options W5 — the 收件匣 › 待確認到期 panel (spec §7.2), against a STUBBED route set.

The backend (``api/option_expiry.py`` + its router) is built in the same wave (W6) and wired
into ``api/app.py`` only in W7b, so this file pins the panel to the spec §7.2 contract with
``page.route`` stubs: the panel's reads, every write it sends (confirm / confirm-all / skip /
unskip — each body asserted), the per-item results of 全部確認 through the in-page confirm
dialog, a 待釐清 item that offers its remedy instead of 確認到期作廢, the deep link to the manual
form, and the sidebar badge adding the expiry count. W7b runs the same flows against the real
routes.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from playwright.sync_api import Page, Route
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory

PUT = "AAPL261002P00100000"
CALL = "AAPL261002C00150000"
OLD = "AAPL260918P00090000"

_ITEMS = [
    {"account_id": "schwab", "account_name": "Charles Schwab", "symbol": PUT,
     "contract": "AAPL 2026-10-02 100 Put", "direction": "short", "contracts": "2",
     "expiry": "2026-10-02", "days_past": 8, "premium_text": "權利金 400.00 USD 全數實現",
     "text": "{account:schwab} AAPL 2026-10-02 100 Put・賣方 2 口・到期日已過 8 天 → 到期作廢，"
             "權利金 400.00 USD 全數實現",
     "confirmable": True, "remedy": None},
    {"account_id": "schwab", "account_name": "Charles Schwab", "symbol": CALL,
     "contract": "AAPL 2026-10-02 150 Call", "direction": "long", "contracts": "1",
     "expiry": "2026-10-02", "days_past": 8, "premium_text": "權利金 250.00 USD 全數損失",
     "text": "{account:schwab} AAPL 2026-10-02 150 Call・買方 1 口・到期日已過 8 天 → 到期作廢，"
             "權利金 250.00 USD 全數損失",
     "confirmable": False,
     "remedy": "刪除該筆到期作廢／被指派／履約列，或補回被刪除的交易"},
]
_SKIPPED = [{"account_id": "schwab", "account_name": "Charles Schwab", "symbol": OLD,
             "contract": "AAPL 2026-09-18 90 Put", "expiry": "2026-09-18",
             "skipped_at": "2026-09-20T10:00:00"}]


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _stub(page: Page) -> list[tuple[str, str, Any]]:
    """The spec §7.2 routes, answered from fixtures; every request is recorded."""
    sent: list[tuple[str, str, Any]] = []

    def handle(route: Route) -> None:
        req = route.request
        path = req.url.split("/api/option-expiry", 1)[1].split("?", 1)[0]
        body = req.post_data_json if req.method == "POST" else None
        sent.append((req.method, path, body))
        if req.method == "GET" and path == "":
            out: Any = {"items": _ITEMS}
        elif path == "/count":
            out = {"count": len(_ITEMS)}
        elif path == "/skipped":
            out = {"items": _SKIPPED}
        elif path == "/confirm":
            out = {"ok": True, "txn_id": 99}
        elif path == "/confirm-all":
            out = {"results": [
                {"account_id": "schwab", "symbol": PUT, "ok": True, "txn_id": 100},
                {"account_id": "schwab", "symbol": CALL, "ok": False,
                 "error": "此合約已登錄到期作廢，請先修改或刪除該列"}]}
        else:   # /skip, /unskip
            out = {"ok": True}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(out))

    page.route("**/api/option-expiry**", handle)
    return sent


def _errors(page: Page) -> list[str]:
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    def _console(m: Any) -> None:
        if getattr(m, "type", None) == "error":
            errors.append(getattr(m, "text", ""))

    page.on("console", _console)
    return errors


@pytest.mark.e2e
def test_the_expiry_panel_renders_and_sends_the_spec_contract(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_golden)
    page = fresh_page
    errors = _errors(page)
    sent = _stub(page)
    page.goto(base + "/dividend-inbox.html", wait_until="load")
    page.wait_for_selector("#expiry-section:not([hidden]) .xp-item")

    assert page.inner_text("#expiry-count") == "2"
    items = page.locator("#expiry-list .xp-item")
    assert items.count() == 2
    first, second = items.nth(0).inner_text(), items.nth(1).inner_text()
    assert "{account:" not in first and "賣方 2 口" in first     # the token is resolved
    assert items.nth(0).locator(".xp-confirm").count() == 1
    # a 待釐清 item: the remedy instead of 確認到期作廢
    assert items.nth(1).locator(".xp-confirm").count() == 0
    assert "未入帳（待釐清）" in second and "刪除該筆到期作廢" in second
    # 改登錄被指派／履約 → the manual form prefilled (a sold contract is ASSIGNED, a bought
    # one EXERCISED — spec §4.1)
    href = items.nth(0).locator(".xp-assign").get_attribute("href") or ""
    assert "option_close=assigned" in href and f"symbol={PUT}" in href and "contracts=2" in href
    href2 = items.nth(1).locator(".xp-assign").get_attribute("href") or ""
    assert "option_close=exercised" in href2

    # the sidebar badge adds the expiry count
    page.wait_for_function(
        "() => { const b = document.querySelector('.sb-badge-alert');"
        " return !!b && (b.title || '').includes('選擇權到期 2'); }")

    # 確認到期作廢 → POST /confirm {account_id, symbol}
    items.nth(0).locator(".xp-confirm").click()
    page.wait_for_selector(".toast-ok")
    assert ("POST", "/confirm", {"account_id": "schwab", "symbol": PUT}) in sent

    # 全部確認 → the in-page dialog → POST /confirm-all → per-item results
    page.click("#expiry-confirm-all")
    page.wait_for_selector(".modal-backdrop .modal-foot .btn-primary")
    assert "AAPL 2026-10-02 100 Put" in page.inner_text(".modal-backdrop .modal-body")
    page.click(".modal-backdrop .modal-foot .btn-primary")
    page.wait_for_selector("#expiry-results:not([hidden]) .xp-result-fail")
    assert "此合約已登錄到期作廢" in page.inner_text("#expiry-results")
    assert any(m == "POST" and p == "/confirm-all" for m, p, _ in sent)

    # 略過 / 取消略過
    page.locator("#expiry-list .xp-item").nth(1).locator(".xp-skip").click()
    page.wait_for_function("() => document.querySelectorAll('.toast-ok').length > 0")
    assert ("POST", "/skip", {"account_id": "schwab", "symbol": CALL}) in sent
    page.click("#expiry-skipped > summary")
    page.click("#expiry-skipped-list .xp-unskip")
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll('.toast-ok .msg'))"
        ".some((n) => n.textContent === '已取消略過')")
    assert ("POST", "/unskip", {"account_id": "schwab", "symbol": OLD}) in sent
    assert not errors, errors


@pytest.mark.e2e
def test_the_panel_stays_hidden_when_nothing_has_expired(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """A stock-only ledger's inbox looks exactly as before: the panel is never shown."""
    base = flow_server(_seed_golden)
    page = fresh_page
    errors = _errors(page)

    def empty(route: Route) -> None:
        out: dict[str, Any] = (
            {"count": 0} if "/count" in route.request.url else {"items": []})
        route.fulfill(status=200, content_type="application/json", body=json.dumps(out))

    page.route("**/api/option-expiry**", empty)
    with page.expect_response("**/api/option-expiry/skipped"):
        page.goto(base + "/dividend-inbox.html", wait_until="load")
    page.wait_for_selector("#rebate-list")
    assert page.locator("#expiry-section").is_hidden()
    assert not errors, errors
