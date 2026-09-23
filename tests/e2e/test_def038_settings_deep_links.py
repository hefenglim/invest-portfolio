"""DEF-038 ② (2026-09-23): 洞察管線's asset cards land ON their block in 系統設定.

The verifier clicked 「進化設定・5 項安全邊界」 and arrived on 帳戶與費率 — the card linked to bare
``settings.html``. The fix links ``settings.html#prompts/evolution`` and teaches settings.html's
router to scroll to the element carrying ``data-anchor`` inside the tab. 自我進化設定 is built
ASYNCHRONOUSLY (after settings-prompts.js's fetches), so this clicks the real card and checks
where the panel actually is in the viewport, not just what the URL says.
"""

import pytest
from playwright.sync_api import Page, expect

_IN_VIEW = """
(sel) => { const n = document.querySelector(sel); if (!n) return null;
           const r = n.getBoundingClientRect();
           return {top: Math.round(r.top), h: Math.round(r.height), vh: window.innerHeight}; }
"""


@pytest.mark.e2e
@pytest.mark.parametrize(
    ("card", "selector", "is_details"),
    [
        ("進化設定", "#view-prompts [data-anchor='evolution']", False),
        ("數據變數庫", "#vars-panel", True),
        ("分析模板庫", "#view-prompts [data-anchor='templates']", False),
    ],
)
def test_the_asset_card_lands_on_its_block(
    live_server: str, browser_page: Page, card: str, selector: str, is_details: bool
) -> None:
    page = browser_page
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto("about:blank")
    page.goto(f"{live_server}/pipeline-hub.html", wait_until="networkidle")
    page.click(f".pp-asset:has-text('{card}')")
    page.wait_for_url("**/settings.html#prompts/**")
    expect(page.locator(".set-tab.active")).to_have_text("AI 提示詞")
    page.wait_for_selector(selector, state="attached")
    page.wait_for_timeout(1500)                     # the router's settle re-asserts land
    pos = page.evaluate(_IN_VIEW, selector)
    assert pos is not None
    assert pos["h"] > 0, f"「{card}」: the target block is not displayed ({pos})"
    assert 0 <= pos["top"] < pos["vh"] - 80, (
        f"「{card}」 landed with its block at top={pos['top']}px of a {pos['vh']}px viewport")
    if is_details:
        assert page.evaluate(f"() => document.querySelector(\"{selector}\").open") is True
