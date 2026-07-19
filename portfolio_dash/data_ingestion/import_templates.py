"""Downloadable CSV import templates — annotated header + worked example rows per kind.

SINGLE SOURCE OF TRUTH: each kind's header is the column-order constant defined next to that
kind's parser (:data:`csv_import.TRANSACTION_COLUMNS`, :data:`dividend_import.DIVIDEND_COLUMNS`,
:data:`fx_import.FX_COLUMNS`, :data:`opening_import.OPENING_COLUMNS`).  A parser column rename
that is not mirrored here is caught by the round-trip guard test — the generated template must
re-parse through the real preview builder (after the import-seam header canonicalization) with
zero ``parse_error`` rows (tests/contract/test_import_template.py).

FU-D19 — the rendered header is ANNOTATED: the date column carries its ISO format hint
(``date(YYYY-MM-DD)`` / ``build_date(YYYY-MM-DD)``) and every optional column is marked
``(選填)``, so a user editing in Excel sees the expected date shape and what may be blank.  The
parsers canonicalize headers (:func:`csv_import.canonical_header` strips the annotation), so the
annotated template round-trips.  Required-vs-optional is declared in :data:`OPTIONAL_COLUMNS`
(mirrors each parser's own required set) and the date column in :data:`DATE_COLUMN_BY_KIND`.

The rendered text uses CRLF line endings and carries NO BOM; the HTTP layer
(:mod:`api.routers.input_center`) prepends the UTF-8 BOM so Excel opens the Chinese ``note``
column cleanly.  The importers strip a leading BOM defensively, so a downloaded-then-reuploaded
template round-trips whether or not the BOM survives the editor.
"""

import csv
import io

from portfolio_dash.data_ingestion.csv_import import TRANSACTION_COLUMNS
from portfolio_dash.data_ingestion.dividend_import import DIVIDEND_COLUMNS
from portfolio_dash.data_ingestion.fx_import import FX_COLUMNS
from portfolio_dash.data_ingestion.opening_import import OPENING_COLUMNS

TEMPLATE_KINDS: tuple[str, ...] = ("transactions", "dividends", "fx", "openings")

# The date column per kind (drives both the header annotation and the router's date
# normalization). Mirrors each parser's date field: transactions/dividends/fx use ``date``,
# opening_inventory uses ``build_date``.
DATE_COLUMN_BY_KIND: dict[str, str] = {
    "transactions": "date",
    "dividends": "date",
    "fx": "date",
    "openings": "build_date",
}

# Optional (may-be-blank) columns per kind — mirrors the required set each parser enforces:
# transactions require account/symbol/side/date/shares/price; dividends require
# account/symbol/date/type/gross; fx requires every column; openings require all but the
# derived original_cost_total. Marked ``(選填)`` in the downloadable template header.
OPTIONAL_COLUMNS: dict[str, frozenset[str]] = {
    "transactions": frozenset({"fee", "tax", "daytrade", "note"}),
    "dividends": frozenset({"withholding", "net", "reinvest_shares", "reinvest_price"}),
    "fx": frozenset(),
    "openings": frozenset({"original_cost_total"}),
}

# Example rows align POSITIONALLY with each kind's column constant. Fixed recent ISO dates;
# accounts + symbols are the seeded ids (tw_broker / schwab / moomoo_my_us / moomoo_my_my,
# 2330 / AAPL / a MY ETF) so the guard test resolves every reference cleanly. is_etf is NOT
# a column — it comes from the instrument registry (hence the MY-ETF row's note).
_TRANSACTION_ROWS: list[list[str]] = [
    # TW buy — fee/tax auto-computed (blank -> account fee-rule set fills them).
    ["tw_broker", "2330", "buy", "2026-07-10", "1000", "612.5", "", "", "", ""],
    # TW sell, day-trade (當沖) -> 0.15% sell tax; daytrade flag = 1.
    ["tw_broker", "2330", "sell", "2026-07-13", "1000", "620", "", "", "1", "當沖"],
    # Schwab US sell — SEC/TAF regulatory fees auto-computed on the sell side.
    ["schwab", "AAPL", "sell", "2026-07-13", "5", "210", "", "", "", ""],
    # Moomoo MY (US market) buy.
    ["moomoo_my_us", "AAPL", "buy", "2026-07-14", "3", "205", "", "", "", ""],
    # Moomoo MY (MY market) ETF buy — the ETF stamp exemption keys off the registry flag.
    ["moomoo_my_my", "0800EA", "buy", "2026-07-14", "100", "1.25", "", "", "",
     "ETF 以標的登錄為準"],
    # Manual fee + tax override — both columns supplied -> auto-compute skipped for this row.
    ["tw_broker", "2330", "sell", "2026-07-15", "500", "620", "20", "5", "", "手動覆寫費稅"],
]

_DIVIDEND_ROWS: list[list[str]] = [
    # TW cash dividend (net = gross, single-tier) — reduces adjusted cost.
    ["tw_broker", "2330", "2026-07-10", "CASH", "5000", "", "", "", ""],
    # US DRIP — 30% withholding + net auto-computed; reinvest_shares from net / reinvest_price.
    ["schwab", "AAPL", "2026-07-11", "DRIP", "100", "", "", "", "150"],
]

_FX_ROWS: list[list[str]] = [
    # Schwab funds USD from TWD; Moomoo funds USD from MYR.
    ["schwab", "2026-07-10", "TWD", "32000", "USD", "1000"],
    ["moomoo_my_us", "2026-07-11", "MYR", "4400", "USD", "1000"],
]

_OPENING_ROWS: list[list[str]] = [
    # original_cost_total blank -> computed as avg * shares; supplied on the second row.
    ["tw_broker", "2330", "1000", "500", "2026-01-02", ""],
    ["schwab", "AAPL", "10", "100", "2026-01-02", "1000"],
]

_HEADERS: dict[str, list[str]] = {
    "transactions": TRANSACTION_COLUMNS,
    "dividends": DIVIDEND_COLUMNS,
    "fx": FX_COLUMNS,
    "openings": OPENING_COLUMNS,
}
_ROWS: dict[str, list[list[str]]] = {
    "transactions": _TRANSACTION_ROWS,
    "dividends": _DIVIDEND_ROWS,
    "fx": _FX_ROWS,
    "openings": _OPENING_ROWS,
}


def template_columns(kind: str) -> list[str]:
    """Canonical (un-annotated) header for *kind*; raises ``KeyError`` for an unknown kind."""
    return _HEADERS[kind]


def _annotate(kind: str, col: str) -> str:
    """A single column with its FU-D19 annotation: the date column carries its ISO hint,
    optional columns are marked ``(選填)``; a required non-date column is unchanged."""
    if col == DATE_COLUMN_BY_KIND.get(kind):
        return f"{col}(YYYY-MM-DD)"
    if col in OPTIONAL_COLUMNS[kind]:
        return f"{col}(選填)"
    return col


def annotated_columns(kind: str) -> list[str]:
    """The rendered template header for *kind* — canonical order, with FU-D19 annotations.
    ``canonical_header`` strips the annotations back to :func:`template_columns`, so the
    annotated template still round-trips through every kind's parser."""
    return [_annotate(kind, c) for c in _HEADERS[kind]]


def template_filename(kind: str) -> str:
    """Download filename for *kind*'s template (e.g. ``import_template_transactions.csv``)."""
    return f"import_template_{kind}.csv"


def render_import_template(kind: str) -> str:
    """Render *kind*'s template as CSV text (annotated header row + example rows), CRLF, NO BOM.

    Raises ``KeyError`` for an unknown kind (the router validates against
    :data:`TEMPLATE_KINDS` first, so callers see a 400 rather than a 500).
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(annotated_columns(kind))
    for row in _ROWS[kind]:
        writer.writerow(row)
    return buf.getvalue()
