"""The re-settle prompt is re-armed by a write, never by the settlement's own controls (code
review of v0.1.32, 2026-10-20; PERF spec §14.3).

``web/api.js`` announces every successful non-GET as ``pd-mutated``, and ``web/settlement.js``
re-arms the prompt on it. The page's own ``POST /api/settlement/run`` and
``POST /api/settlement/cancel`` are such writes: after a CANCEL the restored dirty day makes
``needs_prompt`` true again, and 「要重新結算歷史資料嗎？」 popped right after the owner said
stop. The settlement API is stubbed on ``fresh_page`` only (``tests/e2e/settlement_stubs.py``,
E's real shapes), on the session ``live_server``.
"""

from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e.settlement_stubs import SettleStub, status, stubbed, table

#: Past settlement.js's MUTATED_WAIT_MS (800 ms) debounce and the status read it starts.
_SETTLE_MS = 2000


def _pending_with_prompt() -> dict[str, Any]:
    """What the status reads after a cancel put a claimed change back: pending, asked about."""
    return status(
        "pending", dirty_from="2025-06-16", stale_from="2025-06-16", needs_prompt=True,
        seq=7, prompt={"from": "2025-06-16", "days": 360},
        pending={"tables": [table("transactions", 2, "2025-06-16", "2025-06-18")],
                 "records_total": 2})


@pytest.mark.e2e
def test_a_settlement_control_never_rearms_the_prompt_but_any_other_write_does(
    live_server: str, fresh_page: Page
) -> None:
    page = fresh_page
    stub = SettleStub(status("clean"))
    other_writes: list[str] = []

    def other_write(route: Route) -> None:
        other_writes.append(route.request.method)
        route.fulfill(status=200, content_type="application/json", body="{}")

    with stubbed(page, stub, extra=[("**/api/e2e-write-probe", other_write)]):
        page.set_viewport_size({"width": 1440, "height": 900})
        page.goto(live_server + "/instruments.html", wait_until="load")
        expect(page.locator("#settle-chip")).to_be_visible()   # the boot read is done
        stub.status = _pending_with_prompt()
        reads = stub.count("/settlement/status")

        # 取消 and 立即重新結算 through the page's own fetch layer: pd-mutated fires for both
        page.evaluate("() => window.pdApi.post('/api/settlement/cancel', {})")
        page.evaluate("() => window.pdApi.post('/api/settlement/run', {scope: 'dirty'})")
        page.wait_for_timeout(_SETTLE_MS)
        assert [p for p, _b in stub.posts] == ["/api/settlement/cancel", "/api/settlement/run"]
        assert stub.count("/settlement/status") == reads, "a control re-read the status"
        assert page.locator(".modal.st-prompt").count() == 0
        # a status read of their own (the panel's poll, startRun / cancelRun) asks nothing
        page.evaluate("() => window.pdSettlement.refresh()")
        page.wait_for_timeout(500)
        assert page.locator(".modal.st-prompt").count() == 0

        # any other write still re-arms it: the same status now asks
        page.evaluate("() => window.pdApi.post('/api/e2e-write-probe', {})")
        prompt = page.locator(".modal.st-prompt")
        expect(prompt).to_be_visible(timeout=10_000)
        expect(prompt).to_contain_text("要重新結算歷史資料嗎？")
        expect(prompt).to_contain_text("2025-06-16 起")
        assert other_writes == ["POST"]
        prompt.get_by_role("button", name="稍後").click()
        expect(prompt).to_have_count(0)
