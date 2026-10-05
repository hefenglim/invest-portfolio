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

X1 (owner ruling 2026-10-05): a row the replay refused is listed, as on the drawer and the
ledger tab, and its ``unbooked`` cell reads 「未入帳（待釐清）」 — the replay's own answer
(``portfolio.returns.replay_dividend_refusals``, one replay per export), never re-derived.
"""

import sqlite3
from datetime import date

from portfolio_dash.data_ingestion.store import (
    list_dividends,
    list_instruments,
    load_ledger_bundle,
)
from portfolio_dash.export.artifact import ExportArtifact, csv_artifact
from portfolio_dash.portfolio.cash import UNBOOKED_DIVIDEND_NOTE
from portfolio_dash.portfolio.returns import replay_dividend_refusals
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
# ``unbooked`` (X1, 2026-10-05) — 「未入帳（待釐清）」 on a row the replay refused, empty on every
# other — sits before ``counts_from``, which stays last as DEF-056's readers key on it (the
# 現金收支明細 CSV's own placement); every earlier column keeps its index.
_COLUMNS = [
    "date", "type", "gross", "withholding", "net",
    "reinvest_shares", "reinvest_price", "ccy", "unbooked", "counts_from",
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
    stored = list_dividends(conn)
    refusals = replay_dividend_refusals(load_ledger_bundle(
        conn, dividends=stored, instruments=instruments))
    rows: list[list[str]] = []
    for d in (r for r in stored if r.symbol == symbol):
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
            "" if refusals.why(d) is None else UNBOOKED_DIVIDEND_NOTE,
            pending.isoformat() if pending is not None else "",
        ])
    return csv_artifact(f"{symbol}_dividends.csv", header=_COLUMNS, rows=rows)
