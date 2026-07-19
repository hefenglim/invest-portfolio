"""Transaction CSV importer: parse → validate → fee-compute → preview/commit.

Also hosts the SHARED import-seam helpers (FU-D19) used by every kind's importer via the
router: :func:`canonical_header` (strip a template annotation from a column name) and
:func:`normalize_import_csv` (canonicalize headers + resolve the date column to ISO, refusing
to guess an ambiguous M/D-vs-D/M column).  The per-kind builders stay ISO-only; the router
normalizes first, so annotated templates and Excel-reformatted dates parse through every kind.
"""

import csv
import io
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from portfolio_dash.data_ingestion.config_seed import get_fee_rule_set
from portfolio_dash.data_ingestion.dateparse import DateCandidate, resolve_date_column
from portfolio_dash.data_ingestion.fees import FeeComputationError, compute_fees
from portfolio_dash.data_ingestion.fx_lookup import resolve_stamp_fx
from portfolio_dash.data_ingestion.preview import ImportPreview, PreviewRow
from portfolio_dash.data_ingestion.resolve import (
    ResolutionStatus,
    resolve,
    suggestion_tail,
)
from portfolio_dash.data_ingestion.store import insert_transaction
from portfolio_dash.data_ingestion.validate import Issue, TxnInput, validate_transaction
from portfolio_dash.shared.models.enums import Side

# Canonical CSV column order for the transactions import — the SINGLE SOURCE the downloadable
# template header is built from (see data_ingestion.import_templates). These names MUST match
# the keys the DictReader in build_transaction_preview reads below (required: account, symbol,
# side, date, shares, price; optional: fee, tax, daytrade, note). The round-trip guard test
# re-parses the generated template to prove header ↔ parser stay in lockstep.
TRANSACTION_COLUMNS: list[str] = [
    "account", "symbol", "side", "date", "shares", "price",
    "fee", "tax", "daytrade", "note",
]

# A column-name annotation from the downloadable template — half- or full-width parentheses,
# e.g. ``date(YYYY-MM-DD)`` / ``fee（選填）``. Stripped so annotated templates parse like plain.
_ANNOTATION_RE = re.compile(r"[(（][^)）]*[)）]")


def canonical_header(name: str) -> str:
    """Canonical column key: drop any parenthetical annotation (half/full-width) + surrounding
    whitespace, then lowercase.  ``date(YYYY-MM-DD)`` -> ``date``, ``fee（選填）`` -> ``fee``, a
    leading BOM + ``Account`` -> ``account``; a plain header is returned unchanged (byte-clean
    templates stay byte-identical).  Applied at the import seam so annotated templates and
    hand-typed casing both match the parsers' canonical column names."""
    return _ANNOTATION_RE.sub("", name).lstrip("\ufeff").strip().lower()


@dataclass(frozen=True)
class DateAmbiguity:
    """A column-level date ambiguity the user must resolve before any write (FU-D19)."""

    column: str
    samples: list[str]
    candidates: list[DateCandidate]


@dataclass(frozen=True)
class NormalizedImport:
    """Result of :func:`normalize_import_csv`: rewritten CSV text + any date ambiguity."""

    text: str
    ambiguity: DateAmbiguity | None


def normalize_import_csv(
    csv_text: str, date_col: str, *, date_format: str | None = None
) -> NormalizedImport:
    """Rewrite *csv_text* to canonical headers + ISO dates for the per-kind builder.

    Headers are canonicalized (annotation + case stripped) so annotated templates parse; the
    *date_col* is inferred at COLUMN level (:func:`dateparse.resolve_date_column`) and each cell
    rewritten to ISO.  A genuine M/D-vs-D/M ambiguity is NOT guessed: ``ambiguity`` is returned
    and the date cells are left as-is so the ISO-only builder errors each row until the caller
    pins *date_format*.  A cell that does not parse under the resolved format is likewise left
    as-is, so the builder reports the offending value per row (unchanged behaviour).

    The AI path and the single-row forms already emit ISO -> the fast path leaves them intact.
    """
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))
    fieldnames = reader.fieldnames
    if not fieldnames:
        return NormalizedImport(text=csv_text, ambiguity=None)  # header-only / empty: nothing to do
    canon = [canonical_header(f) for f in fieldnames]
    rows: list[dict[str, str]] = [
        {canonical_header(k): (v or "").strip() for k, v in row.items() if k is not None}
        for row in reader
    ]

    ambiguity: DateAmbiguity | None = None
    if date_col in canon:
        result = resolve_date_column([r.get(date_col, "") for r in rows], pinned=date_format)
        if result.ambiguous:
            ambiguity = DateAmbiguity(
                column=date_col, samples=result.samples, candidates=result.candidates)
        else:
            for r, d in zip(rows, result.dates, strict=True):
                if d is not None:
                    r[date_col] = d.isoformat()

    buf = io.StringIO()
    writer = csv.DictWriter(
        buf, fieldnames=canon, lineterminator="\r\n", extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return NormalizedImport(text=buf.getvalue(), ambiguity=ambiguity)


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

    # --- symbol resolution: write the RESOLVED symbol ---
    # EXACT -> rewrite the payload symbol to the registered symbol. NEEDS_AI (every
    # non-exact outcome, R6-A) -> HARD issue (unregistered symbol: no quote ccy, not in
    # the pricing worklist — the row would be uninterpretable; register first). The raw
    # symbol is kept on the payload and non-binding name suggestions (if any) are
    # appended to the message — the resolver never coerces a code to a near neighbour.
    res = resolve(conn, inp.symbol)
    symbol = inp.symbol
    if res.status is ResolutionStatus.NEEDS_AI:
        message = f"未註冊標的 {inp.symbol} — 請先至「標的管理」註冊"
        message += suggestion_tail(res.candidates)
        issues.append(
            Issue(
                kind="symbol_unresolved",
                needs_confirm=False,
                message=message,
            )
        )
    elif res.instrument is not None:  # EXACT
        symbol = res.instrument.symbol

    # --- fee / tax auto-fill (only when account exists and values are missing) ---
    fee: Decimal | None = inp.fee
    tax: Decimal | None = inp.tax
    snap: dict[str, str] = {}

    acc = conn.execute(
        "SELECT fee_rule_set FROM accounts WHERE account_id=?",
        (inp.account_id,),
    ).fetchone()
    if acc is not None and (fee is None or tax is None):
        # Registry-authoritative ETF flag (same rule as manual.py — stress-audit
        # finding 2026-07-15): a registered instrument's is_etf wins; the input
        # flag only covers unregistered symbols (e.g. an AI draft pre-registration).
        is_etf = res.instrument.is_etf if res.instrument is not None else inp.is_etf
        rules = get_fee_rule_set(acc["fee_rule_set"], conn)
        # FE-D2: Moomoo US MY stamp needs the trade-date USD/MYR rate (fees.py is pure).
        stamp_fx: Decimal | None = None
        if rules.has_us_stamp:
            stamp_fx = resolve_stamp_fx(conn, inp.trade_date)
            if stamp_fx is None:
                issues.append(Issue(
                    kind="stamp_fx_missing", needs_confirm=True,
                    message="無 USD/MYR 匯率,印花稅未計"))
        try:
            fr = compute_fees(
                rules,
                inp.side,
                inp.quantity,
                inp.price,
                is_etf=is_etf,
                daytrade=inp.daytrade,
                stamp_fx=stamp_fx,
            )
        except FeeComputationError as exc:
            # Overflow-sized input (M4): a hard row issue, never a 500.
            issues.append(Issue(kind="fee_overflow", message=str(exc)))
        else:
            if fee is None:
                fee = fr.fee
            if tax is None:
                tax = fr.tax
            snap = fr.snapshot

    # Build payload for the writer (string dict + prefixed snapshot entries)
    payload: dict[str, str] = {
        "account_id": inp.account_id,
        "symbol": symbol,
        "side": inp.side.value,
        "quantity": str(inp.quantity),
        "price": str(inp.price),
        "trade_date": inp.trade_date.isoformat(),
        "daytrade": "1" if inp.daytrade else "0",  # persisted through the writer (MED-1)
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
                  ``price``.  Optional: ``fee``, ``tax``, ``note``, ``daytrade``
                  (``1``/``true`` marks a TW same-day round trip → 0.15% sell tax).

    Returns:
        :class:`ImportPreview` containing one :class:`PreviewRow` per data row.
    """
    # lstrip a leading UTF-8 BOM: the downloadable template ships WITH a BOM (Excel), so a
    # download->re-upload (or paste) round-trip must not turn the first header into a BOM+account.
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))
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
                daytrade=raw.get("daytrade", "").lower() in ("1", "true", "y", "yes"),
                note=raw.get("note") or None,
            )
        except (KeyError, ValueError, InvalidOperation) as exc:
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


def write_transaction_row(
    conn: sqlite3.Connection, row: PreviewRow, *, commit: bool = True
) -> int:
    """Insert one transaction from a committed :class:`PreviewRow`.

    Extracts the ``snap.*`` keys from :attr:`PreviewRow.payload` to reconstruct
    the fee-rule snapshot, then delegates to :func:`~store.insert_transaction`.

    ``commit`` is forwarded to the store insert; the batch path (:func:`commit_preview`)
    passes ``commit=False`` so the whole batch commits once (all-or-nothing, #1).

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
        daytrade=p.get("daytrade") == "1",
        commit=commit,
    )
