"""Downloadable CSV import templates — canonical header + worked example rows per kind.

SINGLE SOURCE OF TRUTH: each kind's header is the column-order constant defined next to that
kind's parser (:data:`csv_import.TRANSACTION_COLUMNS`, :data:`dividend_import.DIVIDEND_COLUMNS`,
:data:`fx_import.FX_COLUMNS`, :data:`opening_import.OPENING_COLUMNS`).  A parser column rename
that is not mirrored here is caught by the round-trip guard test — the generated template must
re-parse through the real preview builder with zero ``parse_error`` rows
(tests/contract/test_import_template.py).

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
    """Canonical header for *kind*; raises ``KeyError`` for an unknown kind."""
    return _HEADERS[kind]


def template_filename(kind: str) -> str:
    """Download filename for *kind*'s template (e.g. ``import_template_transactions.csv``)."""
    return f"import_template_{kind}.csv"


def render_import_template(kind: str) -> str:
    """Render *kind*'s template as CSV text (header row + example rows), CRLF, NO BOM.

    Raises ``KeyError`` for an unknown kind (the router validates against
    :data:`TEMPLATE_KINDS` first, so callers see a 400 rather than a 500).
    """
    header = _HEADERS[kind]
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(header)
    for row in _ROWS[kind]:
        writer.writerow(row)
    return buf.getvalue()
