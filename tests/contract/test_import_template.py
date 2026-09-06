"""Contract + round-trip guard for the CSV import template download (FU-D16).

Two guarantees:
1. ``GET /api/import/template?kind=…`` serves a UTF-8-with-BOM, CRLF ``text/csv`` whose header
   row IS the parser's own column constant, with a download filename; unknown kind -> 400.
2. THE POINT OF THE SINGLE SOURCE: every generated template must re-parse through the REAL
   preview builder with ZERO ``parse_error`` / ``unknown_account`` rows — so a parser column
   rename that is not mirrored in the template header is caught here.
"""

import csv
import io
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.api.routers.input_center import _BUILDERS
from portfolio_dash.data_ingestion.csv_import import normalize_import_csv
from portfolio_dash.data_ingestion.import_templates import (
    _TRANSACTION_ROWS,
    DATE_COLUMN_BY_KIND,
    TEMPLATE_KINDS,
    annotated_columns,
    render_import_template,
    template_columns,
)
from portfolio_dash.data_ingestion.preview import ImportPreview
from portfolio_dash.data_ingestion.store import upsert_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument

_BOM = "\ufeff"


def _built(kind: str, conn: sqlite3.Connection, text: str) -> ImportPreview:
    """Parse *text* the way the runtime does: normalize (canonical headers + ISO dates) at the
    import seam, then hand the clean CSV to the kind's ISO-only builder.

    The builder comes from the PRODUCTION map (``input_center._BUILDERS``), not from a copy
    kept here. This file used to restate the five builders, which made it a sixth
    registration point that the F-28 comment does not list and no guard covers \u2014 and the
    first kind whose builder needs an injected dependency (``cash``, whose withdraw guard
    takes its pool arithmetic from ``api/``) proved the cost: the copy would have exercised
    a builder the runtime never calls, so "re-parses through the real preview builder" would
    have quietly stopped being true.
    """
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
    """W7: the SPLIT example is fully clean; the other two are stopped for ONE reason each.

    Both name placeholder destinations because the seed has no second same-currency
    instrument to point at — and after D48a the two are stopped at DIFFERENT tiers, which is
    the ruling made visible:

    * **EXCHANGE → NEWCO** stays E10's HARD ``unregistered_symbol``. An exchange's
      destination is an existing listed security, so "register it first" is still right.
    * **SPINOFF → SPINCO** is now the SOFT ``spinoff_child_autoregister``: the child does not
      exist until this event, so the row commits once acknowledged and creates it.

    Pinning both tiers here stops the template from quietly acquiring a second defect behind
    the expected one, and stops the narrowing from leaking to the kind it was not given for.
    """
    preview = _built("corporate_actions", template_conn,
                     render_import_template("corporate_actions"))
    assert len(preview.rows) == 3
    assert not preview.rows[0].issues, [i.kind for i in preview.rows[0].issues]

    exchange, spinoff = preview.rows[1], preview.rows[2]
    assert {i.kind for i in exchange.issues if not i.needs_confirm} == {
        "unregistered_symbol"}
    assert {i.kind for i in spinoff.issues if not i.needs_confirm} == set()
    assert "spinoff_child_autoregister" in {
        i.kind for i in spinoff.issues if i.needs_confirm}


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


@pytest.mark.parametrize("kind", TEMPLATE_KINDS)
def test_every_example_row_is_exactly_as_wide_as_the_header(kind: str) -> None:
    """M4-04: a template is a PRODUCT THE USER FILLS IN, so a short example row is bad data
    waiting to happen — open it in Excel, type under the last column, and every cell from
    that point on is one column left of where the parser will read it.

    Measured 2026-09-02 on the DIVIDENDS template: header 10 fields, both example rows 9.
    ``ex_date`` was appended to ``DIVIDEND_COLUMNS`` (R6, review ⑧) and neither
    ``_DIVIDEND_ROWS`` nor ``OPTIONAL_COLUMNS['dividends']`` was updated with it — so it was
    also the only blank-able column in any template with no ``(選填)`` marker. Every other
    kind was already 11/11/11, 6/6/6, 6/6/6, 9/9/9, 7/7/7.

    The round-trip guard above could not see it: ``csv.DictReader`` pads a short row with
    ``None`` and the missing column was optional, so the row parsed clean.

    Asserted on the RENDERED text rather than on ``_ROWS``, because that is the artefact the
    owner downloads (annotated header, CRLF, quoting and all).
    """
    rows = list(csv.reader(io.StringIO(render_import_template(kind))))
    header, examples = rows[0], rows[1:]
    assert examples, f"{kind}: template carries no example row"
    widths = {i: len(r) for i, r in enumerate(examples, start=1) if len(r) != len(header)}
    assert not widths, (
        f"{kind}: header has {len(header)} columns {header}; "
        f"example row(s) {widths} disagree")


@pytest.mark.parametrize("kind", TEMPLATE_KINDS)
def test_every_blank_able_column_is_marked_optional(kind: str) -> None:
    """The other half of M4-04: a column an example row leaves EMPTY must carry ``(選填)``.

    An unmarked column that the template itself leaves blank tells the owner two opposite
    things at once — which is how ``ex_date`` read before this: required-looking, and blank
    in both examples.
    """
    rows = list(csv.reader(io.StringIO(render_import_template(kind))))
    header, examples = rows[0], rows[1:]
    for pos, name in enumerate(header):
        # A cell the row does not reach counts as blank too —「the example leaves this column
        # empty」 is the same fact whether the cell is "" or absent, and ``ex_date`` was
        # absent, which is exactly how it dodged this question for a release.
        blank_somewhere = any(pos >= len(r) or r[pos] == "" for r in examples)
        if blank_somewhere:
            assert "(選填)" in name or "(YYYY-MM-DD)" in name, (
                f"{kind}: column {name!r} is blank in an example row but is not marked 選填")
