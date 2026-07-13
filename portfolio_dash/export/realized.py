"""Realized-P&L snapshot export (reconciliation channel).

Source of truth: the calculation core's ledger replay — ``build_dashboard(...).realized``
(``portfolio.results.RealizedPnL``), the SAME realized rows the dashboard 已實現損益
panel renders. This builder serializes those computed Decimals at source precision; it
computes no numbers of its own.

Retires the client-side display dump (``web/export.js`` ``pdExport`` over the rendered
已實現損益 table) as the reconciliation data source: that path emitted DISPLAY values
(zh-TW account names, presentation formatting) read out of the DOM. Per the owner
directive (2026-07-14) every 匯出 CSV must go through this backend channel so numbers
come straight from the Decimal core, not from rendered cells.
"""

import sqlite3
from datetime import datetime

from portfolio_dash.export.artifact import ExportArtifact, csv_artifact
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.wire import decimal_str

_COLUMNS = [
    "account_id", "symbol", "quote_ccy", "sell_date", "shares_sold",
    "proceeds_net", "original_cost_removed", "adjusted_cost_removed", "realized",
]


def build_realized_csv(
    conn: sqlite3.Connection, *, now: datetime, reporting: Currency
) -> ExportArtifact:
    data = build_dashboard(conn, now=now, reporting=reporting)
    rows: list[list[str]] = []
    for r in data.realized.rows:
        rows.append([
            r.account_id, r.symbol, r.quote_ccy.value, r.sell_date.isoformat(),
            decimal_str(r.shares_sold), decimal_str(r.proceeds_net),
            decimal_str(r.original_cost_removed), decimal_str(r.adjusted_cost_removed),
            decimal_str(r.realized),
        ])
    as_of = data.as_of.date().isoformat()
    footer = [f"as_of={as_of}, generated={now.isoformat()}"]
    return csv_artifact(f"realized_pnl_{as_of}.csv",
                        header=_COLUMNS, rows=rows, footer_lines=footer)
