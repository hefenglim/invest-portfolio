"""Contract + round-trip guard for the CSV import template download (FU-D16).

Two guarantees:
1. ``GET /api/import/template?kind=…`` serves a UTF-8-with-BOM, CRLF ``text/csv`` whose header
   row IS the parser's own column constant, with a download filename; unknown kind -> 400.
2. THE POINT OF THE SINGLE SOURCE: every generated template must re-parse through the REAL
   preview builder with ZERO ``parse_error`` / ``unknown_account`` rows — so a parser column
   rename that is not mirrored in the template header is caught here.
"""

import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.corporate_action_import import (
    build_corporate_action_preview,
)
from portfolio_dash.data_ingestion.csv_import import (
    build_transaction_preview,
    normalize_import_csv,
)
from portfolio_dash.data_ingestion.dividend_import import build_dividend_preview
from portfolio_dash.data_ingestion.fx_import import build_fx_preview
from portfolio_dash.data_ingestion.import_templates import (
    _TRANSACTION_ROWS,
    DATE_COLUMN_BY_KIND,
    TEMPLATE_KINDS,
    annotated_columns,
    render_import_template,
    template_columns,
)
from portfolio_dash.data_ingestion.opening_import import build_opening_preview
from portfolio_dash.data_ingestion.preview import ImportPreview
from portfolio_dash.data_ingestion.store import upsert_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument

_BOM = "\ufeff"

_BUILDERS = {
    "transactions": build_transaction_preview,
    "dividends": build_dividend_preview,
    "fx": build_fx_preview,
    "openings": build_opening_preview,
    "corporate_actions": build_corporate_action_preview,
}


def _built(kind: str, conn: sqlite3.Connection, text: str) -> ImportPreview:
    """Parse *text* the way the runtime does: normalize (canonical headers + ISO dates) at the
    import seam, then hand the clean CSV to the kind's ISO-only builder."""
    norm = normalize_import_csv(text, DATE_COLUMN_BY_KIND[kind])
    return _BUILDERS[kind](conn, norm.text)


@pytest.fixture
def template_conn(golden_db: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """The golden seed (accounts + 2330 + AAPL + holdings + USD/MYR fx) PLUS a registered MY
    ETF, so every symbol/account the transactions template references resolves cleanly."""
    upsert_instrument(
        golden_db,
        Instrument(
            symbol="0800EA", market=Market.MY, quote_ccy=Currency.MYR, sector="ETF",
            name="TradePlus S&P New China Tracker", board=".KL", is_etf=True,
        ),
    )
    golden_db.commit()
    yield golden_db


# --- 1. endpoint shape ---------------------------------------------------------------


@pytest.mark.parametrize("kind", TEMPLATE_KINDS)
def test_template_endpoint_shape(api_client: TestClient, kind: str) -> None:
    r = api_client.get("/api/import/template", params={"kind": kind})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    text = r.content.decode("utf-8")
    assert text.startswith(_BOM)  # Excel-friendly BOM so Chinese `note` opens cleanly
    assert "\r\n" in text  # CRLF line endings (reconciliation-grade, matches export)
    header_line = text[len(_BOM):].split("\r\n")[0]
    # FU-D19: the rendered header carries annotations (date format + 選填 markers).
    assert header_line == ",".join(annotated_columns(kind))
    assert f"import_template_{kind}.csv" in r.headers["content-disposition"]


def test_template_header_carries_date_and_optional_annotations() -> None:
    txn = annotated_columns("transactions")
    assert "date(YYYY-MM-DD)" in txn
    assert "fee(選填)" in txn and "note(選填)" in txn
    # openings annotates the build_date column, not `date`.
    assert "build_date(YYYY-MM-DD)" in annotated_columns("openings")
    # fx has no optional columns -> no 選填 markers, but still the date hint.
    assert "date(YYYY-MM-DD)" in annotated_columns("fx")
    assert not any("選填" in c for c in annotated_columns("fx"))


def test_template_endpoint_default_kind_is_transactions(api_client: TestClient) -> None:
    r = api_client.get("/api/import/template")
    assert r.status_code == 200
    body = r.content.decode("utf-8")[len(_BOM):]
    assert body.split("\r\n")[0] == ",".join(annotated_columns("transactions"))


def test_template_endpoint_unknown_kind_400(api_client: TestClient) -> None:
    r = api_client.get("/api/import/template", params={"kind": "nope"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "validation_error"


# --- 2. round-trip guard: the generated template re-parses with no hard classification ---


def _issue_kinds(preview: ImportPreview) -> list[str]:
    return [i.kind for row in preview.rows for i in row.issues]


@pytest.mark.parametrize("kind", TEMPLATE_KINDS)
def test_template_roundtrips_through_real_builder(
    template_conn: sqlite3.Connection, kind: str
) -> None:
    # FU-D19: the ANNOTATED template must round-trip through the real seam (normalize -> builder).
    preview = _built(kind, template_conn, render_import_template(kind))
    assert preview.rows, f"{kind}: template produced no data rows"
    kinds = _issue_kinds(preview)
    # Soft warnings (duplicate / sell-exceeds) would be acceptable; a parse_error or
    # unknown_account means the template header/columns drifted from the parser.
    assert "parse_error" not in kinds, f"{kind} parse_error: {kinds}"
    assert "unknown_account" not in kinds, f"{kind} unknown_account: {kinds}"


def test_transactions_template_is_fully_clean(template_conn: sqlite3.Connection) -> None:
    """With every referenced symbol registered, EVERY example row carries NO hard issue —
    the annotated template is directly writable, not just parseable.

    The count is derived from the template's own row constant, not written as a literal: a
    literal makes adding an example row look like a regression, which is how the number gets
    "fixed" instead of the row being checked.
    """
    preview = _built("transactions", template_conn, render_import_template("transactions"))
    assert len(preview.rows) == len(_TRANSACTION_ROWS)
    hard = [(r.index, [i.kind for i in r.issues]) for r in preview.rows if r.has_hard_issue]
    assert not hard, f"unexpected hard-issue rows: {hard}"


def test_corporate_action_template_split_row_is_directly_writable(
    template_conn: sqlite3.Connection
) -> None:
    """W7: the SPLIT example is fully clean; the other two are refused for ONE reason.

    The EXCHANGE / SPINOFF examples name placeholder destinations because the seed has no
    second same-currency instrument to point at. That they are refused is E10 working
    (D19 — keyed on REGISTRATION, a database fact, never on the shape of the string), and
    pinning the reason here stops the template from quietly acquiring a second defect
    behind the one that is expected.
    """
    preview = _built("corporate_actions", template_conn,
                     render_import_template("corporate_actions"))
    assert len(preview.rows) == 3
    assert not preview.rows[0].issues, [i.kind for i in preview.rows[0].issues]
    for row in preview.rows[1:]:
        assert {i.kind for i in row.issues if not i.needs_confirm} == {
            "unregistered_symbol"}


def test_template_with_bom_prefix_still_parses(template_conn: sqlite3.Connection) -> None:
    """The served bytes carry a BOM; a download->re-upload/paste round-trip must not break
    the header (the normalize seam lstrips a leading BOM before canonicalizing)."""
    preview = _built(
        "transactions", template_conn, _BOM + render_import_template("transactions")
    )
    assert len(preview.rows) == len(_TRANSACTION_ROWS)
    assert "parse_error" not in _issue_kinds(preview)


# ---------------------------------------------------------------------------
# The frontend restates the column set. Two surfaces, one fact -> a drift guard.
# ---------------------------------------------------------------------------

_WEB = Path(__file__).resolve().parents[2] / "web"


def _kind_columns(kind: str) -> list[str]:
    """Each kind's canonical column list, from the parser constant that owns it."""
    return list(template_columns(kind))


@pytest.mark.parametrize("kind", TEMPLATE_KINDS)
def test_the_dropzone_hint_names_every_column_the_parser_reads(kind: str) -> None:
    """``web/input.js``'s ``CSV_HINTS`` is a SECOND statement of the column set.

    Added 2026-08-11, after ``short_sale`` was added to the transactions parser and the hint
    would have kept telling users the old set. This project has the two-surfaces-one-fact
    drift already on record (the settings dual-surface class), and a hint that omits a column
    is worse than no hint: the user believes they have the full header and their file is
    silently short a field the parser would have accepted.

    The assertion is DIRECTIONAL on purpose — every parser column must appear in the hint,
    while the hint may carry extra prose (「選填」 markers, the ratio warning, format hints).
    A set-equality test would fail on the annotations that make the hint useful.
    """
    hint_source = (_WEB / "input.js").read_text(encoding="utf-8")
    match = re.search(rf"^\s*{re.escape(kind)}:\s*'(.*?)',?$", hint_source, re.M)
    assert match, f"no CSV_HINTS entry for {kind} in web/input.js"
    hint = match.group(1)
    missing = [c for c in _kind_columns(kind) if c not in hint]
    assert not missing, f"{kind}: hint omits {missing}"


def test_the_paste_placeholder_is_the_transactions_header() -> None:
    """``web/trades.html``'s paste box shows a bare comma header — a THIRD statement of it.

    It carries no annotations, so this one can be exact, and exactness is what makes it
    useful: a user pasting rows in placeholder order gets them in parser order.
    """
    html = (_WEB / "trades.html").read_text(encoding="utf-8")
    expected = ",".join(_kind_columns("transactions"))
    assert f'placeholder="{expected}"' in html, (
        f"web/trades.html paste placeholder is not the canonical header: {expected}")
