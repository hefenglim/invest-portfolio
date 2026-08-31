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

**The whole file is one batch — but only the part of it that will be WRITTEN** (QA-01,
2026-08-29). A cash CSV is normally "the deposit that funded the account, then what was spent
out of it", so a withdrawal is validated against the stored ledger PLUS its siblings in the
same file — otherwise the first import into a fresh ledger rejects every withdrawal it
contains, which is the E1a class of failure (a feature that cannot accept the data it exists
to accept). The timeline is date-ordered, so file order is irrelevant, and two withdrawals
that only JOINTLY overdraft are both caught.

⚠ The batch was originally every row that PARSED, which is a different set from every row
that will be COMMITTED, and the difference funded overdrafts: a deposit this preview REJECTS
(hard issue) and a deposit the caller DESELECTED on the commit are both rows no ledger will
ever hold, yet each covered a withdrawal that was then written alone. Measured: a two-row file
(DEPOSIT 100,000 TWD + WITHDRAW 60,000 TWD) committed with ``select=[1]`` wrote the withdrawal
and left the pool at −60,000, while the manual door answers 422 for the identical movement —
the exact asymmetry ``architecture.md``'s C3 seam exists to prevent. Both halves are closed
here: :func:`_pool_free_issues` drops the structurally-invalid rows, and *select* drops the
deselected ones (bound by ``api/routers/input_center.py`` after it intersects the caller's
ticks).

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
from collections.abc import Collection, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError

from portfolio_dash.data_ingestion.preview import ImportPreview, PreviewRow
from portfolio_dash.data_ingestion.store import (
    StoredCashMovement,
    insert_cash_movement,
    list_accounts,
    list_cash_movements,
)
from portfolio_dash.data_ingestion.validate import (
    CashMovementInput,
    CashPool,
    CashPoolFn,
    Issue,
    alias_import_account,
    cash_movement_kind,
    resolve_acq_home_amount,
    validate_cash_movement,
)
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.assets import Account

_ZERO = Decimal("0")
#: A balance no cash row can plausibly exceed. Used ONLY by :func:`_inert_probe`.
_UNBOUNDED = Decimal(10) ** 24

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
    # The broker-statement kinds (2026-08-13). A US export states these in English, but the
    # owner reads a statement in Chinese and hand-edits the template in Excel, so both
    # spellings are accepted for the same reason 入金 / 出金 are.
    "利息": "INTEREST",
    "利息收入": "INTEREST",
    "融資利息": "INTEREST_EXPENSE",
    "利息支出": "INTEREST_EXPENSE",
    "券商費用": "BROKER_FEE",
    "帳戶費用": "BROKER_FEE",
}


def _canonical_kind(raw: str) -> str:
    """Map a zh label onto the stored kind; pass anything else through (upper-cased)."""
    return _KIND_ALIASES.get(raw.strip(), cash_movement_kind(raw))


def _finite(value: Decimal, column: str, label: str, text: str) -> Decimal:
    """*value*, or a zh ``ValueError`` when the cell holds ``NaN`` / ``±Infinity`` / ``sNaN``.

    ``Decimal("NaN")`` CONSTRUCTS — ``InvalidOperation`` is never raised — so the two callers'
    ``except`` arms below saw nothing wrong and handed a non-finite amount to
    :class:`CashMovementInput`. Pydantic rejects it (``finite_number``) with a
    ``ValidationError``, which is a ``ValueError`` subclass, so ``_parse_row``'s
    ``str(exc)`` put pydantic's own English report into the owner's import-preview 原因
    column: ``"1 validation error for CashMovementInput / amount / Input should be a finite
    number …"``. The degradation was already right (one row rejected, siblings kept); only
    the wording was wrong, and the wording is that column's entire content.

    Same wording as the fx door's identical guard (``fx_import._parse_row``, QA-07) — one
    broken numeric cell says one thing whichever bulk door it arrived through — and the RAW
    cell is echoed rather than a re-formatted Decimal, because it is what the owner has to go
    and fix. ``is_finite()`` covers all four spellings in one predicate and cannot itself
    raise.
    """
    if not value.is_finite():
        raise ValueError(f"{label}（{column}）必須是有限數字，目前是「{text}」")
    return value


def _decimal(raw: dict[str, str], column: str, label: str) -> Decimal:
    """One required Decimal cell, or a zh ``ValueError`` naming the column.

    Read by SUBSCRIPT on purpose (J-2) — the argument :func:`_date_cell` makes below, and the
    one :func:`fx_import._cell` applies to every required cell of the other bulk door: an
    absent HEADER must raise ``KeyError`` into :func:`_parse_row`'s 缺少必填欄位 arm, because
    "you forgot the column" and "row 4's cell is empty" are different mistakes with different
    fixes. ``.get(column, "")`` collapsed the two, so a file with no ``amount`` column at all
    was answered 「金額（amount）不可空白」 — sending the owner to look for a blank cell in a
    column that does not exist. Until then ``date`` was the only required cell of this door
    that made the distinction.

    A present-but-blank cell keeps its own sentence: ``build_cash_movement_preview`` has
    already stripped every cell, so a SHORT row (``DictReader`` fills the tail with ``None``)
    still arrives here as ``""``.
    """
    text = raw[column].strip()
    if not text:
        raise ValueError(f"{label}（{column}）不可空白")
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"{label}（{column}）不是數字：{text}") from None
    return _finite(value, column, label, text)


def _optional_decimal(raw: dict[str, str], column: str, label: str) -> Decimal | None:
    """One OPTIONAL Decimal cell — ``None`` when the column, or the cell, is absent.

    ``.get()`` here, deliberately NOT :func:`_decimal`'s subscript (J-2). ``acq_home_amount``
    is optional by spec (F1: a cost that is not known is left blank, never guessed), so a file
    without the column is the ordinary shape of a hand-made cash file rather than a mistake,
    and raising ``KeyError`` on it would reject every one of them. The two readers diverge
    because the two columns do.
    """
    text = raw.get(column, "").strip()
    if not text:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"{label}（{column}）不是數字：{text}") from None
    return _finite(value, column, label, text)


def _date_cell(raw: dict[str, str], column: str, label: str) -> date:
    """One required ISO date cell, or a zh ``ValueError`` naming the column (G-1).

    ``date.fromisoformat`` raises a ``ValueError`` whose text is CPython's own English, and
    ``_parse_row``'s ``except … str(exc)`` arm rendered it verbatim in the import preview's
    原因 column — the one column whose entire job is to tell the owner why a row was
    rejected::

        2026-13-01  ->  month must be in 1..12
        2026/07/01  ->  Invalid isoformat string: '2026/07/01'   (the shape Excel writes)
        (blank)     ->  Invalid isoformat string: ''

    The first does not even mention a date, and none of them names the column, so on a
    多-row file the owner was told a month was wrong somewhere and left to find it.

    The column is read by SUBSCRIPT on purpose: an absent header must keep raising
    ``KeyError`` into the 缺少必填欄位 arm below, because "you forgot the column" and "this
    cell is blank" are different mistakes with different fixes. Reading it with ``.get()``
    would collapse the two. :func:`build_cash_movement_preview` has already stripped every
    cell, so a short CSV row (``DictReader`` fills the tail with ``None``) arrives as ``""``.

    Word for word the fx door's helper (:func:`fx_import._date_cell`) — one broken date cell
    says one thing whichever bulk door it arrived through, asserted byte-identical by
    ``tests/data_ingestion/test_m2_import_date_zh.py``. The transaction door has said this in
    Chinese since QA-23, but through its own ``_CellError`` machinery
    (``csv_import._date_cell`` / ``_NOT_A_DATE``), whose exception type this module would
    have to import and catch as well; a four-line reader was the smaller seam.
    """
    text = raw[column]
    if not text:
        raise ValueError(f"{label}（{column}）不可空白")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValueError(
            f"{label}（{column}）格式不正確，須為 YYYY-MM-DD，目前是「{text}」") from None


def _currency(raw: dict[str, str]) -> Currency:
    """The ``ccy`` cell as a :class:`Currency`, or a zh ``ValueError``.

    ``Currency("XYZ")`` raises an English pydantic/enum message; a hard issue the owner
    reads must name the column and the supported set instead.

    Upper-cased for the LOOKUP only; the message echoes the RAW cell (J-3). Both halves are
    load-bearing. A file saying ``usd`` is unambiguous and stays ACCEPTED — the fx door was
    widened to match this one in H-2, so two bulk doors cannot disagree about the same cell —
    while a file saying ``gbp`` used to be answered 「幣別（ccy）無法辨識：GBP」, quoting a code
    the owner never typed back at them, in the one column whose whole job is to say which cell
    to go and fix. Same rule as the non-finite and date guards above: echo what is in the file,
    not what this module made of it.

    Read by SUBSCRIPT for :func:`_decimal`'s reason, applied to the last required cell that
    still had ``.get()`` (K-1). ``ccy`` is required, so an absent HEADER must raise
    ``KeyError`` into :func:`_parse_row`'s 缺少必填欄位 arm rather than reporting the blank
    cell it is not: 「幣別（ccy）不可空白」 on a file with no ``ccy`` column sends the owner to
    look for an empty cell that cannot exist. The fx door has answered
    ``缺少必填欄位 from_ccy`` for the equivalent file since H-2. A present-but-blank cell keeps
    its own sentence below.
    """
    text = raw["ccy"].strip()
    if not text:
        raise ValueError("幣別（ccy）不可空白")
    try:
        return Currency(text.upper())
    except ValueError:
        supported = "／".join(c.value for c in Currency)
        raise ValueError(f"幣別（ccy）無法辨識：{text}（僅支援 {supported}）") from None


def _parse_row(raw: dict[str, str]) -> tuple[CashMovementInput | None, Issue | None]:
    """One CSV row -> a validator input, or a hard ``parse_error`` issue."""
    try:
        account_id, alias_issue = alias_import_account(raw.get("account", "").strip())
        inp = CashMovementInput(
            account_id=account_id,
            date=_date_cell(raw, "date", "日期"),
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
    except ValidationError as exc:
        # BEFORE the ``ValueError`` arm, because ``ValidationError`` IS a ``ValueError`` —
        # which is how pydantic's English report reached the owner's 原因 column in the first
        # place (see :func:`_finite`). With both numeric cells guarded above, no input this
        # parser builds can reach here today; the arm exists so the NEXT constraint added to
        # ``CashMovementInput`` cannot silently re-open the leak. A model error names a
        # field, so the message names it too rather than restating a rule this module does
        # not own.
        errors = exc.errors()
        loc = errors[0].get("loc", ()) if errors else ()
        field = ".".join(str(p) for p in loc)
        return None, Issue(
            kind="parse_error",
            message=f"欄位 {field} 的內容格式不正確" if field else "資料格式不正確")
    except (ValueError, InvalidOperation) as exc:
        return None, Issue(kind="parse_error", message=str(exc))
    return inp, alias_issue


def _inert_probe(
    account_id: str,
    ccy: Currency,
    *,
    include: Sequence[CashMovementInput] = (),
    exclude_id: int | None = None,
) -> CashPool:
    """A deliberately inert :class:`CashPoolFn`: it reads no ledger and refuses nothing.

    Its ONLY caller is :func:`_pool_free_issues`, and its only job is to neutralise the
    withdraw branch so that what comes back is the STRUCTURAL prefix of the shared validator.
    That prefix decides BATCH MEMBERSHIP; it never decides a row's own verdict, which is
    answered a few lines later by the real injected probe, for every row. So this cannot
    weaken a guard — it can only keep a row OUT of the set the guard reasons over.

    Why this and not a list of "structural" issue kinds: the validator returns at most one
    issue and every pool-free check runs before the withdraw guard, so neutralising the pool
    is exact by construction and stays exact if a future check is added on either side of the
    line. Naming the kinds instead would go stale silently, which is the failure mode this
    whole wave is about.
    """
    return CashPool(balance=_UNBOUNDED, low=_ZERO)


def _pool_free_issues(
    conn: sqlite3.Connection,
    inp: CashMovementInput,
    *,
    accounts: dict[str, Account],
) -> list[Issue]:
    """The shared validator's verdict with the MONEY question taken out of it.

    Everything but the withdraw guard: unknown kind, non-positive amount, unknown account,
    currency↔account coherence (audit C2), and the acquisition-cost rules (F1). Re-running the
    one owner of those rules is deliberate — restating them here would make this module a
    second owner, which is the failure ``validate_cash_movement`` was extracted to end.
    """
    return validate_cash_movement(conn, inp, pool=_inert_probe, accounts=accounts)


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
    conn: sqlite3.Connection,
    csv_text: str,
    *,
    pool: CashPoolFn,
    select: Collection[int] | None = None,
) -> ImportPreview:
    """Parse *csv_text* into an :class:`ImportPreview` of cash_movements rows.

    Expected columns: account, date, kind, ccy, amount, acq_home_amount (optional), note
    (optional).

    Every hard finding is :func:`validate_cash_movement`'s — the SAME guard the manual door
    runs, including FU-D43a's withdraw block — so the two doors cannot diverge. This file
    adds only the two soft, bulk-specific advisories in :func:`_bulk_only_issues`.

    *pool* is the injected pool arithmetic and has no default; see the module docstring.

    *select* is the set of row indices that will actually be COMMITTED (``None`` = all of
    them, which is the preview door and every non-selecting caller). It narrows the batch a
    withdrawal is validated against, never what this door permits: a deselected row still gets
    its own verdict, it simply stops funding its siblings.
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

    # Hoisted ONCE for the whole file (trap #21): both are whole-table reads that
    # ``validate_cash_movement`` and the duplicate check would otherwise repeat per row.
    accounts = {a.account_id: a for a in list_accounts(conn)}
    stored = list_cash_movements(conn)

    # ⚠ The batch is the set of rows that will be WRITTEN — structurally valid AND selected
    # (see the module docstring). The row's OWN balance verdict is deliberately not part of
    # this test: excluding a row because its withdraw guard failed would make the membership
    # self-referential, and the direction it errs in is the safe one — a debit that will not
    # be written was counted, so its siblings were judged with LESS headroom than they get.
    batch = [
        inp
        for idx, _raw, inp, _issues in parsed
        if inp is not None
        and (select is None or idx in select)
        and not _pool_free_issues(conn, inp, accounts=accounts)
    ]

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
