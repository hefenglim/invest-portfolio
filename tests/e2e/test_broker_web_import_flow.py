"""E2E (Playwright, real server + real frontend): the WEB broker-import door (C6).

The sibling file ``test_broker_convert_import_flow.py`` walks the CLI route — run the script,
then upload its four files one at a time in the right order. This one walks the route the
owner will actually take:

    drop the RAW broker export → one report → **one button** → the ledger → 復原

and it asserts the two properties that make that button safe to press:

* **the numbers land exactly where the CLI route puts them** — same corpus, same positions,
  because there is one conversion and the endpoint is a wrapper around it, not a second
  implementation; and
* **the whole thing comes back off** — every batch the run wrote is listed with a 復原
  control, and using them returns the ledger to empty. An easy way to load five years of
  broker history with no way to undo it is a button nobody should press.

Also covered here because it has never had a browser test at all: the **import-batch card**
(#83). The endpoints have existed since the provenance work with zero callers in ``web/``,
which means the undo was reachable only from a SQLite console.

ZERO unexpected console / page errors; the browser context comes from the shared
``fresh_page`` fixture (issue #67's third-party stub).
"""

import json
import sqlite3
import urllib.request
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import FilePayload, Page, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import upsert_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from tests.e2e.conftest import FlowServerFactory

_CORPUS = Path(__file__).resolve().parents[1] / "golden" / "broker"
_EXPORTS = ("schwab_2024.csv", "schwab_2025.csv")
_TICKERS = ("ALFA", "BETA", "GAMM", "OLDX", "NEWX", "PARE", "PREH")


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _get_json(base_url: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(base_url + path, timeout=10) as r:  # noqa: S310 (loopback)
        data: dict[str, Any] = json.loads(r.read().decode("utf-8"))
        return data


def _sink(page: Page) -> tuple[list[str], list[str]]:
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(getattr(m, "text", ""))
            if getattr(m, "type", None) == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    return console_errors, page_errors


def _seed(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    for symbol in _TICKERS:
        upsert_instrument(conn, Instrument(symbol=symbol, market=Market.US,
                                           quote_ccy=Currency.USD, sector="Tech",
                                           name=f"{symbol} Corp"))
    conn.commit()


def _shares(base: str) -> dict[str, Decimal]:
    return {h["symbol"]: Decimal(h["shares"])
            for h in _get_json(base, "/api/dashboard")["holdings"]}


def _open_broker_mode(page: Page, base: str) -> None:
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#csv-source .chip", state="attached")
    page.click("#tab-csv")
    page.locator("#csv-source .chip", has_text="券商對帳單").click()
    page.wait_for_selector("#bk-dropzone", state="visible")
    # …and the standard-template block yields the pane rather than stacking under it.
    expect(page.locator("#csv-standard")).to_be_hidden()


def _drop_exports(page: Page) -> dict[str, Any]:
    """Upload the RAW statements through the real control; return the convert response."""
    with page.expect_response("**/api/broker/convert") as resp:
        page.set_input_files("#bk-file-input", files=[
            FilePayload(name=n, mimeType="text/csv", buffer=(_CORPUS / n).read_bytes())
            for n in _EXPORTS
        ])
    assert resp.value.status == 200, f"convert status {resp.value.status}"
    body: dict[str, Any] = resp.value.json()
    return body


@pytest.mark.e2e
def test_a_raw_statement_converts_imports_and_undoes_from_the_page(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """★ The whole door: drop → report → 全部寫入 → the same ledger the CLI route builds → 復原.

    Breaks if: the conversion drifts between the endpoint and the script (the positions below
    are the CLI test's own numbers); the commit sequencing loses a kind or gets the order
    wrong; the batch card stops listing what was written; or 復原 stops removing exactly the
    rows its batch wrote.
    """
    base = flow_server(_seed)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    assert _shares(base) == {}
    assert _get_json(base, "/api/import/batches")["batches"] == []

    _open_broker_mode(page, base)
    body = _drop_exports(page)
    assert body["ok"] is True and body["blocking"] == []

    # The report states the verdict in words before it offers the button.
    expect(page.locator("#bk-report")).to_contain_text("對帳通過")
    expect(page.locator("#bk-report")).to_contain_text("交易")

    # The one-leg split has no ratio in the file, so the report asks for it rather than
    # guessing — D14's decimal ratio in another costume. Left blank here on purpose: the
    # run must still work, and the row must simply not be written.
    assert body["actions_needing_input"], "the corpus carries a one-leg split"

    page.wait_for_function(
        "() => { const b = document.querySelector('#bk-commit'); return b && !b.disabled; }")
    with page.expect_response("**/api/import/commit") as first:
        page.click("#bk-commit")
    assert first.value.status == 200
    # The button drives several commits in SEQUENCE, so the run is over only when the
    # summary appears. Matched on the full phrase: 「寫入」 alone also occurs in the report
    # the page was already showing, so a substring test would pass before anything ran.
    expect(page.locator("#bk-report")).to_contain_text("全部寫入完成", timeout=15000)
    expect(page.locator("#bk-report")).not_to_contain_text("寫入中止")

    # --- the ledger the CLI route also produces ------------------------------------------
    held = _shares(base)
    assert held["BETA"] == Decimal("255")            # 85 bought × 3-for-1 SPLIT
    assert held["NEWX"] == Decimal("200")            # OLDX EXCHANGEd 1:1 into NEWX
    assert "OLDX" not in held
    assert held["PARE"] == Decimal("150")
    assert held["ALFA"] == Decimal("101.81366934")   # incl. the DRIP's fractional reinvest

    # --- every batch it wrote is listed, with a 復原 beside it ----------------------------
    batches = _get_json(base, "/api/import/batches")["batches"]
    # The CLI route's own numbers, kind for kind. Counting rather than listing kinds is what
    # catches a sequencing bug that writes a file twice or drops half of one.
    assert {b["kind"]: b["row_count"] for b in batches} == {
        "transactions": 11, "dividends": 1, "cash": 7, "corporate_actions": 3}
    for b in batches:
        # The source file NAME rides the batch, so a year from now the list still says where
        # a row came from. (Both statements were uploaded together, so both names appear.)
        assert "schwab_2024.csv" in (b["source_name"] or "")
    rows = page.locator("#bk-batches tr")
    expect(rows).to_have_count(len(batches))
    expect(page.locator("#bk-batches")).to_contain_text("schwab_2024.csv")

    # --- 復原, through the real control, until the ledger is empty again ------------------
    for _ in range(len(batches)):
        page.locator("#bk-batches tr").first.locator("button", has_text="復原").click()
        dialog = page.locator(".modal-backdrop .modal", has_text="復原這批匯入")
        expect(dialog).to_be_visible()
        with page.expect_response("**/api/import/batches/**"):
            dialog.locator("button", has_text="復原").click()
        page.wait_for_timeout(150)

    assert _get_json(base, "/api/import/batches")["batches"] == []
    assert _shares(base) == {}, "復原 must return the ledger to where it started"

    assert _unexpected(console_errors) == []
    assert page_errors == []


def _unexpected(console_errors: list[str], *allowed: str) -> list[str]:
    return [e for e in console_errors if not any(a in e for a in allowed)]


@pytest.mark.e2e
def test_an_unreadable_row_refuses_in_words_and_offers_no_button(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """Rule 7 at the door: an ``(action, description)`` pair the adapter has never seen stops
    the run with the pair quoted, and **no** import control appears.

    The refusal has to reach the SCREEN, not just the response. A converter that fails
    silently in the network tab is a converter the owner believes worked.
    """
    base = flow_server(_seed)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    _open_broker_mode(page, base)
    with page.expect_response("**/api/broker/convert") as resp:
        page.set_input_files("#bk-file-input", files=[FilePayload(
            name="schwab_unmapped.csv", mimeType="text/csv",
            buffer=(_CORPUS / "schwab_unmapped.csv").read_bytes())])
    assert resp.value.status == 422
    assert resp.value.json()["error"]["code"] == "broker_row_unmapped"

    expect(page.locator("#bk-report")).to_contain_text("無法轉換")
    expect(page.locator("#bk-report")).to_contain_text("不會用猜的")
    assert page.locator("#bk-commit").is_disabled()
    assert _get_json(base, "/api/import/batches")["batches"] == []

    # Chromium logs the 422 as a console error; nothing else is allowed.
    assert _unexpected(console_errors, "status of 422") == []
    assert page_errors == []
