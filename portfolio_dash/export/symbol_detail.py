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

from portfolio_dash.data_ingestion.store import list_dividends, list_instruments
from portfolio_dash.export.artifact import ExportArtifact, csv_artifact
from portfolio_dash.shared.models.enums import DividendType
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
_COLUMNS = [
    "date", "type", "gross", "withholding", "net",
    "reinvest_shares", "reinvest_price", "ccy",
]


def build_symbol_detail_csv(
    conn: sqlite3.Connection, *, symbol: str
) -> ExportArtifact | None:
    instruments = {i.symbol: i for i in list_instruments(conn)}
    inst = instruments.get(symbol)
    if inst is None:
        return None  # unknown symbol -> router answers 400
    ccy = inst.quote_ccy.value
    rows: list[list[str]] = []
    for d in list_dividends(conn, symbol=symbol):
        rows.append([
            d.date.isoformat(),
            _DIV_TYPE_WIRE[DividendType(d.type)],
            decimal_str(d.gross),
            decimal_str(d.withholding),
            decimal_str(d.net),
            "" if d.reinvest_shares is None else decimal_str(d.reinvest_shares),
            "" if d.reinvest_price is None else decimal_str(d.reinvest_price),
            ccy,
        ])
    return csv_artifact(f"{symbol}_dividends.csv", header=_COLUMNS, rows=rows)
