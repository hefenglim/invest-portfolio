"""CSV import for opening_inventory rows — reuses the preview/commit infrastructure."""

import csv
import io
import sqlite3
from datetime import date
from decimal import Decimal, InvalidOperation

from portfolio_dash.data_ingestion.csv_import import unread_columns_issues
from portfolio_dash.data_ingestion.preview import ImportPreview, PreviewRow
from portfolio_dash.data_ingestion.resolve import ResolutionStatus, resolve
from portfolio_dash.data_ingestion.store import upsert_opening
from portfolio_dash.data_ingestion.validate import (
    Issue,
    alias_import_account,
    unknown_account_issue,
    validate_opening_cost,
)
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


class _CellError(ValueError):
    """A vetted zh sentence naming the column that could not be read (I-5).

    The parse arm used to answer ``Issue(message=str(exc))`` for ANY ``KeyError`` /
    ``ValueError`` / ``InvalidOperation``, so the owner's 原因 column read 「'account'」,
    「Invalid isoformat string: '2026/01/02'」 or 「[<class 'decimal.ConversionSyntax'>]」. Every
    cell is now read through a typed reader that raises THIS with the dividend / cash / fx
    doors' wording, and only this class is forwarded verbatim; anything else gets a fixed
    sentence — never ``str(exc)``.
    """


def _decimal_cell(raw: dict[str, str], column: str, label: str) -> Decimal:
    """One required, finite Decimal cell. Subscript read: an absent HEADER is the
    缺少必填欄位 arm's business, a blank CELL is this sentence."""
    text = raw[column].strip()
    if not text:
        raise _CellError(f"{label}（{column}）不可空白")
    return _finite_decimal(text, column, label)


def _optional_decimal_cell(raw: dict[str, str], column: str, label: str) -> Decimal | None:
    text = raw.get(column, "").strip()
    return _finite_decimal(text, column, label) if text else None


def _finite_decimal(text: str, column: str, label: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise _CellError(f"{label}（{column}）不是數字：{text}") from None
    if not value.is_finite():
        # ``Decimal("NaN")`` CONSTRUCTS; the ``shares <= 0`` check below would then raise
        # ``InvalidOperation`` outside any arm — a 500 for one broken cell.
        raise _CellError(f"{label}（{column}）必須是有限數字，目前是「{text}」")
    return value


def _date_cell(raw: dict[str, str], column: str, label: str) -> date:
    text = raw[column].strip()
    if not text:
        raise _CellError(f"{label}（{column}）不可空白")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise _CellError(
            f"{label}（{column}）格式不正確，須為 YYYY-MM-DD，目前是「{text}」") from None


def build_opening_preview(conn: sqlite3.Connection, csv_text: str) -> ImportPreview:
    """Parse *csv_text* into an :class:`ImportPreview` of opening_inventory rows.

    Required columns: account, symbol, shares, original_cost_total, build_date.
    Optional column (legacy): original_avg_cost. When ``original_cost_total`` is omitted but
    ``original_avg_cost`` is present, the total is derived (avg * shares) and a soft
    ``opening_total_derived`` issue is raised. When BOTH are present and they disagree beyond
    ``max(1 minor unit, 0.5% * total)``, a soft ``opening_cost_mismatch`` issue is raised; the
    authoritative ``original_cost_total`` is stored regardless (never the rounded average).

    F-13 (D37): the RESOLVED total must be **> 0**, a HARD ``non_positive_opening_cost``
    issue — see :func:`~portfolio_dash.data_ingestion.validate.validate_opening_cost`. This
    is the door the single-row 期初 form uses too (``web/input.js`` posts a one-row CSV), so
    the manual path is covered by the same check.
    """
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))  # tolerate a leading BOM
    rows: list[PreviewRow] = []
    # I-4 (DEF-026's seam, every kind): the columns this door will not read are NAMED on
    # each row as an advisory (「已忽略欄位：…」), never dropped in silence.
    ignored = unread_columns_issues(
        [(h or "").strip() for h in (reader.fieldnames or [])], OPENING_COLUMNS)
    for idx, raw0 in enumerate(reader):
        raw: dict[str, str] = {k.strip(): (v or "").strip() for k, v in raw0.items()}
        issues: list[Issue] = []

        # --- parse required identity/quantity fields + optional legacy avg ---
        try:
            # Legacy Moomoo account id -> moomoo_my (+ soft info issue appended below).
            account_id, alias_issue = alias_import_account(raw["account"])
            symbol = raw["symbol"]
            shares = _decimal_cell(raw, "shares", "股數")
            build = _date_cell(raw, "build_date", "建倉日期")
            total = _optional_decimal_cell(raw, "original_cost_total", "原始總成本")
            avg = _optional_decimal_cell(raw, "original_avg_cost", "原始均價")
        except KeyError as exc:
            rows.append(PreviewRow(index=idx, raw=raw, issues=[
                Issue(kind="parse_error", message=f"缺少必填欄位 {exc.args[0]}")]))
            continue
        except _CellError as exc:
            # BEFORE the belt-and-braces arm, because ``_CellError`` IS a ``ValueError``.
            rows.append(PreviewRow(index=idx, raw=raw, issues=[
                Issue(kind="parse_error", message=str(exc))]))
            continue
        except (ValueError, InvalidOperation):
            # Unreachable by any cell the readers above accept; exists so the NEXT cell added
            # here cannot re-open the leak. Never ``str(exc)`` — that is the leak.
            rows.append(PreviewRow(index=idx, raw=raw, issues=[
                Issue(kind="parse_error",
                      message="這一列的內容無法解析，請對照範本檢查各欄位格式")]))
            continue

        if alias_issue is not None:
            issues.append(alias_issue)

        # --- validate account exists (also yields the settlement ccy for the mismatch tol) ---
        acct_row = conn.execute(
            "SELECT settlement_ccy FROM accounts WHERE account_id=?", (account_id,)
        ).fetchone()
        settle_ccy: str | None = acct_row["settlement_ccy"] if acct_row is not None else None
        if acct_row is None:
            # L-1: the shared sentence. A BLANK ``account`` cell rendered 「帳戶  不存在」
            # here — two spaces, no name — while the cash door already said 「帳戶不可空白」.
            issues.append(unknown_account_issue(account_id))

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
                Issue(kind="non_positive_shares", message="股數必須大於 0")
            )

        # --- F-13 (D37): the RESOLVED total must be positive — HARD ---
        # Placed after the resolution above, not next to the parse, because the legacy
        # derivation reaches a zero total without ever naming one: `original_avg_cost = 0`
        # yields `avg * shares == 0` and would otherwise import carrying nothing but the SOFT
        # `opening_total_derived` notice. One check, both paths.
        if (bad_cost := validate_opening_cost(total)) is not None:
            issues.append(bad_cost)

        # --- warn if symbol cannot be resolved (soft — needs confirm) ---
        if resolve(conn, symbol).status is ResolutionStatus.NEEDS_AI:
            issues.append(
                Issue(
                    kind="symbol_unresolved",
                    needs_confirm=True,
                    message=f"未註冊標的 {symbol} — 請先至「標的管理」註冊",
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

    for row in rows:
        row.issues.extend(ignored)
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
