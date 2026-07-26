"""E2E: the ✦ 新功能 badge + panel (per-feature seen) + 版本發佈資訊 history browser (WP-WN).

Runs against its OWN isolated uvicorn subprocess (guest mode, fresh whatsnew state), so
the seen writes do not pollute other tests. Mirrors the E1-E10 flow style
(tests/e2e/test_flows_e1_e10.py): expect-polling, never sleeps.
"""

from collections.abc import Iterator

import pytest
from playwright.sync_api import Page
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Re-enable loopback sockets per test (pytest-socket re-bans before each test)."""
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _pick_feature(page: Page) -> dict[str, str]:
    """A feature that is CURRENTLY in the ✦ panel window and deep-links into a settings tab.

    The panel renders only the six most recent versions, so any hard-coded ``version:id``
    eventually falls out of it and every locator built on that key times out — that is how
    both flows below broke after v0.1.23 shipped (v0.1.17 became the seventh version). This
    reads the live payload the panel itself renders and returns the first feature whose
    ``href`` is a ``settings.html#<tab>`` deep link WITH a blink ``target``, so the flows
    keep asserting exactly what they always did without pinning a catalog entry.
    """
    payload = page.evaluate(
        "async () => (await fetch('/api/whats-new', {credentials:'same-origin'})).json()"
    )
    for group in payload.get("groups") or payload.get("versions") or []:
        for f in group.get("features") or []:
            href, target = f.get("href") or "", f.get("target") or ""
            if href.startswith("settings.html#") and target:
                return {"key": f"{group['version']}:{f['id']}", "href": href,
                        "target": target, "title": f.get("title") or ""}
    raise AssertionError(
        "no settings-tab feature with a target is visible in the ✦ panel window; "
        "the whats-new catalog needs one for these flows to exercise the deep link"
    )


@pytest.mark.e2e
def test_whatsnew_per_feature_seen_and_persist(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """Fresh DB -> dot shows. Opening does NOT ack. 前往 clears only that feature's NEW
    (dot persists while others are unread). 全部標示已讀 clears the dot; a reload keeps it."""
    base = flow_server(_seed_golden)
    page = fresh_page
    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    # Fresh install: the ✦ button shows the unseen dot after whatsnew init.
    page.goto(base + "/index.html", wait_until="load")
    page.wait_for_selector("#wn-btn")
    page.wait_for_selector("#wn-btn .wn-dot")

    # Open the panel: version groups + at least one 前往 render. Opening does NOT ack, so
    # the dot is STILL present (round-3 change from the old open-acks-everything behaviour).
    page.click("#wn-btn")
    page.wait_for_selector(".wn-backdrop .wn-group")
    assert page.locator(".wn-backdrop .wn-go").count() > 0
    assert page.locator(".wn-backdrop .wn-new-pill").count() > 0
    assert page.locator("#wn-btn .wn-dot").count() == 1

    # Click a SPECIFIC 前往: marks THAT feature (not `.first` — a release bump prepends a
    # version group whose first feature may be href-less 「知道了」).
    #
    # The feature is DISCOVERED from the live payload, never hard-coded (2026-07-26): the ✦
    # panel shows only the most recent SIX versions, so any pinned `version:id` inevitably
    # ages out of the window and the locator starts timing out — which is exactly how this
    # test broke once v0.1.23 pushed v0.1.17 off the panel. Picking a still-visible feature
    # keeps the assertion identical while making it immune to the release cadence.
    feat = _pick_feature(page)
    first_key = feat["key"]
    row = page.locator('.wn-backdrop .wn-feat[data-wn-key="' + first_key + '"]')
    row.locator(".wn-go").click()
    page.wait_for_url("**/" + feat["href"])

    # The dot persists (other features are still unread). Reopen the panel: the clicked
    # feature's row has NO NEW pill, while other rows still do.
    page.wait_for_selector("#wn-btn .wn-dot")
    page.click("#wn-btn")
    page.wait_for_selector(".wn-backdrop .wn-group")
    seen_row = page.locator('.wn-backdrop .wn-feat[data-wn-key="' + first_key + '"]')
    seen_row.wait_for(state="visible")
    assert seen_row.locator(".wn-new-pill").count() == 0
    assert page.locator(".wn-backdrop .wn-new-pill").count() > 0  # others still unread

    # 全部標示已讀 clears every pill and the ambient dot.
    page.click(".wn-backdrop .wn-foot button")
    page.wait_for_selector("#wn-btn .wn-dot", state="detached")
    assert page.locator(".wn-backdrop .wn-new-pill").count() == 0

    # Reload: the acknowledgement persisted, so the dot does not come back.
    page.goto(base + "/settings.html", wait_until="load")
    page.wait_for_selector("#wn-btn")
    page.wait_for_load_state("networkidle")
    assert page.locator("#wn-btn .wn-dot").count() == 0

    assert not page_errors, page_errors


@pytest.mark.e2e
def test_whatsnew_callout_arrival_and_cancel_on_switch(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """前往 -> in-page callout + blink on the target; a tab switch cancels both and they do
    not resurface on switch-back (only a fresh 前往 re-arms)."""
    base = flow_server(_seed_golden)
    page = fresh_page
    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    # Open the panel and click a settings-tab 前往, DISCOVERED from the live payload rather
    # than pinned to a version:id (see the note in the test above — the ✦ panel keeps only
    # the last six versions, so a pinned key ages out and the locator times out).
    page.goto(base + "/index.html", wait_until="load")
    page.wait_for_selector("#wn-btn")
    page.click("#wn-btn")
    page.wait_for_selector(".wn-backdrop .wn-go")
    feat = _pick_feature(page)
    tab = feat["href"].split("#", 1)[1]
    other_tab = "accounts" if tab != "accounts" else "notify"
    page.locator('.wn-backdrop .wn-feat[data-wn-key="' + feat["key"] + '"]') \
        .locator(".wn-go").click()

    # Arrives on the feature's tab: the callout is visible with the feature title, and the
    # blink wraps the element the catalog entry points at.
    page.wait_for_url("**/" + feat["href"])
    callout = page.locator(".wn-callout")
    callout.wait_for(state="visible")
    assert callout.locator(".wn-callout-title").inner_text() == feat["title"]
    # The blink lands on the catalog target's enclosing `section.panel` when it has one, else
    # on the target element itself (`_resolveAnchor`), so assert the relationship rather than
    # a fixed descendant selector — otherwise the check silently depends on which feature the
    # panel window happens to be showing.
    page.wait_for_selector(".wn-flash")
    assert page.evaluate(
        "(sel) => { const f = document.querySelector('.wn-flash');"
        " const t = document.querySelector(sel);"
        " return !!(f && t && (f === t || f.contains(t))); }",
        feat["target"],
    ), f"the blink did not land on (or around) {feat['target']}"

    # Switch tab (hashchange): callout + flash vanish immediately.
    page.evaluate("window.location.hash = '" + other_tab + "'")
    page.wait_for_selector(".wn-callout", state="detached")
    page.wait_for_selector(".wn-flash", state="detached")

    # Switch back: they do NOT resurface.
    page.evaluate("window.location.hash = '" + tab + "'")
    page.wait_for_load_state("networkidle")
    assert page.locator(".wn-callout").count() == 0
    assert page.locator(".wn-flash").count() == 0

    assert not page_errors, page_errors


@pytest.mark.e2e
def test_whatsnew_history_browser_opens_from_settings(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """settings 一般 -> 版本發佈資訊 button -> history modal renders the first page of groups."""
    base = flow_server(_seed_golden)
    page = fresh_page
    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    page.goto(base + "/settings.html", wait_until="load")
    page.wait_for_selector("#gen-whatsnew")
    page.click("#gen-whatsnew")
    page.wait_for_selector(".wnh-backdrop .wnh-group")
    assert page.locator(".wnh-backdrop .wnh-group").count() > 0
    # a "載入更早版本" pager button exists (the catalog has more than one page of versions).
    assert page.locator(".wnh-backdrop .wnh-foot button").count() == 1

    assert not page_errors, page_errors
