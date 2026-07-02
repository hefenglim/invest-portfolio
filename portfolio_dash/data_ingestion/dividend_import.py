"""CSV import for dividends — reuses the preview/commit infrastructure."""

import csv
import io
import sqlite3
from datetime import date
from decimal import Decimal, InvalidOperation

from portfolio_dash.data_ingestion.dividend_model import apply_dividend_model
from portfolio_dash.data_ingestion.preview import ImportPreview, PreviewRow
from portfolio_dash.data_ingestion.resolve import ResolutionStatus, resolve
from portfolio_dash.data_ingestion.store import insert_dividend
from portfolio_dash.data_ingestion.validate import Issue


def _opt_decimal(row: dict[str, str], key: str) -> Decimal | None:
    """Return a Decimal parsed from *row[key]*, or None when missing/empty/invalid."""
    val = row.get(key, "").strip()
    if not val:
        return None
    try:
        return Decimal(val)
    except InvalidOperation:
        return None


def build_dividend_preview(conn: sqlite3.Connection, csv_text: str) -> ImportPreview:
    """Parse *csv_text* into an :class:`ImportPreview` of dividends rows.

    Required columns: account, symbol, date, type, gross.
    Optional columns: withholding, net, reinvest_shares, reinvest_price.

    The dividend model for the row is derived from the ``type`` column
    (``DRIP`` / ``STOCK`` / ``cash``); ``apply_dividend_model`` fills in computed
    amounts which are stored in ``payload`` for later commit.
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
            div_date = date.fromisoformat(raw["date"])
            # Normalize + validate type (2026-07-03): the raw value used to be
            # stored as-is, so a lowercase "cash" poisoned the ledger (readers do
            # DividendType(s.type) and raise). Same write/read invariant as ever:
            # never store what the read path cannot represent.
            div_type = raw["type"].strip().upper()
            if div_type not in {"CASH", "STOCK", "DRIP", "NET"}:
                raise ValueError(f"unknown dividend type {raw['type']!r}")
            gross = Decimal(raw["gross"])
        except (KeyError, ValueError, InvalidOperation) as exc:
            rows.append(
                PreviewRow(
                    index=idx,
                    raw=raw,
                    issues=[Issue(kind="parse_error", message=str(exc))],
                )
            )
            continue

        # --- parse optional numeric overrides ---
        withholding_override = _opt_decimal(raw, "withholding")
        net_override = _opt_decimal(raw, "net")
        reinvest_shares_override = _opt_decimal(raw, "reinvest_shares")
        reinvest_price_override = _opt_decimal(raw, "reinvest_price")

        # --- validate account exists (hard issue) ---
        if (
            conn.execute(
                "SELECT 1 FROM accounts WHERE account_id=?", (account_id,)
            ).fetchone()
            is None
        ):
            issues.append(
                Issue(kind="unknown_account", message=f"unknown account {account_id!r}")
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

        # --- apply dividend model to compute withholding / net / reinvest_shares ---
        amounts = apply_dividend_model(
            div_type,
            gross=gross,
            withholding=withholding_override,
            net=net_override,
            reinvest_shares=reinvest_shares_override,
            reinvest_price=reinvest_price_override,
        )

        payload: dict[str, str] = {
            "account_id": account_id,
            "symbol": symbol,
            "date": div_date.isoformat(),
            "type": div_type,
            "gross": str(amounts.gross),
            "withholding": str(amounts.withholding),
            "net": str(amounts.net),
        }
        if amounts.reinvest_shares is not None:
            payload["reinvest_shares"] = str(amounts.reinvest_shares)
        if amounts.reinvest_price is not None:
            payload["reinvest_price"] = str(amounts.reinvest_price)

        rows.append(PreviewRow(index=idx, raw=raw, payload=payload, issues=issues))

    return ImportPreview(rows=rows)


def write_dividend_row(
    conn: sqlite3.Connection, row: PreviewRow, *, commit: bool = True
) -> int:
    """Persist one accepted dividends row and return its autoincrement id.

    ``commit`` is forwarded to the store insert; the batch path passes ``commit=False``
    so the whole batch commits once (all-or-nothing, #1).
    """
    p = row.payload
    rs_str = p.get("reinvest_shares")
    rp_str = p.get("reinvest_price")
    return insert_dividend(
        conn,
        account_id=p["account_id"],
        symbol=p["symbol"],
        div_date=date.fromisoformat(p["date"]),
        div_type=p["type"],
        gross=Decimal(p["gross"]),
        withholding=Decimal(p["withholding"]),
        net=Decimal(p["net"]),
        reinvest_shares=Decimal(rs_str) if rs_str is not None else None,
        reinvest_price=Decimal(rp_str) if rp_str is not None else None,
        commit=commit,
    )
