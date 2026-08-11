"""The e2e browser talks to the app under test and to NOTHING else (issue #67).

Why this file exists. Every shipped page loads its webfont from `fonts.googleapis.com`
(which fans out to 17-24 `fonts.gstatic.com` subset files) and ECharts from
`cdn.jsdelivr.net`. Google serves that stylesheet `stale-while-revalidate=604800` and
rotates the subset filenames underneath it, so a slightly stale stylesheet asks for files
Google has already retired and EVERY font subset on the page 404s at once. Because the
suite asserts ZERO browser console errors, that remote rotation reddened ~2 random tests
per full run — a different pair every time, never reproducible on a targeted re-run.

`tests/e2e/conftest.py::install_third_party_stub` removes the dependency. These two tests
keep it removed: one proves the behaviour (no non-loopback request leaves the browser with
anything but a 200 stub, and the gstatic fan-out is gone), the other proves the coverage
(every browser context built in tests/e2e is routed).
"""

import ast
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import ConsoleMessage, Page, Request, Response

from tests.e2e.conftest import _is_app_url

_STUB_CALL = "install_third_party_stub"


@pytest.mark.e2e
def test_page_load_makes_no_unstubbed_third_party_request(
    live_server: str, fresh_page: Page
) -> None:
    """index.html — the heaviest page (webfont + ECharts) — reaches no third party.

    Asserts the three things the flake needed in order to happen: (a) every non-loopback
    response is a 200 from the stub, so a remote 404 can never reach the console; (b) NO
    request is made to fonts.gstatic.com at all — the empty stylesheet kills the whole
    subset fan-out at its source; (c) zero console errors, the assertion the flake broke.
    """
    page = fresh_page
    third_party: list[str] = []
    failures: list[str] = []
    console_errors: list[str] = []

    def _on_response(resp: Response) -> None:
        if not _is_app_url(resp.url):
            third_party.append(f"{resp.status} {resp.url}")

    def _on_requestfailed(req: Request) -> None:
        if not _is_app_url(req.url):
            failures.append(f"{req.failure} {req.url}")

    def _on_console(msg: ConsoleMessage) -> None:
        if msg.type == "error":
            console_errors.append(msg.text)

    page.on("response", _on_response)
    page.on("requestfailed", _on_requestfailed)
    page.on("console", _on_console)
    page.goto(live_server + "/index.html", wait_until="load")
    page.wait_for_timeout(1_500)  # let the async chart/asset tail settle

    non_200 = [row for row in third_party if not row.startswith("200 ")]
    assert not non_200, f"a third-party response escaped the stub: {non_200}"
    assert not failures, f"a third-party request failed at the network layer: {failures}"

    gstatic = [row for row in third_party if urlparse(row.split(" ", 1)[1]).netloc
               == "fonts.gstatic.com"]
    assert not gstatic, (
        "the webfont subset fan-out is back — the stylesheet stub is not empty: "
        f"{gstatic[:3]} ({len(gstatic)} requests)"
    )
    assert not console_errors, f"/index.html console errors: {console_errors}"


def _creates_a_context(node: ast.Call) -> bool:
    """`<x>.new_context(...)`, or `<...browser>.new_page(...)` (which makes one implicitly)."""
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr == "new_context":
        return True
    if func.attr != "new_page":
        return False
    receiver = func.value
    if isinstance(receiver, ast.Name):
        return receiver.id.endswith("browser")
    return isinstance(receiver, ast.Attribute) and receiver.attr.endswith("browser")


def test_every_browser_context_in_e2e_installs_the_stub() -> None:
    """A context created without the stub silently re-adds a CDN to the suite's verdict.

    A STATIC guard, because the failure it prevents is a NEW e2e file quietly building its
    own context — which no runtime test would ever execute. It reads the AST rather than
    the text so that prose mentioning the call (this file, and the conftest brief) is not
    mistaken for a call site; the earlier line-grep version flagged its own docstring while
    a real unstubbed context two files away sat at the same time (both fixed 2026-08-11).
    """
    offenders: list[str] = []
    for path in sorted(Path(__file__).resolve().parent.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for scope in [tree, *[n for n in ast.walk(tree)
                             if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]]:
            calls = [n for n in ast.walk(scope) if isinstance(n, ast.Call)]
            creators = [n for n in calls if _creates_a_context(n)]
            if not creators:
                continue
            stubbed = any(
                (isinstance(n.func, ast.Name) and n.func.id == _STUB_CALL)
                or (isinstance(n.func, ast.Attribute) and n.func.attr == _STUB_CALL)
                for n in calls
            )
            if not stubbed:
                offenders.append(f"{path.name}:{creators[0].lineno}")
    assert not offenders, (
        f"browser context(s) built without {_STUB_CALL}(...) — see tests/e2e/conftest.py: "
        f"{sorted(set(offenders))}"
    )
