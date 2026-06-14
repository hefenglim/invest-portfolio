"""CSV import for fx_conversions — reuses the preview/commit infrastructure."""

import csv
import io
import sqlite3
from datetime import date
from decimal import Decimal, InvalidOperation

from portfolio_dash.data_ingestion.preview import ImportPreview, PreviewRow
from portfolio_dash.data_ingestion.store import insert_fx_conversion
from portfolio_dash.data_ingestion.validate import Issue
from portfolio_dash.shared.enums import Currency


def build_fx_preview(conn: sqlite3.Connection, csv_text: str) -> ImportPreview:
    """Parse *csv_text* into an :class:`ImportPreview` of fx_conversions rows.

    Expected columns: account, date, from_ccy, from_amount, to_ccy, to_amount.

    Hard issues (block commit):
    - ``unknown_account``: account not found in accounts table.
    - ``parse_error``: required column missing or unparseable.
    - ``non_positive_amount``: from_amount or to_amount <= 0.
    - ``same_currency``: from_ccy == to_ccy.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    rows: list[PreviewRow] = []
    for idx, raw0 in enumerate(reader):
        raw: dict[str, str] = {k.strip(): (v or "").strip() for k, v in raw0.items()}
        issues: list[Issue] = []

        # --- parse required fields ---
        try:
            account_id = raw["account"]
            conv_date = date.fromisoformat(raw["date"])
            from_ccy = Currency(raw["from_ccy"])
            from_amount = Decimal(raw["from_amount"])
            to_ccy = Currency(raw["to_ccy"])
            to_amount = Decimal(raw["to_amount"])
        except (KeyError, ValueError, InvalidOperation) as exc:
            rows.append(
                PreviewRow(
                    index=idx,
                    raw=raw,
                    issues=[Issue(kind="parse_error", message=str(exc))],
                )
            )
            continue

        # --- validate account exists (hard) ---
        if (
            conn.execute(
                "SELECT 1 FROM accounts WHERE account_id=?", (account_id,)
            ).fetchone()
            is None
        ):
            issues.append(
                Issue(kind="unknown_account", message=f"unknown account {account_id!r}")
            )

        # --- validate amounts positive (hard) ---
        if from_amount <= 0:
            issues.append(
                Issue(kind="non_positive_amount", message="from_amount must be > 0")
            )
        if to_amount <= 0:
            issues.append(
                Issue(kind="non_positive_amount", message="to_amount must be > 0")
            )

        # --- validate currencies differ (hard) ---
        if from_ccy == to_ccy:
            issues.append(
                Issue(
                    kind="same_currency",
                    message=f"from_ccy and to_ccy must differ (both {from_ccy.value})",
                )
            )

        payload: dict[str, str] = {
            "account_id": account_id,
            "date": conv_date.isoformat(),
            "from_ccy": from_ccy.value,
            "from_amount": str(from_amount),
            "to_ccy": to_ccy.value,
            "to_amount": str(to_amount),
        }
        rows.append(PreviewRow(index=idx, raw=raw, payload=payload, issues=issues))

    return ImportPreview(rows=rows)


def write_fx_row(
    conn: sqlite3.Connection, row: PreviewRow, *, commit: bool = True
) -> int:
    """Persist one accepted fx_conversions row and return its autoincrement id.

    ``commit`` is forwarded to the store insert; the batch path passes ``commit=False``
    so the whole batch commits once (all-or-nothing, #1).
    """
    p = row.payload
    return insert_fx_conversion(
        conn,
        account_id=p["account_id"],
        date=date.fromisoformat(p["date"]),
        from_ccy=Currency(p["from_ccy"]),
        from_amount=Decimal(p["from_amount"]),
        to_ccy=Currency(p["to_ccy"]),
        to_amount=Decimal(p["to_amount"]),
        commit=commit,
    )
