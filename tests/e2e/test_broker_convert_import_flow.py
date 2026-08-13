"""E2E (Playwright, real server + real frontend): a BROKER export, converted, then imported.

``scripts/schwab_convert.py`` turns a Charles Schwab transaction export into this app's own
import templates. Everything downstream of it already had tests; the join did not. This file
closes the loop the owner actually walks:

    broker CSV → converter → import_*.csv → the 匯入 upload control → the ledger → the report

and it is the only test that asserts the converter's OUTPUT is something the IMPORTER accepts.
The two live in different packages with different validators, so "the converter produced 11
transaction rows" and "the app imported 11 transaction rows" are separate facts — a column
renamed on either side, a kind spelled the broker's way, a ratio written as a decimal, and the
files convert perfectly into something no door will take.

The corpus is the committed synthetic one (``tests/golden/broker/schwab_2024.csv`` +
``schwab_2025.csv``), and it is deliberately awkward: overlapping export windows, a
self-cancelling pair, an option row, a reverse split, an exchange, a DRIP, a position older
than the window, and the three cash kinds a US broker statement carries.

**Import ORDER is load-bearing and is part of what this asserts.** Transactions go first: a
corporate action is validated against the position that exists on its own date, so uploading
the actions first would refuse (or silently mis-apply to) every one of them. The end-state
share counts below are the proof that the two halves met — 85 shares became 255 only because
the SPLIT found the buy that the earlier file wrote.

``*_TO_COMPLETE.csv`` is covered by the second test as the REJECTION it is designed to be.

ZERO unexpected console / page errors throughout; the browser context comes from the shared
``fresh_page`` fixture (issue #67's third-party stub).
"""

import json
import shutil
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
from scripts import schwab_convert
from tests.e2e.conftest import FlowServerFactory

_CORPUS = Path(__file__).resolve().parents[1] / "golden" / "broker"

#: The corpus files the converter is pointed at. ``schwab_unmapped.csv`` lives in the same
#: directory and is EXCLUDED on purpose: it is the fixture for rule 7 (an action the adapter
#: has never seen), so a converter run over the whole directory exits 2 by design.
_EXPORTS = ("schwab_2024.csv", "schwab_2025.csv")

#: Every ticker the corpus names, registered before the import. Not a convenience: a corporate
#: action HARD-rejects an unregistered ``from_symbol``/``to_symbol`` (E10), because an action
#: that renames a position the app has never heard of cannot be checked against anything.
_TICKERS = ("ALFA", "BETA", "GAMM", "OLDX", "NEWX", "PARE", "PREH")


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Re-enable loopback sockets PER TEST (pytest-socket re-bans before every test); each
    flow spawns a fresh isolated uvicorn (free-port probe + readiness poll need loopback)."""
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


# --------------------------------------------------------------------------- plumbing

def _get_json(base_url: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(base_url + path, timeout=10) as r:  # noqa: S310 (loopback)
        data: dict[str, Any] = json.loads(r.read().decode("utf-8"))
        return data


def _sink(page: Page) -> tuple[list[str], list[str]]:
    """Console-error + pageerror sinks. Chromium logs every 4xx as a console error and this
    flow EXPECTS some (the 422s that raise the warning dialog), so the list is filtered by
    substring rather than dropped."""
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(getattr(m, "text", ""))
            if getattr(m, "type", None) == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    return console_errors, page_errors


def _unexpected(console_errors: list[str], *allowed: str) -> list[str]:
    return [e for e in console_errors if not any(a in e for a in allowed)]


def _seed_registered_us_account(conn: sqlite3.Connection) -> None:
    """The schwab account plus every ticker the corpus names. No ledger rows: the whole
    point is that the import writes them."""
    seed_accounts(conn)
    for symbol in _TICKERS:
        upsert_instrument(conn, Instrument(symbol=symbol, market=Market.US,
                                           quote_ccy=Currency.USD, sector="Tech",
                                           name=f"{symbol} Corp"))
    conn.commit()


def _convert(tmp_path: Path) -> Path:
    """Run the REAL converter over the committed corpus; return the output directory."""
    export_dir = tmp_path / "export"
    out_dir = tmp_path / "converted"
    export_dir.mkdir()
    for name in _EXPORTS:
        shutil.copyfile(_CORPUS / name, export_dir / name)
    rc = schwab_convert.main([
        "--export", str(export_dir), "--out", str(out_dir), "--account", "schwab"])
    assert rc == 0, f"converter exited {rc} (1 = a blocking reconcile issue, 2 = a bad run)"
    return out_dir


def _open_csv_tab(page: Page, base: str) -> None:
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#csv-kinds .chip", state="attached")
    page.click("#tab-csv")
    page.wait_for_selector("#csv-dropzone", state="visible")


def _upload(page: Page, chip: str, path: Path) -> dict[str, Any]:
    """Select the CSV kind chip, upload *path* through the real file input, return the
    preview body.

    The paste box is emptied FIRST. Switching kinds re-previews whatever text is already
    there, so after a rejected upload the chip click would fire a preview of the PREVIOUS
    file parsed as the new kind — and ``expect_response`` would hand this function that
    response instead of the one for the file it just uploaded. An empty box short-circuits
    ``runCsvPreview`` before it issues a request, so the only preview in flight is ours.
    """
    page.fill("#csv-paste", "")
    page.locator("#csv-kinds .chip", has_text=chip).click()
    with page.expect_response("**/api/import/preview") as pv:
        page.set_input_files("#csv-file-input", files=[FilePayload(
            name=path.name, mimeType="text/csv",
            buffer=path.read_bytes())])
    assert pv.value.status == 200, f"{path.name}: preview status {pv.value.status}"
    body: dict[str, Any] = pv.value.json()
    return body


def _import_file(page: Page, chip: str, path: Path, *, warn_rows: int) -> dict[str, Any]:
    """Upload + commit one converted file; return the commit response body.

    ``warn_rows`` is asserted, not tolerated: the converter's job is to leave nothing to
    confirm that the statement itself did not already say. A file that starts warning about
    rows it used to import cleanly is a regression in the converter, and a warning that
    silently disappears means a guard stopped firing.
    """
    preview = _upload(page, chip, path)
    assert sum(1 for r in preview["rows"] if r["status"] == "warn") == warn_rows, preview["rows"]
    assert [r for r in preview["rows"] if r["status"] == "error"] == []
    page.wait_for_function(
        "() => { const b = document.querySelector('#csv-confirm'); return b && !b.disabled; }")
    with page.expect_response("**/api/import/commit") as cm:
        page.click("#csv-confirm")
    if warn_rows:
        # A soft issue holds the write until the owner confirms it in the dialog.
        assert cm.value.status == 422, f"{path.name}: expected the warning gate"
        assert cm.value.json()["error"]["code"] == "warnings_unacknowledged"
        dialog = page.locator(".modal-backdrop .modal", has_text="匯入警告確認")
        expect(dialog).to_be_visible()
        with page.expect_response("**/api/import/commit") as acked:
            dialog.locator("button", has_text="確認寫入").click()
        assert acked.value.status == 200, f"{path.name}: acked commit {acked.value.status}"
        acked_body: dict[str, Any] = acked.value.json()
        return acked_body
    assert cm.value.status == 200, f"{path.name}: commit status {cm.value.status}"
    body: dict[str, Any] = cm.value.json()
    return body


def _shares(base: str) -> dict[str, Decimal]:
    return {h["symbol"]: Decimal(h["shares"])
            for h in _get_json(base, "/api/dashboard")["holdings"]}


# ------------------------------------------------------------------ the converted import

@pytest.mark.e2e
def test_a_converted_schwab_export_imports_through_the_browser(
    flow_server: FlowServerFactory, fresh_page: Page, tmp_path: Path
) -> None:
    """The converter's four import files upload and commit, and the ledger then agrees.

    Breaks if: the converter emits a column the parser does not accept (every row would come
    back ✕ 錯誤); a kind/ratio spelling drifts between the two packages; the 資金 door stops
    accepting the three broker-statement cash kinds; or the corporate actions stop reaching
    the replay (the share counts below are the only place that is visible).
    """
    out_dir = _convert(tmp_path)
    base = flow_server(_seed_registered_us_account)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    assert _shares(base) == {}                    # nothing yet — the import writes it all

    _open_csv_tab(page, base)

    # 交易 FIRST (see the module docstring): a corporate action is checked against the
    # position that exists on its own date. Two ⚠ rows, both honest: the ALFA sell is the
    # preview's known non-sibling-awareness (the covering buy is two rows above it in the
    # SAME file), and the PREH sell really has no position — its 期初庫存 is the worksheet
    # the export could not fill in.
    txns = _import_file(page, "交易", out_dir / "import_transactions.csv", warn_rows=2)
    assert txns["written"] == 11 and txns["skipped"] == 0

    divs = _import_file(page, "股利", out_dir / "import_dividends.csv", warn_rows=0)
    assert divs["written"] == 1

    cash = _import_file(page, "資金", out_dir / "import_cash.csv", warn_rows=0)
    assert cash["written"] == 7

    # ⚠ 1 row: NEWX has no price yet, which the exchange preview says out loud because ONE
    # unpriced holding suppresses the whole portfolio's XIRR.
    actions = _import_file(page, "公司行動", out_dir / "import_corporate_actions.csv",
                           warn_rows=1)
    assert actions["written"] == 3

    # --- the ledger, rebuilt from what was written ---------------------------------------
    held = _shares(base)
    assert held["BETA"] == Decimal("255")            # 85 bought × 3-for-1 SPLIT
    assert held["GAMM"] == Decimal("100")            # 1,000 ÷ 10 reverse split − 30 + 30
    assert held["NEWX"] == Decimal("200")            # OLDX 200 EXCHANGEd 1:1 into NEWX
    assert "OLDX" not in held                        # …and the old line is gone
    assert held["PARE"] == Decimal("150")
    # 100 + 50 − 60 + 10 bought, plus the DRIP's fractional reinvest — the dividend file
    # reached the same replay as the transaction file.
    assert held["ALFA"] == Decimal("101.81366934")
    # The prehistory sell has no covering position (its 期初庫存 is the unfillable worksheet),
    # so it stands as an oversold line rather than being quietly absorbed.
    assert held["PREH"] == Decimal("-80")

    movements = _get_json(base, "/api/cash")["movements"]["rows"]
    assert sorted(m["kind"] for m in movements) == sorted(
        ["deposit", "deposit", "deposit", "interest", "interest_expense",
         "broker_fee", "broker_fee"])

    # Four uploads, four undoable batches (one per kind) — the whole import is reversible.
    batches = _get_json(base, "/api/import/batches")["batches"]
    assert {b["kind"]: b["row_count"] for b in batches} == {
        "transactions": 11, "dividends": 1, "cash": 7, "corporate_actions": 3}

    assert _unexpected(console_errors, "status of 422") == []
    assert page_errors == []


# --------------------------------------------------------------- the worksheets, rejected

@pytest.mark.e2e
def test_the_to_complete_worksheets_are_rejected_until_filled_in(
    flow_server: FlowServerFactory, fresh_page: Page, tmp_path: Path
) -> None:
    """``*_TO_COMPLETE.csv`` must NOT import — that is the design, not an obstacle.

    The converter writes those two files with the fields the export genuinely does not
    determine left blank: a one-leg split states its delta and never its ratio (D14), and a
    position older than the export window has no cost in it (D37). Guessing either produces a
    plausible-looking wrong number — a share count that turns a later sell into a 賣超 and
    discards a cost basis permanently.

    Breaks if either door starts accepting a blank required field, or downgrades the refusal
    to a confirmable warning (the confirm button would enable and the owner could click past
    it).
    """
    out_dir = _convert(tmp_path)
    base = flow_server(_seed_registered_us_account)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    _open_csv_tab(page, base)

    for chip, name, expected in (
        ("公司行動", "corporate_actions_TO_COMPLETE.csv", "ratio_to"),
        ("期初", "openings_TO_COMPLETE.csv", "original_cost_total"),
    ):
        path = out_dir / name
        assert path.exists(), f"the converter did not write {name}"
        preview = _upload(page, chip, path)
        assert [r["status"] for r in preview["rows"]] == ["error"], preview["rows"]
        assert expected in preview["rows"][0]["reason"]
        # ✕ 錯誤 on screen, and 確認寫入 held disabled — there is no path past it.
        expect(page.locator("#csv-body .st-error")).to_have_count(1)
        expect(page.locator("#csv-confirm")).to_be_disabled()

    # Nothing was written by either upload.
    assert _get_json(base, "/api/import/batches")["batches"] == []
    assert _shares(base) == {}
    assert _get_json(base, "/api/ledgers/openings")["total_count"] == 0

    assert _unexpected(console_errors) == []
    assert page_errors == []
