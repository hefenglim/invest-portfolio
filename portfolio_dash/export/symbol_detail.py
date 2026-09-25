"""Symbol-detail dividend-history export (reconciliation channel).

Source of truth: the dividend ledger via ``data_ingestion.store.list_dividends`` — the
SAME store rows the symbol drawer's 配息史 section renders (``GET /api/symbol/{symbol}
/detail`` builds ``dividend_events`` from exactly this call). This builder reads the
store directly (not the HTTP layer) and serializes the ledger dividends at source
precision.

Retires the client-side display dump (``web/export.js`` ``pdExport`` over the drawer's
``dividend_events`` array) as the reconciliation data source. Per the owner directive
(2026-07-14) the export comes straight from the ledger, not from rendered/serialized
drawer values. An unknown symbol (not a registered instrument) is rejected by the
router with 400 — the builder returns ``None`` to signal that.
"""

import sqlite3
from datetime import date

from portfolio_dash.data_ingestion.store import list_dividends, list_instruments
from portfolio_dash.export.artifact import ExportArtifact, csv_artifact
from portfolio_dash.shared.models.enums import DividendType
from portfolio_dash.shared.models.ledger import dividend_effective_date, pending_from
from portfolio_dash.shared.wire import decimal_str

# Lowercase wire type, identical to api/routers/symbol.py::_DIV_TYPE_WIRE.
_DIV_TYPE_WIRE = {
    DividendType.CASH: "cash",
    DividendType.STOCK: "stock",
    DividendType.DRIP: "drip",
    DividendType.NET: "net",
}

# Mirrors the drawer's 配息史 columns (date/type/gross/net/reinvest/ccy) + withholding
# (the reconciliation channel keeps the full ledger row, not just the displayed cells).
# ``counts_from`` (DEF-056 R5, appended LAST so every earlier column keeps its index): the
# drawer badges a dividend that does not count yet (「未來日期：YYYY-MM-DD 起計入」), so the
# channel that reconciles that table says the same — the day it starts to count, empty when
# it already does. Not an import format, so no round trip to keep (unlike the ledger CSVs).
_COLUMNS = [
    "date", "type", "gross", "withholding", "net",
    "reinvest_shares", "reinvest_price", "ccy", "counts_from",
]


def build_symbol_detail_csv(
    conn: sqlite3.Connection, *, symbol: str, today: date
) -> ExportArtifact | None:
    """``today`` is the SERVER's valuation day (the request clock), as for the drawer."""
    instruments = {i.symbol: i for i in list_instruments(conn)}
    inst = instruments.get(symbol)
    if inst is None:
        return None  # unknown symbol -> router answers 400
    ccy = inst.quote_ccy.value
    rows: list[list[str]] = []
    for d in list_dividends(conn, symbol=symbol):
        pending = pending_from(dividend_effective_date(d.type, d.date, d.ex_date), today)
        rows.append([
            d.date.isoformat(),
            _DIV_TYPE_WIRE[DividendType(d.type)],
            decimal_str(d.gross),
            decimal_str(d.withholding),
            decimal_str(d.net),
            "" if d.reinvest_shares is None else decimal_str(d.reinvest_shares),
            "" if d.reinvest_price is None else decimal_str(d.reinvest_price),
            ccy,
            pending.isoformat() if pending is not None else "",
        ])
    return csv_artifact(f"{symbol}_dividends.csv", header=_COLUMNS, rows=rows)
