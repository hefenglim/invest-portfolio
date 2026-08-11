"""CSV import for cash_movements — the 6th ledger kind (入金 / 出金 / 期初資金 / 折讓款).

Deferred at W7 for a stated reason, not forgotten: the withdraw guard needs the pool's
balance and its date-ordered running minimum, both of which live in ``portfolio/cash.py``,
and ``data_ingestion → portfolio`` is not an edge. The prerequisite recorded there —
extract ``validate_cash_movement`` first — is done, and the layering is resolved by
INJECTION (D17's shape) rather than by dropping the guard. The full argument, including the
three rejected alternatives, is the architecture note above
:func:`~data_ingestion.validate.validate_cash_movement`.

**The pool probe is a REQUIRED argument.** It has no default on purpose. The failure this
kind must not have is a registration that forgets to bind the arithmetic and ships a bulk
door with a weaker guard than the single-row form next to it — a silently weaker guard on
the path that writes N rows at once. With no default, forgetting the bind is a ``mypy``
error and a ``TypeError``, never a quiet overdraft. (That is also the answer to D39's
objection to injection: what it rejected was an OPTIONAL registration, which degrades
silently when missed.)

**The whole file is one batch.** A cash CSV is normally "the deposit that funded the
account, then what was spent out of it", so a withdrawal is validated against the stored
ledger PLUS its siblings in the same file — otherwise the first import into a fresh ledger
rejects every withdrawal it contains, which is the E1a class of failure (a feature that
cannot accept the data it exists to accept). The timeline is date-ordered, so file order is
irrelevant, and two withdrawals that only JOINTLY overdraft are both caught.

**``acq_home_amount`` is a column; ``acq_rate`` is not** (spec 2026-07-30, F1). Omitting the
acquisition cost entirely was the tempting shape — it is optional, and the manual form has
its own picker for it — but a bulk import is exactly how a foreign pool gets funded (a
broker's wire history), so leaving it out would make every bulk-imported foreign credit
permanently basis-less. That is not a cash-only blemish: ``covered_ratio`` falls below 1,
and F3 scales the WHOLE foreign exposure — stocks included — by it. The RATE is deliberately
NOT offered here even though the form accepts one: a form converts a typed rate at the seam
and stores the amount, whereas a rate COLUMN puts a rounded average into the file itself, and
a file is re-read, re-edited and re-imported. Store the amount, never the rate.
"""

import csv
import io
import sqlite3
from datetime import date
from decimal import Decimal, InvalidOperation

from portfolio_dash.data_ingestion.preview import ImportPreview, PreviewRow
from portfolio_dash.data_ingestion.store import (
    StoredCashMovement,
    insert_cash_movement,
    list_accounts,
    list_cash_movements,
)
from portfolio_dash.data_ingestion.validate import (
    CashMovementInput,
    CashPoolFn,
    Issue,
    alias_import_account,
    cash_movement_kind,
    resolve_acq_home_amount,
    validate_cash_movement,
)
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.assets import Account

# Canonical CSV column order — SINGLE SOURCE for the downloadable template header (see
# data_ingestion.import_templates). Kept in lockstep with the DictReader keys below by the
# round-trip guard test.
CASH_MOVEMENT_COLUMNS: list[str] = [
    "account", "date", "kind", "ccy", "amount", "acq_home_amount", "note",
]

# The owner reads 入金 / 出金 on a statement; DEPOSIT / WITHDRAW is OUR vocabulary. Both
# spellings are accepted for the same reason ``corporate_action_import`` accepts 分割 —
# the file should not have to be translated before it can be read. An unrecognised label is
# passed through UNCHANGED so ``unknown_movement_kind``'s message can name what was typed.
_KIND_ALIASES: dict[str, str] = {
    "入金": "DEPOSIT",
    "存入": "DEPOSIT",
    "匯入": "DEPOSIT",
    "出金": "WITHDRAW",
    "提領": "WITHDRAW",
    "匯出": "WITHDRAW",
    "期初": "OPENING",
    "期初資金": "OPENING",
    "折讓": "REBATE",
    "折讓款": "REBATE",
    "退款": "REBATE",
}


def _canonical_kind(raw: str) -> str:
    """Map a zh label onto the stored kind; pass anything else through (upper-cased)."""
    return _KIND_ALIASES.get(raw.strip(), cash_movement_kind(raw))


def _decimal(raw: dict[str, str], column: str, label: str) -> Decimal:
    """One required Decimal cell, or a zh ``ValueError`` naming the column."""
    text = raw.get(column, "").strip()
    if not text:
        raise ValueError(f"{label}（{column}）不可空白")
    try:
        return Decimal(text)
    except InvalidOperation:
        raise ValueError(f"{label}（{column}）不是數字：{text}") from None


def _optional_decimal(raw: dict[str, str], column: str, label: str) -> Decimal | None:
    text = raw.get(column, "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        raise ValueError(f"{label}（{column}）不是數字：{text}") from None


def _currency(raw: dict[str, str]) -> Currency:
    """The ``ccy`` cell as a :class:`Currency`, or a zh ``ValueError``.

    ``Currency("XYZ")`` raises an English pydantic/enum message; a hard issue the owner
    reads must name the column and the supported set instead.
    """
    text = raw.get("ccy", "").strip().upper()
    if not text:
        raise ValueError("幣別（ccy）不可空白")
    try:
        return Currency(text)
    except ValueError:
        supported = "／".join(c.value for c in Currency)
        raise ValueError(f"幣別（ccy）無法辨識：{text}（僅支援 {supported}）") from None


def _parse_row(raw: dict[str, str]) -> tuple[CashMovementInput | None, Issue | None]:
    """One CSV row -> a validator input, or a hard ``parse_error`` issue."""
    try:
        account_id, alias_issue = alias_import_account(raw.get("account", "").strip())
        inp = CashMovementInput(
            account_id=account_id,
            date=date.fromisoformat(raw["date"].strip()),
            kind=_canonical_kind(raw.get("kind", "")),
            ccy=_currency(raw),
            amount=_decimal(raw, "amount", "金額"),
            note=(raw.get("note", "").strip() or None),
            # AMOUNT only — never a rate column (see the module docstring).
            acq_home_amount=_optional_decimal(
                raw, "acq_home_amount", "取得成本（家幣金額）"),
        )
    except KeyError as exc:
        return None, Issue(kind="parse_error", message=f"缺少必填欄位 {exc.args[0]}")
    except (ValueError, InvalidOperation) as exc:
        return None, Issue(kind="parse_error", message=str(exc))
    return inp, alias_issue


def _bulk_only_issues(
    inp: CashMovementInput,
    *,
    stored: list[StoredCashMovement],
    accounts: dict[str, Account],
) -> list[Issue]:
    """Two SOFT findings this door adds that the single-row form does not.

    A deliberate asymmetry, and in the safe direction: every HARD rule is the shared
    :func:`validate_cash_movement`, so the CSV path is never the weaker one. These two are
    advisory (``needs_confirm``), and the manual door has no preview seam to show an
    advisory on — returning one there would turn a hint into a rejection, i.e. a behaviour
    change to a path this work deliberately leaves alone.

    * **duplicate_movement** — a cash movement has no natural key, so re-uploading the same
      file books every amount a second time and nothing on any screen says so. This is the
      M7 duplicate-trade guard applied to the failure mode bulk import actually has.
    * **foreign_withdraw_no_fx (N1)** — ``domain-ledger.md``: a foreign WITHDRAW recognises
      NO realized FX; it only reduces the pool's exposure. If the money was really converted
      back to the home currency, the correct entry is an ``fx_conversion`` with both amounts,
      and booking it as a withdrawal silently under-reports 換匯損益. Soft, never coerced —
      a genuine foreign cash withdrawal is legitimate and the importer cannot tell the two
      apart, which is exactly when the answer is to ask rather than to guess.
    """
    issues: list[Issue] = []
    kind = cash_movement_kind(inp.kind)
    if any(
        s.account_id == inp.account_id and s.date == inp.date
        and s.kind == kind and s.ccy == inp.ccy and s.amount == inp.amount
        for s in stored
    ):
        issues.append(Issue(
            kind="duplicate_movement", needs_confirm=True,
            message=("帳本中已有一筆完全相同的資金異動"
                     "（同帳戶、同日期、同類型、同幣別、同金額）。"
                     "重複匯入同一個檔案會把這筆金額計算兩次，"
                     "而資金異動沒有可以辨識重複的鍵值，畫面上不會有任何提示。"
                     "確認這確實是另外一筆嗎？")))
    account = accounts.get(inp.account_id)
    if kind == "WITHDRAW" and account is not None and inp.ccy != account.funding_ccy:
        issues.append(Issue(
            kind="foreign_withdraw_no_fx", needs_confirm=True,
            message=(f"這是一筆 {inp.ccy.value} 外幣出金。外幣出金只會減少外幣部位，"
                     "不會認列已實現匯兌損益。"
                     f"若這筆錢其實是換回 {account.funding_ccy.value}，"
                     "請改用「換匯」登錄（兩側金額都要填），否則換匯損益會少計。"
                     "確認這確實是把外幣提領出去嗎？")))
    return issues


def _payload(inp: CashMovementInput, account: Account | None) -> dict[str, str]:
    """Commit data for :func:`write_cash_movement_row` (all Decimals as strings).

    The acquisition cost is stored RESOLVED — quantized to the funding currency's minor unit
    by the same :func:`resolve_acq_home_amount` the manual door uses — so a foreign credit
    imported from a file and one typed into the form persist the identical figure. An unknown
    account yields no funding currency and therefore no cost; that row is hard-blocked by
    ``unknown_account`` and never reaches the writer.
    """
    payload = {
        "account_id": inp.account_id,
        "date": inp.date.isoformat(),
        "kind": cash_movement_kind(inp.kind),
        "ccy": inp.ccy.value,
        "amount": str(inp.amount),
    }
    if account is not None:
        acq, _issue = resolve_acq_home_amount(inp, funding_ccy=account.funding_ccy)
        if acq is not None:
            payload["acq_home_amount"] = str(acq)
    if inp.note:
        payload["note"] = inp.note
    return payload


def build_cash_movement_preview(
    conn: sqlite3.Connection, csv_text: str, *, pool: CashPoolFn
) -> ImportPreview:
    """Parse *csv_text* into an :class:`ImportPreview` of cash_movements rows.

    Expected columns: account, date, kind, ccy, amount, acq_home_amount (optional), note
    (optional).

    Every hard finding is :func:`validate_cash_movement`'s — the SAME guard the manual door
    runs, including FU-D43a's withdraw block — so the two doors cannot diverge. This file
    adds only the two soft, bulk-specific advisories in :func:`_bulk_only_issues`.

    *pool* is the injected pool arithmetic and has no default; see the module docstring.
    """
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("﻿")))  # tolerate a BOM
    parsed: list[tuple[int, dict[str, str], CashMovementInput | None, list[Issue]]] = []
    for idx, raw0 in enumerate(reader):
        raw: dict[str, str] = {k.strip(): (v or "").strip()
                               for k, v in raw0.items() if k is not None}
        # ``found`` is the parse_error when ``inp`` is None, and the (optional) legacy
        # account-alias notice when it is not — one return, two meanings, per _parse_row.
        inp, found = _parse_row(raw)
        parsed.append((idx, raw, inp, [found] if found is not None else []))

    batch = [inp for _idx, _raw, inp, _issues in parsed if inp is not None]
    # Hoisted ONCE for the whole file (trap #21): both are whole-table reads that
    # ``validate_cash_movement`` and the duplicate check would otherwise repeat per row.
    accounts = {a.account_id: a for a in list_accounts(conn)}
    stored = list_cash_movements(conn)

    rows: list[PreviewRow] = []
    for idx, raw, inp, issues in parsed:
        if inp is None:
            rows.append(PreviewRow(index=idx, raw=raw, issues=issues))
            continue
        all_issues: list[Issue] = [
            *issues,
            *validate_cash_movement(
                conn, inp, pool=pool, batch=batch, accounts=accounts),
            *_bulk_only_issues(inp, stored=stored, accounts=accounts),
        ]
        rows.append(PreviewRow(
            index=idx, raw=raw,
            payload=_payload(inp, accounts.get(inp.account_id)), issues=all_issues))
    return ImportPreview(rows=rows)


def write_cash_movement_row(
    conn: sqlite3.Connection, row: PreviewRow, *, commit: bool = True
) -> int:
    """Persist one accepted cash_movements row and return its autoincrement id.

    ``commit`` is forwarded to the store insert; the batch path passes ``commit=False`` so
    the whole batch commits once (all-or-nothing, #1).
    """
    p = row.payload
    return insert_cash_movement(
        conn,
        account_id=p["account_id"],
        move_date=date.fromisoformat(p["date"]),
        kind=p["kind"],
        ccy=Currency(p["ccy"]),
        amount=Decimal(p["amount"]),
        note=p.get("note"),
        acq_home_amount=(Decimal(p["acq_home_amount"])
                         if "acq_home_amount" in p else None),
        commit=commit,
    )
