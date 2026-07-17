"""E2E: the CSV 匯入 tab — file-picker, drag/drop upload, paste, preview, confirm (FU-D16).

Guards the dead-zone fix: before FU-D16, `#pane-csv` never had the ids `initCsv()` binds, so
click-to-select, drag-drop AND paste-preview were all unbound. This drives the REAL stack
(uvicorn + SQLite + the served web/ frontend) against a fresh isolated server and asserts all
three input paths work and a committed row actually lands in the ledger — with ZERO console /
page errors throughout.
"""

import json
import urllib.request
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from playwright.sync_api import FilePayload, Page
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Re-enable loopback sockets PER TEST (pytest-socket re-bans before every test); each flow
    spawns a fresh isolated uvicorn (free-port probe + readiness poll need loopback TCP)."""
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _get_json(base_url: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(base_url + path, timeout=5) as r:  # noqa: S310 (loopback)
        data: dict[str, Any] = json.loads(r.read().decode("utf-8"))
        return data


def _shares_of(body: dict[str, Any], symbol: str) -> str | None:
    for h in body["holdings"]:
        if h["symbol"] == symbol:
            shares: str = h["shares"]
            return shares
    return None


def _sink(page: Page) -> tuple[list[str], list[str]]:
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(getattr(m, "text", ""))
            if getattr(m, "type", None) == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    return console_errors, page_errors


def _open_csv_tab(page: Page, base: str) -> None:
    page.goto(base + "/trades.html", wait_until="load")
    # boot (GET /input/context) is done once initCsv has built the kind chips + bound the
    # dropzone / hidden file input.
    page.wait_for_selector("#csv-kinds .chip", state="attached")
    page.click("#tab-csv")
    page.wait_for_selector("#csv-dropzone", state="visible")


@pytest.mark.e2e
def test_csv_import_all_three_input_paths_and_commit(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_golden)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    before = _shares_of(_get_json(base, "/api/dashboard"), "2330")
    assert before is not None and Decimal(before) == Decimal("1000")

    _open_csv_tab(page, base)

    # (a) clicking the dropzone opens the browser file chooser (the click->picker path).
    with page.expect_file_chooser() as fc_info:
        page.click("#csv-dropzone")
    assert fc_info.value is not None

    # (b) selecting a file loads it (FileReader) -> preview renders with counts.
    upload = (
        b"account,symbol,side,date,shares,price\r\n"
        b"tw_broker,2330,buy,2026-07-10,100,600\r\n"
    )
    with page.expect_response("**/api/import/preview") as pv:
        page.set_input_files(
            "#csv-file-input",
            files=[FilePayload(name="buy.csv", mimeType="text/csv", buffer=upload)],
        )
    assert pv.value.status == 200
    page.wait_for_selector("#csv-body tr")
    page.wait_for_function(
        "() => { const c = document.querySelector('#csv-counts');"
        " return c && c.textContent.includes('可寫入'); }"
    )
    # the filename surfaces + confirm enables once a non-error preview lands.
    page.wait_for_function(
        "() => { const f = document.querySelector('#csv-file');"
        " return f && f.textContent.trim() !== ''; }"
    )
    page.wait_for_function(
        "() => { const b = document.querySelector('#csv-confirm'); return b && !b.disabled; }"
    )

    with page.expect_response("**/api/import/commit") as cm:
        page.click("#csv-confirm")
    assert cm.value.status == 200
    page.wait_for_selector("#csv-result", state="visible")

    after = _shares_of(_get_json(base, "/api/dashboard"), "2330")
    assert after is not None and Decimal(after) == Decimal("1100")  # 1000 + 100 committed

    # (c) paste path: typing CSV into the textarea also previews (debounced).
    paste = (
        "account,symbol,side,date,shares,price\r\n"
        "schwab,AAPL,buy,2026-07-10,5,150"
    )
    with page.expect_response("**/api/import/preview") as pv2:
        page.fill("#csv-paste", paste)
    assert pv2.value.status == 200
    page.wait_for_function("() => document.querySelectorAll('#csv-body tr').length >= 1")

    assert not console_errors and not page_errors, (
        f"CSV import flow: console={console_errors!r} page={page_errors!r}"
    )


@pytest.mark.e2e
def test_csv_template_download_serves_csv(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """The 下載範本 button downloads a text/csv template whose header leads with `account`."""
    base = flow_server(_seed_golden)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    _open_csv_tab(page, base)

    with page.expect_download() as dl_info:
        page.click("#csv-template")
    download = dl_info.value
    assert download.suggested_filename == "import_template_transactions.csv"
    path = download.path()
    assert path is not None
    text = path.read_text(encoding="utf-8-sig")  # utf-8-sig strips the Excel BOM
    # FU-D19: the header now annotates the date column with its ISO format.
    assert text.split("\r\n")[0].startswith("account,symbol,side,date(YYYY-MM-DD),shares,price")

    assert not console_errors and not page_errors, (
        f"template download: console={console_errors!r} page={page_errors!r}"
    )


@pytest.mark.e2e
def test_csv_ambiguous_date_shows_chooser_then_commits(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """FU-D19: pasting a CSV whose date column is genuinely ambiguous (M/D vs D/M) must NOT be
    guessed — the chooser appears with the confirm held disabled; picking a format resolves the
    preview and the commit then lands."""
    base = flow_server(_seed_golden)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    before = _shares_of(_get_json(base, "/api/dashboard"), "2330")
    assert before is not None and Decimal(before) == Decimal("1000")

    _open_csv_tab(page, base)

    # 3/4 and 5/6 read differently as M/D vs D/M -> ambiguous, the backend refuses to guess.
    ambiguous = (
        "account,symbol,side,date,shares,price\r\n"
        "tw_broker,2330,buy,3/4/2026,100,600\r\n"
        "tw_broker,2330,buy,5/6/2026,100,600"
    )
    with page.expect_response("**/api/import/preview") as pv:
        page.fill("#csv-paste", ambiguous)
    assert pv.value.status == 200
    # the chooser surfaces and the confirm is held disabled (no guess path).
    page.wait_for_selector("#csv-datefmt", state="visible")
    page.wait_for_function(
        "() => { const b = document.querySelector('#csv-confirm'); return b && b.disabled; }"
    )
    # both interpretations are offered.
    opts = page.eval_on_selector_all(
        "#csv-datefmt-select option", "els => els.map(o => o.value)"
    )
    assert "mdy" in opts and "dmy" in opts

    # pick D/M/YYYY -> re-preview resolves, chooser hides, confirm enables.
    with page.expect_response("**/api/import/preview") as pv2:
        page.select_option("#csv-datefmt-select", "dmy")
    assert pv2.value.status == 200
    page.wait_for_selector("#csv-datefmt", state="hidden")
    page.wait_for_function(
        "() => { const b = document.querySelector('#csv-confirm'); return b && !b.disabled; }"
    )

    with page.expect_response("**/api/import/commit") as cm:
        page.click("#csv-confirm")
    assert cm.value.status == 200
    page.wait_for_selector("#csv-result", state="visible")

    after = _shares_of(_get_json(base, "/api/dashboard"), "2330")
    assert after is not None and Decimal(after) == Decimal("1200")  # 1000 + 100 + 100

    assert not console_errors and not page_errors, (
        f"CSV ambiguous-date flow: console={console_errors!r} page={page_errors!r}"
    )
