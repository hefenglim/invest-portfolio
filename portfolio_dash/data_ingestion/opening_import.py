"""CSV import for opening_inventory rows — reuses the preview/commit infrastructure."""

import csv
import io
import sqlite3
from datetime import date
from decimal import Decimal, InvalidOperation

from portfolio_dash.data_ingestion.preview import ImportPreview, PreviewRow
from portfolio_dash.data_ingestion.resolve import ResolutionStatus, resolve
from portfolio_dash.data_ingestion.store import upsert_opening
from portfolio_dash.data_ingestion.validate import Issue


def build_opening_preview(conn: sqlite3.Connection, csv_text: str) -> ImportPreview:
    """Parse *csv_text* into an :class:`ImportPreview` of opening_inventory rows.

    Expected columns: account, symbol, shares, original_avg_cost, build_date.
    Optional column: original_cost_total (computed as avg * shares when omitted).
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    rows: list[PreviewRow] = []
    for idx, raw0 in enumerate(reader):
        raw: dict[str, str] = {k.strip(): (v or "").strip() for k, v in raw0.items()}
        issues: list[Issue] = []

        # --- parse required fields ---
        try:
            account_id = raw["account"]
            symbol = raw["symbol"]
            shares = Decimal(raw["shares"])
            avg = Decimal(raw["original_avg_cost"])
            build = date.fromisoformat(raw["build_date"])
            raw_total = raw.get("original_cost_total", "")
            total = Decimal(raw_total) if raw_total else avg * shares
        except (KeyError, ValueError, InvalidOperation) as exc:
            rows.append(
                PreviewRow(
                    index=idx,
                    raw=raw,
                    issues=[Issue(kind="parse_error", message=str(exc))],
                )
            )
            continue

        # --- validate account exists ---
        if (
            conn.execute(
                "SELECT 1 FROM accounts WHERE account_id=?", (account_id,)
            ).fetchone()
            is None
        ):
            issues.append(
                Issue(kind="unknown_account", message=f"unknown account {account_id}")
            )

        # --- validate shares positive ---
        if shares <= 0:
            issues.append(
                Issue(kind="non_positive_shares", message="shares must be > 0")
            )

        # --- warn if symbol cannot be resolved (soft — needs confirm) ---
        if resolve(conn, symbol).status is ResolutionStatus.NEEDS_AI:
            issues.append(
                Issue(
                    kind="symbol_unresolved",
                    needs_confirm=True,
                    message=f"unresolved {symbol}",
                )
            )

        payload: dict[str, str] = {
            "account_id": account_id,
            "symbol": symbol,
            "shares": str(shares),
            "original_avg_cost": str(avg),
            "original_cost_total": str(total),
            "build_date": build.isoformat(),
        }
        rows.append(PreviewRow(index=idx, raw=raw, payload=payload, issues=issues))

    return ImportPreview(rows=rows)


def write_opening_row(
    conn: sqlite3.Connection, row: PreviewRow, *, commit: bool = True
) -> int:
    """Persist one accepted opening_inventory row and return its row index.

    Uses row index (not an autoincrement id) as the written marker, because
    opening_inventory uses a composite PK with no surrogate key.

    ``commit`` is forwarded to the store upsert; the batch path passes ``commit=False``
    so the whole batch commits once (all-or-nothing, #1).
    """
    p = row.payload
    upsert_opening(
        conn,
        account_id=p["account_id"],
        symbol=p["symbol"],
        shares=Decimal(p["shares"]),
        original_avg_cost=Decimal(p["original_avg_cost"]),
        original_cost_total=Decimal(p["original_cost_total"]),
        build_date=date.fromisoformat(p["build_date"]),
        commit=commit,
    )
    return row.index
