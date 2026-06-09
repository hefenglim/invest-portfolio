"""Transaction CSV importer: parse → validate → fee-compute → preview/commit."""

import csv
import io
import sqlite3
from datetime import date
from decimal import Decimal

from portfolio_dash.data_ingestion.config_seed import get_fee_rule_set
from portfolio_dash.data_ingestion.fees import compute_fees
from portfolio_dash.data_ingestion.preview import ImportPreview, PreviewRow
from portfolio_dash.data_ingestion.resolve import ResolutionStatus, resolve
from portfolio_dash.data_ingestion.store import insert_transaction
from portfolio_dash.data_ingestion.validate import Issue, TxnInput, validate_transaction
from portfolio_dash.shared.models.enums import Side


def txn_preview_row(
    conn: sqlite3.Connection,
    index: int,
    raw: dict[str, str],
    inp: TxnInput,
) -> PreviewRow:
    """Build a :class:`PreviewRow` for a single transaction input.

    Runs validation, symbol resolution, and fee/tax auto-fill from the account's
    FeeRuleSet.  Reusable by both the CSV importer and the AI agents input path.

    Args:
        conn:  Active SQLite connection (schema in place, accounts seeded).
        index: Row index (0-based) used to identify the row in the preview.
        raw:   Original raw key/value mapping for display purposes.
        inp:   Parsed and typed transaction input.

    Returns:
        A fully populated :class:`PreviewRow`.
    """
    issues: list[Issue] = list(validate_transaction(conn, inp))

    # --- symbol resolution: flag unresolved symbols as soft issues ---
    if resolve(conn, inp.symbol).status is ResolutionStatus.NEEDS_AI:
        issues.append(
            Issue(
                kind="symbol_unresolved",
                needs_confirm=True,
                message=f"unresolved {inp.symbol}",
            )
        )

    # --- fee / tax auto-fill (only when account exists and values are missing) ---
    fee: Decimal | None = inp.fee
    tax: Decimal | None = inp.tax
    snap: dict[str, str] = {}

    acc = conn.execute(
        "SELECT fee_rule_set FROM accounts WHERE account_id=?",
        (inp.account_id,),
    ).fetchone()
    if acc is not None and (fee is None or tax is None):
        fr = compute_fees(
            get_fee_rule_set(acc["fee_rule_set"]),
            inp.side,
            inp.quantity,
            inp.price,
            is_etf=inp.is_etf,
            daytrade=inp.daytrade,
        )
        if fee is None:
            fee = fr.fee
        if tax is None:
            tax = fr.tax
        snap = fr.snapshot

    # Build payload for the writer (string dict + prefixed snapshot entries)
    payload: dict[str, str] = {
        "account_id": inp.account_id,
        "symbol": inp.symbol,
        "side": inp.side.value,
        "quantity": str(inp.quantity),
        "price": str(inp.price),
        "trade_date": inp.trade_date.isoformat(),
        "note": inp.note or "",
        **{f"snap.{k}": v for k, v in snap.items()},
    }

    return PreviewRow(
        index=index,
        raw=raw,
        payload=payload,
        fee=fee,
        tax=tax,
        issues=issues,
    )


def build_transaction_preview(conn: sqlite3.Connection, csv_text: str) -> ImportPreview:
    """Parse *csv_text* into an :class:`ImportPreview` of transaction rows.

    Each row is validated, symbol-resolved, and auto-filled with fee/tax from
    the account's FeeRuleSet (unless the CSV already supplies those columns).
    Rows that fail to parse are captured with a ``parse_error`` issue.

    Args:
        conn:     Active SQLite connection (schema in place, accounts seeded).
        csv_text: Full CSV text including a header row.  Required columns:
                  ``account``, ``symbol``, ``side``, ``date``, ``shares``,
                  ``price``.  Optional: ``fee``, ``tax``, ``note``.

    Returns:
        :class:`ImportPreview` containing one :class:`PreviewRow` per data row.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    rows: list[PreviewRow] = []

    for idx, raw_row in enumerate(reader):
        raw = {k.strip(): (v or "").strip() for k, v in raw_row.items()}

        # --- parse: build TxnInput from CSV columns ---
        try:
            inp = TxnInput(
                account_id=raw["account"],
                symbol=raw["symbol"],
                side=Side(raw["side"].upper()),
                quantity=Decimal(raw["shares"]),
                price=Decimal(raw["price"]),
                trade_date=date.fromisoformat(raw["date"]),
                fee=Decimal(raw["fee"]) if raw.get("fee") else None,
                tax=Decimal(raw["tax"]) if raw.get("tax") else None,
                note=raw.get("note") or None,
            )
        except (KeyError, ValueError) as exc:
            rows.append(
                PreviewRow(
                    index=idx,
                    raw=raw,
                    issues=[Issue(kind="parse_error", message=str(exc))],
                )
            )
            continue

        rows.append(txn_preview_row(conn, idx, raw, inp))

    return ImportPreview(rows=rows)


def write_transaction_row(conn: sqlite3.Connection, row: PreviewRow) -> int:
    """Insert one transaction from a committed :class:`PreviewRow`.

    Extracts the ``snap.*`` keys from :attr:`PreviewRow.payload` to reconstruct
    the fee-rule snapshot, then delegates to :func:`~store.insert_transaction`.

    Returns:
        The new transaction's primary-key id.
    """
    p = row.payload
    snapshot = {k[5:]: v for k, v in p.items() if k.startswith("snap.")}
    return insert_transaction(
        conn,
        account_id=p["account_id"],
        symbol=p["symbol"],
        side=Side(p["side"]),
        quantity=Decimal(p["quantity"]),
        price=Decimal(p["price"]),
        fees=row.fee if row.fee is not None else Decimal("0"),
        tax=row.tax if row.tax is not None else Decimal("0"),
        trade_date=date.fromisoformat(p["trade_date"]),
        fee_rule_snapshot=snapshot,
        note=p["note"] or None,
    )
