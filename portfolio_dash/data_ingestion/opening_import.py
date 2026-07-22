"""CSV import for opening_inventory rows — reuses the preview/commit infrastructure."""

import csv
import io
import sqlite3
from datetime import date
from decimal import Decimal, InvalidOperation

from portfolio_dash.data_ingestion.preview import ImportPreview, PreviewRow
from portfolio_dash.data_ingestion.resolve import ResolutionStatus, resolve
from portfolio_dash.data_ingestion.store import upsert_opening
from portfolio_dash.data_ingestion.validate import Issue, alias_import_account
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.money import MINOR_UNITS

# Canonical CSV column order for the opening_inventory import — SINGLE SOURCE for the
# downloadable template header (see data_ingestion.import_templates).
# A6 (2026-07-21) inverted the contract: REQUIRED = account, symbol, shares,
# original_cost_total (the authoritative money of record), build_date; original_avg_cost is
# OPTIONAL (legacy) — a rounded average is never the authority (domain-ledger.md). Optional
# columns trail the required set; kept in lockstep with the DictReader keys by the round-trip
# guard test.
OPENING_COLUMNS: list[str] = [
    "account", "symbol", "shares", "original_cost_total", "build_date", "original_avg_cost",
]


def _minor_unit(ccy: str | None) -> Decimal:
    """One minor unit of the settlement currency (TWD -> 1, USD/MYR -> 0.01). Falls back to
    0.01 for an unknown/None ccy (the row already carries a hard ``unknown_account`` issue, so
    the mismatch check is moot there)."""
    try:
        minor = MINOR_UNITS[Currency(ccy)] if ccy else 2
    except (ValueError, KeyError):
        minor = 2
    return Decimal(1).scaleb(-minor)


def build_opening_preview(conn: sqlite3.Connection, csv_text: str) -> ImportPreview:
    """Parse *csv_text* into an :class:`ImportPreview` of opening_inventory rows.

    Required columns: account, symbol, shares, original_cost_total, build_date.
    Optional column (legacy): original_avg_cost. When ``original_cost_total`` is omitted but
    ``original_avg_cost`` is present, the total is derived (avg * shares) and a soft
    ``opening_total_derived`` issue is raised. When BOTH are present and they disagree beyond
    ``max(1 minor unit, 0.5% * total)``, a soft ``opening_cost_mismatch`` issue is raised; the
    authoritative ``original_cost_total`` is stored regardless (never the rounded average).
    """
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))  # tolerate a leading BOM
    rows: list[PreviewRow] = []
    for idx, raw0 in enumerate(reader):
        raw: dict[str, str] = {k.strip(): (v or "").strip() for k, v in raw0.items()}
        issues: list[Issue] = []

        # --- parse required identity/quantity fields + optional legacy avg ---
        try:
            # Legacy Moomoo account id -> moomoo_my (+ soft info issue appended below).
            account_id, alias_issue = alias_import_account(raw["account"])
            symbol = raw["symbol"]
            shares = Decimal(raw["shares"])
            build = date.fromisoformat(raw["build_date"])
            total_raw = raw.get("original_cost_total", "")
            avg_raw = raw.get("original_avg_cost", "")
            avg = Decimal(avg_raw) if avg_raw else None
            total = Decimal(total_raw) if total_raw else None
        except (KeyError, ValueError, InvalidOperation) as exc:
            rows.append(
                PreviewRow(
                    index=idx,
                    raw=raw,
                    issues=[Issue(kind="parse_error", message=str(exc))],
                )
            )
            continue

        if alias_issue is not None:
            issues.append(alias_issue)

        # --- validate account exists (also yields the settlement ccy for the mismatch tol) ---
        acct_row = conn.execute(
            "SELECT settlement_ccy FROM accounts WHERE account_id=?", (account_id,)
        ).fetchone()
        settle_ccy: str | None = acct_row["settlement_ccy"] if acct_row is not None else None
        if acct_row is None:
            issues.append(
                Issue(kind="unknown_account", message=f"unknown account {account_id}")
            )

        # --- resolve the authoritative total (money of record) ---
        if total is not None:
            # both given: cross-check the (rounded) legacy avg against the authoritative total.
            if avg is not None:
                tol = max(_minor_unit(settle_ccy), total.copy_abs() * Decimal("0.005"))
                if (avg * shares - total).copy_abs() > tol:
                    issues.append(
                        Issue(
                            kind="opening_cost_mismatch",
                            needs_confirm=True,
                            message="均價×股數與原始總成本不符，請確認",
                        )
                    )
        elif avg is not None:
            total = avg * shares
            issues.append(
                Issue(
                    kind="opening_total_derived",
                    needs_confirm=True,
                    message="未提供原始總成本，已由均價×股數推導（僅相容舊檔）",
                )
            )
        else:
            rows.append(
                PreviewRow(
                    index=idx,
                    raw=raw,
                    issues=[
                        Issue(
                            kind="parse_error",
                            message="缺少 original_cost_total（或提供 original_avg_cost）",
                        )
                    ],
                )
            )
            continue

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
        original_cost_total=Decimal(p["original_cost_total"]),
        build_date=date.fromisoformat(p["build_date"]),
        commit=commit,
    )
    return row.index
