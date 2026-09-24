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
    # M5-b (demo audit, second full re-verification 2026-09-22): both pickers are named by
    # web/names.js. The account picker read 「TW Broker（tw_broker）」 — the API's English
    # name + the raw id — on the page whose trade form reads 「台灣券商（TWD）」.
    expect(page.locator("#bk-broker option[value='schwab']")).to_have_text("嘉信 Schwab")
    expect(page.locator("#bk-account option[value='schwab']")).to_have_text("嘉信 Schwab（USD）")
    labels = page.locator("#bk-account option").all_inner_texts()
    assert not [t for t in labels if "Broker" in t or "Charles" in t or "_" in t], labels
    # DEF-029 (2026-09-23): the account picker lists ONLY the accounts of the chosen broker.
    # A Schwab statement could be aimed at 「Moomoo MY（USD／MYR）」 and wrote a real buy under
    # the wrong broker with every check green; at 「台灣券商（TWD）」 it read 「對帳通過」 and
    # was refused only at commit. Neither is offered any more (the server refuses them too).
    assert page.locator("#bk-account option").all_inner_texts() == ["嘉信 Schwab（USD）"]
    assert page.locator("#bk-account option[value='tw_broker']").count() == 0
    assert page.locator("#bk-account option[value='moomoo_my']").count() == 0


def _acknowledge_dialog(page: Page, *expect_texts: str) -> dict[str, Any]:
    """Resolve the NEXT 匯入警告確認 dialog by ticking every warning row and confirming;
    returns the acknowledged commit's request body. The blanket ack used to swallow these
    unseen — the corpus raises two: the PREH 賣超 (交易) and NEWX's missing price after
    the EXCHANGE (公司行動)."""
    dialog = page.locator(".modal-backdrop .modal", has_text="匯入警告確認")
    expect(dialog).to_be_visible(timeout=15000)
    for text in expect_texts:
        expect(dialog).to_contain_text(text)
    for cb in dialog.locator("input.bk-warn-tick").all():
        assert not cb.is_checked(), "a warning row must never start ticked"
        cb.check()
    with page.expect_response("**/api/import/commit") as acked:
        dialog.locator("button", has_text="寫入勾選的警告列").click()
    assert acked.value.status == 200, acked.value.text()
    return _sent_body(acked.value.request.post_data_json)


def _sent_body(data: Any) -> dict[str, Any]:
    """The JSON a commit request carried (Playwright types it ``Any | None``)."""
    assert isinstance(data, dict), data
    body: dict[str, Any] = data
    assert body["ack_warnings"] is True
    return body


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

    # --- DEF-028: every row is on the screen, with a checkbox, before anything is written --
    report = page.locator("#bk-report")
    for kind, n in (("transactions", 11), ("dividends", 1), ("cash", 7), ("corporate_actions", 3)):
        expect(report.locator(f"details.bk-kind[data-kind='{kind}'] tbody tr")).to_have_count(n)
        expect(report.locator(f"details.bk-kind[data-kind='{kind}'] input.bk-row-tick:checked")
               ).to_have_count(n)
    # The source line rides every row; the type column is zh; the figures are the CSV's.
    expect(report.locator("details.bk-kind[data-kind='transactions'] tbody tr").first
           ).to_contain_text("schwab_2024.csv:4")
    expect(report.locator("details.bk-kind[data-kind='transactions'] tbody tr").first
           ).to_contain_text("買入")
    # …and the rows that went elsewhere are named with their destination: the DRIP's three
    # legs became dividend row 1, the journal pairs were dropped.
    expect(report.locator("details.bk-dropped")).to_contain_text("已合併為一筆股利")
    expect(report.locator("details.bk-dropped")).to_contain_text("股利 第 1 列")
    expect(report.locator("details.bk-dropped")).to_contain_text("schwab_2024.csv:6")
    expect(report.locator("details.bk-dropped")).to_contain_text("互相抵銷")
    # DEF-027: the pre-history hint is measured against the ledger (empty here: 0 held).
    expect(report).to_contain_text("帳本在 2024-01-02 已有 0 股，仍缺 80 股")

    page.wait_for_function(
        "() => { const b = document.querySelector('#bk-commit'); return b && !b.disabled; }")
    expect(page.locator("#bk-commit")).to_have_text("寫入勾選列（22）")
    with page.expect_response("**/api/import/commit") as first:
        page.click("#bk-commit")
    # ★ DEF-027: the first commit (transactions) is sent UNACKNOWLEDGED and the server refuses
    # it — the corpus sells 80 PREH the ledger never held — so the page must now ASK, row by
    # row, instead of acknowledging on the owner's behalf (which wrote the sell with no
    # dialog, discarded the basis and read 「✓ 全部寫入完成」).
    assert first.value.status == 422
    assert first.value.json()["error"]["code"] == "warnings_unacknowledged"
    dialog = page.locator(".modal-backdrop .modal", has_text="匯入警告確認")
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text("交易")
    expect(dialog).to_contain_text("PREH")
    expect(dialog).to_contain_text("2025-11-20")
    expect(dialog).to_contain_text("賣出 80 股，超過持有的 0 股")
    expect(dialog).to_contain_text("永久捨棄")
    expect(dialog).to_contain_text("之後再買回也不會還原")
    ticks = dialog.locator("input.bk-warn-tick")
    expect(ticks).to_have_count(1)
    assert not ticks.first.is_checked(), "a warning row must never start ticked"
    confirm = dialog.locator("button", has_text="寫入勾選的警告列")
    assert confirm.is_disabled(), "nothing ticked → nothing to acknowledge"
    ticks.first.check()
    assert not confirm.is_disabled()
    with page.expect_response("**/api/import/commit") as acked:
        confirm.click()
    assert acked.value.status == 200
    sent = _sent_body(acked.value.request.post_data_json)
    assert sent["kind"] == "transactions"
    assert sorted(sent["select"]) == list(range(11)), "the ticked row travels as select"
    # The SECOND warning the blanket ack used to swallow: NEWX (the EXCHANGE's new symbol)
    # has no price row, so the corporate-actions commit asks too — one dialog per kind.
    sent = _acknowledge_dialog(page, "公司行動", "NEWX", "2025-05-22", "沒有任何價格紀錄")
    assert sent["kind"] == "corporate_actions" and sorted(sent["select"]) == [0, 1, 2]
    # The button drives several commits in SEQUENCE, so the run is over only when the
    # summary appears. Matched on the full phrase: 「寫入」 alone also occurs in the report
    # the page was already showing, so a substring test would pass before anything ran.
    expect(page.locator("#bk-report")).to_contain_text("寫入完成", timeout=15000)
    expect(page.locator("#bk-report")).not_to_contain_text("寫入中止")
    expect(page.locator(".modal-backdrop")).to_have_count(0)

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
    # DEF-049: each undo now runs the ledger tab's replay guard over the ledger WITHOUT that
    # batch, and the list is undone newest-first — so taking a batch off while an EARLIER one
    # still holds rows that depended on it (a split the later sells were booked against, a
    # deposit the trades spent) is answered with the ledger tab's own ack dialog. Each is
    # answered 「我了解，仍要復原」 here, as the owner undoing the whole run would; the end
    # state is still the empty ledger.
    ack_titles: list[str] = []
    for _ in range(len(batches)):
        page.locator("#bk-batches tr").first.locator("button", has_text="復原").click()
        dialog = page.locator(".modal-backdrop .modal", has_text="復原這批匯入")
        expect(dialog).to_be_visible()
        with page.expect_response("**/api/import/batches/**") as undone:
            dialog.locator("button", has_text="復原").click()
        while undone.value.status == 422:
            ack = page.locator(".modal-backdrop .modal").filter(
                has=page.locator("button", has_text="我了解，仍要復原"))
            expect(ack).to_be_visible()
            ack_titles.append(ack.locator(".modal-title").inner_text())
            with page.expect_response("**/api/import/batches/**") as undone:
                ack.locator("button", has_text="我了解，仍要復原").click()
        assert undone.value.status == 200
        page.wait_for_timeout(150)
    # Measured on this corpus: the cash batch goes first (newest) while the trades it funded
    # are still booked, so its pool dips — the only acknowledgement the whole run needs.
    assert ack_titles == ["現金將變為負數"], ack_titles

    assert _get_json(base, "/api/import/batches")["batches"] == []
    assert _shares(base) == {}, "復原 must return the ledger to where it started"

    # Chromium logs each unacknowledged 422 (two here: 交易, 公司行動) as a console error —
    # that refusal is the feature; nothing else is allowed.
    assert _unexpected(console_errors, "status of 422") == []
    assert page_errors == []


def _unexpected(console_errors: list[str], *allowed: str) -> list[str]:
    return [e for e in console_errors if not any(a in e for a in allowed)]


@pytest.mark.e2e
def test_an_unticked_row_is_not_written_and_a_skipped_warning_row_is_not_written(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """DEF-028 + DEF-027, the other branch: the table's checkbox decides what is sent, and
    the dialog's 「略過所有警告列」 writes the clean rows without the acknowledgement.

    Breaks if: the ticks are decorative (F-03's exact defect on the standard pane — a
    button labelled 勾選列 that wrote the whole paste); the skip path sends the ack for the
    warning row anyway; or the acknowledgement rides a literal again.
    """
    base = flow_server(_seed)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    _open_broker_mode(page, base)
    body = _drop_exports(page)
    assert body["ok"] is True

    # Untick the last ALFA buy (2025-12-15, row index 10) in the table.
    page.locator("#bk-row-transactions-10").uncheck()
    expect(page.locator("#bk-commit")).to_have_text("寫入勾選列（21）")
    with page.expect_response("**/api/import/commit") as first:
        page.click("#bk-commit")
    assert first.value.status == 422
    dialog = page.locator(".modal-backdrop .modal", has_text="匯入警告確認")
    expect(dialog).to_contain_text("PREH")
    with page.expect_response("**/api/import/commit") as skipped:
        dialog.locator("button", has_text="略過所有警告列").click()
    assert skipped.value.status == 200
    sent = _sent_body(skipped.value.request.post_data_json)
    # The warning row (index 9) and the unticked row (index 10) are both out of `select`;
    # the acknowledgement only releases the server's whole-file gate for rows not sent.
    assert sorted(sent["select"]) == list(range(9))
    # The corporate-actions kind asks about NEWX's missing price next; acknowledged here so
    # the split/exchange still land (the numbers below depend on the 3-for-1 and the 1:1).
    _acknowledge_dialog(page, "公司行動", "NEWX")
    expect(page.locator("#bk-report")).to_contain_text("寫入完成", timeout=15000)
    expect(page.locator("#bk-report")).to_contain_text("交易 寫入 9 筆")

    held = _shares(base)
    assert "PREH" not in held, "the skipped 賣超 row must not reach the ledger"
    assert held["ALFA"] == Decimal("91.81366934")   # 101.81366934 without the unticked buy
    batches = _get_json(base, "/api/import/batches")["batches"]
    assert {b["kind"]: b["row_count"] for b in batches}["transactions"] == 9

    # Chromium logs the 422 as a console error; nothing else is allowed.
    assert _unexpected(console_errors, "status of 422") == []
    assert page_errors == []


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
