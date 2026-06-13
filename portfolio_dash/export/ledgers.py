"""Four-ledger zip export (spec 02): raw table dumps + fee rules + manifest."""

import json
import sqlite3
from datetime import datetime

from portfolio_dash.api.serialize import to_wire
from portfolio_dash.data_ingestion.config_seed import FEE_RULES
from portfolio_dash.export.artifact import ExportArtifact, csv_blob, zip_artifact

_LEDGER_TABLES = ["transactions", "dividends", "fx_conversions", "opening_inventory"]
_SCHEMA_VERSION = 1


def _dump_table(conn: sqlite3.Connection, table: str) -> tuple[bytes, int]:
    """Raw `SELECT *` dump: header = DB columns, one row per record. (table is from a
    fixed allow-list, never user input.)"""
    cur = conn.execute(f"SELECT * FROM {table}")
    header = [c[0] for c in cur.description]
    rows = [["" if v is None else str(v) for v in row] for row in cur.fetchall()]
    return csv_blob(header, rows), len(rows)


def build_ledgers_zip(conn: sqlite3.Connection, *, now: datetime) -> ExportArtifact:
    as_of = now.date().isoformat()
    files: dict[str, bytes] = {}
    counts: dict[str, int] = {}
    for table in _LEDGER_TABLES:
        blob, n = _dump_table(conn, table)
        files[f"{table}.csv"] = blob
        counts[table] = n
    fee_snapshot = {name: to_wire(rs.model_dump()) for name, rs in FEE_RULES.items()}
    files["fee_rules_snapshot.json"] = json.dumps(
        fee_snapshot, ensure_ascii=False, indent=2).encode("utf-8")
    manifest = {"as_of": as_of, "schema_version": _SCHEMA_VERSION,
                "generated": now.isoformat(), "counts": counts}
    files["manifest.json"] = json.dumps(
        manifest, ensure_ascii=False, indent=2).encode("utf-8")
    return zip_artifact(f"ledgers_{as_of}.zip", files)
