"""E2E flow (Playwright, real server + real frontend) — digest dashboard cards UX (W2-B2).

Drives the REAL stack (uvicorn subprocess + on-disk SQLite + StaticFiles web/) against a
GUEST DB seeded with holdings + two recent closes but NO stored digest, so both digest cards
start in their empty state. Verifies:
  * both cards show the 排程中心 link + an inline 立即產生 button (item 4),
  * clicking 立即產生 (guest-open per FU-D4) regenerates the digest and renders it IN PLACE
    with no page reload (poll /latest until the fresh digest lands),
  * movers render the instrument NAME with a native tooltip carrying 收盤 / 更新 (item 5),
  * ZERO console errors + ZERO uncaught page errors throughout.

The manual run is async (202 + background daemon thread that opens its OWN session on the
same file DB), so this exercises the true generate→store→poll→render loop the in-process
contract tests deliberately cannot.
"""

import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import ConsoleMessage, Page
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Market
from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory

_TAIPEI = ZoneInfo("Asia/Taipei")


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Re-enable loopback sockets PER TEST. pytest-socket's --disable-socket re-bans
    sockets before every test; the session-scoped _e2e_loopback_socket only lifts the
    ban once. These flows create fresh Python sockets per test (flow_server's free-port
    probe + readiness poll), so each needs the loopback exception re-applied here."""
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _seed_digest_ready(conn: sqlite3.Connection) -> None:
    """Golden holdings + TWO closes dated relative to the REAL clock the subprocess uses,
    so a manual daily digest computes a day-change + up/down movers. No digest is stored →
    the card starts in its empty state (link + 立即產生)."""
    _seed_golden(conn)
    today = datetime.now(_TAIPEI).date()
    yday = today - timedelta(days=1)
    fetched = datetime(today.year, today.month, today.day, 15, 0, tzinfo=_TAIPEI)
    upsert_prices(
        conn,
        [
            PriceRow(instrument="2330", market=Market.TW, as_of=yday,
                     close=Decimal("600"), source="test"),
            PriceRow(instrument="2330", market=Market.TW, as_of=today,
                     close=Decimal("606"), source="test"),   # +1% → up mover
            PriceRow(instrument="AAPL", market=Market.US, as_of=yday,
                     close=Decimal("120"), source="test"),
            PriceRow(instrument="AAPL", market=Market.US, as_of=today,
                     close=Decimal("117.6"), source="test"),  # −2% → down mover
        ],
        fetched_at=fetched,
    )


def test_digest_cards_empty_then_generate_inplace(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base_url = flow_server(_seed_digest_ready)  # guest mode (no users)
    page = fresh_page

    console_errors: list[str] = []
    page_errors: list[str] = []

    def _on_console(msg: ConsoleMessage) -> None:
        if getattr(msg, "type", None) == "error":
            console_errors.append(getattr(msg, "text", repr(msg)))

    def _on_pageerror(exc: object) -> None:
        page_errors.append(str(exc))

    page.on("console", _on_console)
    page.on("pageerror", _on_pageerror)

    page.goto(base_url + "/index.html", wait_until="load")

    # Both cards start in the empty state: a real 排程中心 link + an inline 立即產生 button.
    page.wait_for_selector("#digest-daily-body .digest-empty")
    page.wait_for_selector("#digest-weekly-body .digest-empty")
    for body in ("#digest-daily-body", "#digest-weekly-body"):
        assert page.query_selector(
            body + ' .digest-empty a[href="settings.html#scheduler"]'
        ) is not None, f"{body}: missing 排程中心 link"
        assert page.query_selector(body + " .digest-gen-btn") is not None, (
            f"{body}: missing 立即產生 button"
        )

    # Mark the window; a full reload would clear it — this proves the update is in-place.
    page.evaluate("window.__pdNoReload = true")

    # Click daily 立即產生 → poll → the fresh digest renders in place (no reload).
    page.click("#digest-daily-body .digest-gen-btn")
    page.wait_for_selector("#digest-daily-body .digest-headline", timeout=60_000)
    assert page.evaluate("window.__pdNoReload") is True, (
        "the card update must not reload the page"
    )

    # Movers render the instrument NAME with a native tooltip carrying 收盤 / 更新 parts.
    page.wait_for_selector("#digest-daily-body .digest-mover")
    chips = page.query_selector_all("#digest-daily-body .digest-mover")
    assert chips, "expected mover chips after generation"
    names = []
    for chip in chips:
        sym = chip.query_selector(".digest-mover-sym")
        assert sym is not None
        names.append(sym.inner_text())
    assert any(n in ("TSMC", "Apple") for n in names), (
        f"movers should display instrument names, got {names!r}"
    )
    titles = [chip.get_attribute("title") or "" for chip in chips]
    assert any("更新" in t and "收盤" in t and "・" in t for t in titles), (
        f"a mover tooltip must carry 收盤 + 更新, got {titles!r}"
    )

    page.remove_listener("console", _on_console)
    page.remove_listener("pageerror", _on_pageerror)
    assert not console_errors and not page_errors, (
        f"console errors={console_errors!r}; page errors={page_errors!r}"
    )
