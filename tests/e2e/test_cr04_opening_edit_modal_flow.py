"""E2E (CR-04, 2026-10-04): the 期初 edit dialog edits 原始總成本 and never rewrites it.

The dialog offered 原始均價 and sent the server's own ``total / shares`` quotient back as
``avg``; the server stored ``total = avg × shares``. Measured: an opening of 3 shares / 100,000
with only its 建檔日 changed came back as 99999.99999999999999999999999. Driven against the
REAL stack: open 交易帳本 › 期初庫存, 編輯 the row, change ONLY the date, 儲存 — the stored total
is still exactly 100000. Then change ONLY the share count — the total still stays, and the
average the table shows is the server's new ``total / shares``.

ZERO console / page errors throughout.
"""

import json
import urllib.request
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from playwright.sync_api import Page, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.data_ingestion.store import upsert_instrument, upsert_opening
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Re-enable loopback sockets PER TEST (pytest-socket re-bans before every test)."""
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _seed(conn: Any) -> None:
    """Golden scenario + one opening whose total is NOT a multiple of its share count."""
    _seed_golden(conn)
    upsert_instrument(conn, Instrument(symbol="2454", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Semiconductors", name="MediaTek", board="TWSE"))
    upsert_opening(conn, account_id="tw_broker", symbol="2454", shares=Decimal("3"),
                   original_cost_total=Decimal("100000"), build_date=date(2026, 1, 2))
    conn.commit()


def _opening(base: str) -> dict[str, Any]:
    with urllib.request.urlopen(base + "/api/ledgers/openings?symbol=2454",  # noqa: S310
                                timeout=5) as r:
        rows: list[dict[str, Any]] = json.loads(r.read().decode("utf-8"))["rows"]
    assert len(rows) == 1, rows
    return rows[0]


def _open_edit_dialog(page: Page, base: str) -> None:
    page.goto(base + "/trades.html", wait_until="load")
    page.click("#tab-lopen")
    page.wait_for_selector("#pane-lopen.active", state="attached")
    row = page.locator("#open-body tr", has_text="2454")
    expect(row).to_have_count(1)
    row.get_by_role("button", name="編輯").click()
    page.wait_for_selector(".modal #edit-open-total", state="visible")


def _save(page: Page) -> None:
    with page.expect_response("**/api/ledgers/openings/**") as resp:
        page.locator(".modal .btn-primary", has_text="儲存").click()
    assert resp.value.status == 200, f"opening edit status {resp.value.status}"
    page.wait_for_selector(".modal", state="detached")


@pytest.mark.e2e
def test_editing_an_opening_never_rewrites_its_original_cost(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed)
    page = fresh_page
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(getattr(m, "text", ""))
            if getattr(m, "type", None) == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    sent: list[dict[str, Any]] = []
    page.on("request", lambda req: sent.append(json.loads(req.post_data or "{}"))
            if req.method == "PUT" and "/api/ledgers/openings/" in req.url else None)

    # ===== the dialog edits the TOTAL (pre-filled with the stored string), not the average ==
    _open_edit_dialog(page, base)
    expect(page.locator(".modal #edit-open-total")).to_have_value("100000")
    labels = [t.strip() for t in page.locator(".modal .field > label").all_inner_texts()]
    assert "原始總成本（TWD）" in labels and "原始均價" not in labels, labels

    # ===== (A) date only → the original cost is byte-identical ==============================
    page.locator(".modal input[type=date]").fill("2026-01-03")
    _save(page)
    assert sent and "avg" not in sent[-1], sent[-1]
    after = _opening(base)
    assert after["total"] == "100000", after          # not 99999.99999999999999999999999
    assert after["date"] == "2026-01-03"

    # ===== (B) shares only → the total stays; the average follows on read ===================
    _open_edit_dialog(page, base)
    page.locator(".modal input[type=number]").first.fill("4")
    _save(page)
    after = _opening(base)
    assert (after["shares"], after["total"], after["avg"]) == ("4", "100000", "25000"), after

    assert not console_errors and not page_errors, (
        f"opening edit flow: console={console_errors!r} page={page_errors!r}"
    )
