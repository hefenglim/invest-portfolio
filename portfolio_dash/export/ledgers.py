"""Ledger exports (spec 02): the four-ledger zip + single-ledger CSV.

Both dump the raw ledger tables (``SELECT *`` — DB columns verbatim, values byte-identical
to what is stored) so a reconciliation reproduces history exactly. The single-ledger CSV
(``build_ledger_csv``) backs the 交易帳本 page's per-tab 匯出 CSV button: it retires the
old client-side dump (``web/export.js`` scraping the rendered pane) as the data source.
Per the owner directive (2026-07-14) the export now comes straight from the ledger table
(all matching rows, source-precision strings), honoring the page's active tab (``kind``)
and date-range inputs (``from``/``to`` on the tab's natural date column).
"""

import json
import sqlite3
from datetime import datetime

from portfolio_dash.data_ingestion.config_seed import get_effective_fee_rules
from portfolio_dash.export.artifact import ExportArtifact, csv_artifact, csv_blob, zip_artifact
from portfolio_dash.shared.wire import to_wire

_LEDGER_TABLES = ["transactions", "dividends", "fx_conversions", "opening_inventory"]
_SCHEMA_VERSION = 1

# Single-ledger CSV: the page's tab `kind` -> (table, ISO-date column for from/to
# filtering). Date columns match the ledgers list router's per-endpoint filter column
# (trade_date / date / build_date). `kind` is validated by the router against this map.
LEDGER_KINDS: dict[str, tuple[str, str]] = {
    "transactions": ("transactions", "trade_date"),
    "dividends": ("dividends", "date"),
    "fx": ("fx_conversions", "date"),
    "opening": ("opening_inventory", "build_date"),
}


def _read_table(
    conn: sqlite3.Connection,
    table: str,
    *,
    date_col: str | None = None,
    frm: str | None = None,
    to: str | None = None,
) -> tuple[list[str], list[list[str]]]:
    """Raw ``SELECT *`` header + rows, optionally range-filtered on *date_col*.

    ``table``/``date_col`` come from fixed allow-lists (never user input); dates are ISO
    TEXT so a lexical string compare is the correct range test (matches the ledgers list
    router's ``_in_range``). Returns raw cell strings (NULL -> "").
    """
    # table/date_col come from a fixed allow-list (never user input); values are bound.
    sql = f"SELECT * FROM {table}"
    params: list[str] = []
    if date_col is not None and (frm or to):
        clauses: list[str] = []
        if frm:
            clauses.append(f"{date_col} >= ?")
            params.append(frm)
        if to:
            clauses.append(f"{date_col} <= ?")
            params.append(to)
        sql += " WHERE " + " AND ".join(clauses)
    cur = conn.execute(sql, params)
    header = [c[0] for c in cur.description]
    rows = [["" if v is None else str(v) for v in row] for row in cur.fetchall()]
    return header, rows


def _dump_table(conn: sqlite3.Connection, table: str) -> tuple[bytes, int]:
    """Raw dump as a CSV byte blob (for the zip) + its row count."""
    header, rows = _read_table(conn, table)
    return csv_blob(header, rows), len(rows)


def build_ledgers_zip(conn: sqlite3.Connection, *, now: datetime) -> ExportArtifact:
    as_of = now.date().isoformat()
    files: dict[str, bytes] = {}
    counts: dict[str, int] = {}
    for table in _LEDGER_TABLES:
        blob, n = _dump_table(conn, table)
        files[f"{table}.csv"] = blob
        counts[table] = n
    fee_snapshot = {
        name: to_wire(rs.model_dump())
        for name, rs in get_effective_fee_rules(conn).items()
    }
    files["fee_rules_snapshot.json"] = json.dumps(
        fee_snapshot, ensure_ascii=False, indent=2).encode("utf-8")
    manifest = {"as_of": as_of, "schema_version": _SCHEMA_VERSION,
                "generated": now.isoformat(), "counts": counts}
    files["manifest.json"] = json.dumps(
        manifest, ensure_ascii=False, indent=2).encode("utf-8")
    return zip_artifact(f"ledgers_{as_of}.zip", files)


def build_ledger_csv(
    conn: sqlite3.Connection, *, kind: str, frm: str | None, to: str | None
) -> ExportArtifact:
    """One reconciliation-grade CSV for a single ledger *kind*, range-filtered.

    Reuses the zip's per-ledger composition (``_read_table``): a raw ``SELECT *`` dump of
    the tab's table, filtered to [``frm``, ``to``] on the tab's date column. Caller
    (router) validates *kind* against :data:`LEDGER_KINDS`.
    """
    table, date_col = LEDGER_KINDS[kind]
    header, rows = _read_table(conn, table, date_col=date_col, frm=frm, to=to)
    tag = f"{frm or 'all'}_{to or 'all'}"
    return csv_artifact(f"ledger_{kind}_{tag}.csv", header=header, rows=rows)
