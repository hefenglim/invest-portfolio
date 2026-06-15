"""Baseline Playwright smoke (spec 19, Task 0.3): prove the harness end to end.

The harness (tests/e2e/conftest.py) serves the REAL app over a uvicorn subprocess
against a seeded golden DB and drives a headless chromium browser. This baseline asserts
the static pages load with ZERO console errors + ZERO uncaught page errors. Per-page
smokes for the other pages are added later by Phase-2 (not here) using `assert_page_ok`.
"""

import pytest

from tests.e2e.conftest import assert_page_ok


@pytest.mark.e2e
def test_login_page_smoke(live_server: str, browser_page: object) -> None:
    """/login.html loads clean (guest mode renders it without auth)."""
    assert_page_ok(browser_page, live_server, "/login.html")


@pytest.mark.e2e
def test_index_page_smoke(live_server: str, browser_page: object) -> None:
    """/index.html (dashboard shell) loads clean from mock-data.js (self-contained)."""
    assert_page_ok(browser_page, live_server, "/index.html")
