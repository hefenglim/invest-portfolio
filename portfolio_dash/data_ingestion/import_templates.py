"""Downloadable CSV import templates — annotated header + worked example rows per kind.

SINGLE SOURCE OF TRUTH: each kind's header is the column-order constant defined next to that
kind's parser (:data:`csv_import.TRANSACTION_COLUMNS`, :data:`dividend_import.DIVIDEND_COLUMNS`,
:data:`fx_import.FX_COLUMNS`, :data:`opening_import.OPENING_COLUMNS`,
:data:`corporate_action_import.CORPORATE_ACTION_COLUMNS`).  A parser column rename
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

from portfolio_dash.data_ingestion.corporate_action_import import CORPORATE_ACTION_COLUMNS
from portfolio_dash.data_ingestion.csv_import import TRANSACTION_COLUMNS
from portfolio_dash.data_ingestion.dividend_import import DIVIDEND_COLUMNS
from portfolio_dash.data_ingestion.fx_import import FX_COLUMNS
from portfolio_dash.data_ingestion.opening_import import OPENING_COLUMNS

# ⚠ A new kind is NOT one edit. Seven registration points (audit F-28), and every one of
# them fails quietly on its own: the parser module + its ``*_COLUMNS`` constant, then
# TEMPLATE_KINDS / DATE_COLUMN_BY_KIND / OPTIONAL_COLUMNS / _HEADERS / _ROWS below, then
# ``api/routers/input_center._BUILDERS`` and ``._WRITERS``, then ``web/input.js``'s
# CSV_KINDS + CSV_HINTS. Miss the router maps and the kind 400s as "未知 kind"; miss the
# frontend and it exists but is unreachable; miss DATE_COLUMN_BY_KIND and the import seam
# raises a KeyError on a date column that is not there.
TEMPLATE_KINDS: tuple[str, ...] = (
    "transactions", "dividends", "fx", "openings", "corporate_actions",
)

# The date column per kind (drives both the header annotation and the router's date
# normalization). Mirrors each parser's date field: transactions/dividends/fx use ``date``,
# opening_inventory uses ``build_date``.
DATE_COLUMN_BY_KIND: dict[str, str] = {
    "transactions": "date",
    "dividends": "date",
    "fx": "date",
    "openings": "build_date",
    "corporate_actions": "date",
}

# Optional (may-be-blank) columns per kind — mirrors the required set each parser enforces:
# transactions require account/symbol/side/date/shares/price; dividends require
# account/symbol/date/type/gross; fx requires every column; openings require
# account/symbol/shares/original_cost_total/build_date and treat original_avg_cost as the
# legacy-optional column (A6). Marked ``(選填)`` in the downloadable template header.
OPTIONAL_COLUMNS: dict[str, frozenset[str]] = {
    "transactions": frozenset({"fee", "tax", "daytrade", "note"}),
    "dividends": frozenset({"withholding", "net", "reinvest_shares", "reinvest_price"}),
    "fx": frozenset(),
    "openings": frozenset({"original_avg_cost"}),
    # cost_carry is SPINOFF-only (E8 rejects it on the other two kinds); note is free text.
    # ratio_to / ratio_from are BOTH required — never one `ratio` column (E6a, §3.1(ii)).
    "corporate_actions": frozenset({"cost_carry", "note"}),
}

# Example rows align POSITIONALLY with each kind's column constant. Fixed recent ISO dates;
# accounts + symbols are the seeded ids (tw_broker / schwab / moomoo_my, 2330 / AAPL / a MY
# ETF) so the guard test resolves every reference cleanly. moomoo_my is the merged dual-market
# account (Batch B), so it books BOTH a US-market and an MY-market example row. is_etf is NOT a
# column — it comes from the instrument registry (hence the MY-ETF row's note).
_TRANSACTION_ROWS: list[list[str]] = [
    # TW buy — fee/tax auto-computed (blank -> account fee-rule set fills them).
    ["tw_broker", "2330", "buy", "2026-07-10", "1000", "612.5", "", "", "", ""],
    # TW sell, day-trade (當沖) -> 0.15% sell tax; daytrade flag = 1.
    ["tw_broker", "2330", "sell", "2026-07-13", "1000", "620", "", "", "1", "當沖"],
    # Schwab US sell — SEC/TAF regulatory fees auto-computed on the sell side.
    ["schwab", "AAPL", "sell", "2026-07-13", "5", "210", "", "", "", ""],
    # Moomoo MY, US-market buy (settles USD; US fee/dividend rules bound to this market).
    ["moomoo_my", "AAPL", "buy", "2026-07-14", "3", "205", "", "", "", ""],
    # Moomoo MY, MY-market ETF buy — the ETF stamp exemption keys off the registry flag.
    ["moomoo_my", "0800EA", "buy", "2026-07-14", "100", "1.25", "", "", "",
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
    # Schwab funds USD from TWD; Moomoo MY funds USD from MYR.
    ["schwab", "2026-07-10", "TWD", "32000", "USD", "1000"],
    ["moomoo_my", "2026-07-11", "MYR", "4400", "USD", "1000"],
]

_OPENING_ROWS: list[list[str]] = [
    # A6: original_cost_total is REQUIRED (authoritative money of record); original_avg_cost is
    # legacy-optional. Row 1 supplies total only; row 2 also carries a matching legacy avg.
    ["tw_broker", "2330", "1000", "500000", "2026-01-02", ""],
    ["schwab", "AAPL", "10", "1000", "2026-01-02", "100"],
]

_CORPORATE_ACTION_ROWS: list[list[str]] = [
    # A forward SPLIT: 2330 is held in tw_broker only, so ONE row is the complete entry
    # (E13 counts holding accounts, not markets). Fully clean against the seeded ledger.
    ["tw_broker", "2026-06-01", "SPLIT", "2330", "2330", "10", "1", "",
     "一股分割為十股（10 換 1）"],
    # An EXCHANGE and a SPINOFF need a DESTINATION symbol, and the seed has no second
    # same-currency instrument to point at — so these two name placeholders. They are
    # REJECTED on re-upload with 「目的標的 … 尚未註冊」, which is E10 doing its job (D19:
    # keyed on registration, a database fact, never on the shape of the string). The shapes
    # are what the example is for: two integer terms, and cost_carry on the SPINOFF only.
    ["schwab", "2026-06-02", "EXCHANGE", "AAPL", "NEWCO", "2", "7", "",
     "併購換股：每 7 股換得 2 股 NEWCO（請改成實際已註冊的代號）"],
    ["schwab", "2026-06-03", "SPINOFF", "AAPL", "SPINCO", "1", "2", "0.3",
     "分拆：每 2 股配 1 股 SPINCO，母公司 30% 成本移轉給子公司"],
]

_HEADERS: dict[str, list[str]] = {
    "transactions": TRANSACTION_COLUMNS,
    "dividends": DIVIDEND_COLUMNS,
    "fx": FX_COLUMNS,
    "openings": OPENING_COLUMNS,
    "corporate_actions": CORPORATE_ACTION_COLUMNS,
}
_ROWS: dict[str, list[list[str]]] = {
    "transactions": _TRANSACTION_ROWS,
    "dividends": _DIVIDEND_ROWS,
    "fx": _FX_ROWS,
    "openings": _OPENING_ROWS,
    "corporate_actions": _CORPORATE_ACTION_ROWS,
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
