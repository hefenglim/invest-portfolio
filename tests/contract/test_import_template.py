"""Contract + round-trip guard for the CSV import template download (FU-D16).

Two guarantees:
1. ``GET /api/import/template?kind=…`` serves a UTF-8-with-BOM, CRLF ``text/csv`` whose header
   row IS the parser's own column constant, with a download filename; unknown kind -> 400.
2. THE POINT OF THE SINGLE SOURCE: every generated template must re-parse through the REAL
   preview builder with ZERO ``parse_error`` / ``unknown_account`` rows — so a parser column
   rename that is not mirrored in the template header is caught here.
"""

import sqlite3
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.csv_import import (
    build_transaction_preview,
    normalize_import_csv,
)
from portfolio_dash.data_ingestion.dividend_import import build_dividend_preview
from portfolio_dash.data_ingestion.fx_import import build_fx_preview
from portfolio_dash.data_ingestion.import_templates import (
    DATE_COLUMN_BY_KIND,
    TEMPLATE_KINDS,
    annotated_columns,
    render_import_template,
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
    # Soft warnings (duplicate / sell-exceeds / fuzzy) would be acceptable; a parse_error or
    # unknown_account means the template header/columns drifted from the parser.
    assert "parse_error" not in kinds, f"{kind} parse_error: {kinds}"
    assert "unknown_account" not in kinds, f"{kind} unknown_account: {kinds}"


def test_transactions_template_is_fully_clean(template_conn: sqlite3.Connection) -> None:
    """With every referenced symbol registered, the six example rows carry NO hard issue —
    the annotated template is directly writable, not just parseable."""
    preview = _built("transactions", template_conn, render_import_template("transactions"))
    assert len(preview.rows) == 6
    hard = [(r.index, [i.kind for i in r.issues]) for r in preview.rows if r.has_hard_issue]
    assert not hard, f"unexpected hard-issue rows: {hard}"


def test_template_with_bom_prefix_still_parses(template_conn: sqlite3.Connection) -> None:
    """The served bytes carry a BOM; a download->re-upload/paste round-trip must not break
    the header (the normalize seam lstrips a leading BOM before canonicalizing)."""
    preview = _built(
        "transactions", template_conn, _BOM + render_import_template("transactions")
    )
    assert len(preview.rows) == 6
    assert "parse_error" not in _issue_kinds(preview)
