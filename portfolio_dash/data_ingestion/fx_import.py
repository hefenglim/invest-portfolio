"""CSV import for fx_conversions — reuses the preview/commit infrastructure.

**The pool probe is a REQUIRED argument** (QA-09, 2026-08-29). Until then this door had no
pool probe at all and no currency check: a grep for ``pool|balance|_allowed_ccys`` over the
whole file returned nothing, while ``POST /api/cash/fx`` next to it enforced both. That is
precisely what ``architecture.md`` rejects at the C3 seam — "leaving the guard in ``api/``
(legal, but then the bulk door ships a weaker guard than the single-row form)". A 換匯 is the
one movement that may NEVER overdraft (FU-D34, 需求五: no ack override, no financing), so the
weaker door was the one that could book it.

The layering is resolved by INJECTION, D17's shape, exactly as the cash door resolves it: the
balance and the date-ordered running minimum live in ``portfolio/cash.py``, which
``data_ingestion`` may not import, so ``api/routers/input_center.py`` — the layer above both —
binds the arithmetic once per file and hands it in. The argument has **no default** on
purpose: forgetting to bind it is a mypy error and a ``TypeError``, never a quiet overdraft.

**The guard's checks live here, not at the router**, so ``POST /api/cash/fx``,
``PUT /api/ledgers/fx/{id}`` and this importer cannot answer three different things about the
same conversion — :func:`fx_ccy_issues` and :func:`fx_balance_issues` are called by all three.

**A conversion is TWO movement legs to the pool arithmetic** (:func:`fx_legs`). Modelling it
that way is what lets the existing ``CashPoolFn`` — whose ``include`` takes movement rows —
answer for a conversion with no protocol change: ``portfolio/cash.py`` already signs a
WITHDRAW as a debit and a DEPOSIT as a credit, and already orders same-day credits before
debits, which is exactly how it treats a real ``fx_out``/``fx_in`` pair.

**The batch is the set of rows that will be WRITTEN** (QA-01's invariant, applied here so the
door being fixed does not repeat the bug of the door one over). A row this preview rejects,
and a row the caller deselected on the commit, are both rows no ledger will ever hold; neither
may fund a sibling. E1a is preserved: a file that converts TWD it acquired in the SAME file is
still importable, because a sibling that IS being written still counts.
"""

import csv
import io
import sqlite3
from collections.abc import Collection, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import NamedTuple

from portfolio_dash.data_ingestion.preview import ImportPreview, PreviewRow
from portfolio_dash.data_ingestion.store import insert_fx_conversion, list_accounts
from portfolio_dash.data_ingestion.validate import (
    CashMovementInput,
    CashPoolFn,
    Issue,
    alias_import_account,
    amount_too_large_issue,
    dip_phrase,
    unknown_account_issue,
)
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.assets import Account
from portfolio_dash.shared.wire import decimal_str

# Canonical CSV column order for the fx_conversions import — SINGLE SOURCE for the downloadable
# template header (see data_ingestion.import_templates). Kept in lockstep with the DictReader
# keys below by the round-trip guard test.
FX_COLUMNS: list[str] = ["account", "date", "from_ccy", "from_amount", "to_ccy", "to_amount"]

_ZERO = Decimal("0")


class _ParsedFx(NamedTuple):
    """One structurally-parsed conversion row, before any ledger is consulted."""

    account_id: str
    on: date
    from_ccy: Currency
    from_amount: Decimal
    to_ccy: Currency
    to_amount: Decimal


def fx_legs(
    *,
    account_id: str,
    on: date,
    from_ccy: Currency,
    from_amount: Decimal,
    to_ccy: Currency,
    to_amount: Decimal,
) -> list[CashMovementInput]:
    """One conversion as the two movement rows the pool arithmetic already understands.

    A conversion is not a row type ``CashPoolFn.include`` accepts, and widening that protocol
    would mean editing the guard every door shares. It does not need widening: the sell leg IS
    a debit of ``from_amount`` in ``from_ccy`` and the buy leg IS a credit of ``to_amount`` in
    ``to_ccy``, on the same date, and ``portfolio/cash.py`` derives both the balance and the
    running minimum from exactly those two facts (it orders same-day credits before debits for
    a real ``fx_in``/``fx_out`` pair too, so the timeline is identical).

    The kinds are the canonical UPPER-CASE spellings: ``portfolio/cash.py`` signs a movement by
    ``kind == "WITHDRAW"``, so a lower-case one would be counted as a CREDIT and the guard
    would watch the pool go up while it was going down.
    """
    return [
        CashMovementInput(account_id=account_id, date=on, kind="WITHDRAW",
                          ccy=from_ccy, amount=from_amount),
        CashMovementInput(account_id=account_id, date=on, kind="DEPOSIT",
                          ccy=to_ccy, amount=to_amount),
    ]


def fx_ccy_issues(
    account: Account, from_ccy: Currency, to_ccy: Currency
) -> list[Issue]:
    """audit C2: both legs must be in the account's {settlement, funding} currencies.

    The same rule and the same wording ``validate_cash_movement`` applies to a movement —
    a pool the account cannot hold is not a pool, it is a typo. Returned in leg order so a
    caller that needs a form-field name can tell which leg was refused.
    """
    allowed = {account.settlement_ccy, account.funding_ccy}
    return [
        Issue(kind="ccy_not_allowed",
              message=(f"{leg.value} 非此帳戶可用幣別"
                       f"（交割幣 {account.settlement_ccy.value}／資金幣 "
                       f"{account.funding_ccy.value}）"))
        for leg in (from_ccy, to_ccy)
        if leg not in allowed
    ]


def fx_amount_issues(from_amount: Decimal, to_amount: Decimal) -> list[Issue]:
    """M5-05: both legs bounded at the M4 magnitude, in leg order (like :func:`fx_ccy_issues`).

    The 換匯 door had no bound at all: a 31-digit leg was refused only if the pool happened
    not to cover it, and written otherwise — after which every later sum in that pool lost
    its last digit to the 28-digit Decimal context. Called by the CSV door's structural pass
    (so an oversized row funds no sibling) and by ``api/routers/cash.py::fx_change_guard``
    (the manual and edit doors), so the three cannot answer differently.
    """
    return [
        issue
        for issue in (amount_too_large_issue(from_amount, "換出金額"),
                      amount_too_large_issue(to_amount, "換入金額"))
        if issue is not None
    ]


def fx_balance_issues(
    *,
    account: Account,
    on: date,
    from_ccy: Currency,
    from_amount: Decimal,
    to_ccy: Currency,
    to_amount: Decimal,
    pool: CashPoolFn,
    siblings: Sequence[CashMovementInput] = (),
) -> list[Issue]:
    """FU-D34 + audit C3 for ONE conversion — the guard EVERY 換匯 door runs.

    **Two checks, and both of them** (QA-11). The balance test reads the pool AS OF the
    conversion's own date (M5-06 — it used to be the END balance of the whole ledger, which
    a deposit dated 2099 could inflate); for a conversion dated today that is the 賬戶現金
    figure the 換匯中心 line displays, so the frontend hint and the backend authority never
    disagree. But a balance on one day cannot see a conversion that strands a LATER spend,
    and an end aggregate could not see a BACK-DATED one: one that leaves the pool at −320,000
    from January to March nets to zero by the end, and was accepted. The withdraw guard next
    door has used the date-ordered running minimum since audit C3, and the equity side has
    been date-aware since 2026-07-31 for the identical problem class ("a net-only check let a
    back-dated sell through"). So the running minimum — over the WHOLE timeline, future rows
    included — is added ALONGSIDE the balance, never in place of it, and its message names
    the day the pool bottoms (M5-07).

    A PRE-EXISTING dip the conversion does not DEEPEN never blocks it
    (``after.low < min(before.low, 0)``) — scoped exactly like ``_withdraw_issues``, so a
    ledger already in the red stays correctable.

    *siblings* are the would-be legs of the OTHER conversions being written in the same batch
    (empty for a single-row door). There is no ack override in either branch: FU-D34 is a hard
    rule, and a door that offered one would be financing.
    """
    before = pool(account.account_id, from_ccy, include=siblings, as_of=on)
    if from_amount > before.balance:
        return [Issue(
            kind="fx_insufficient_balance",
            message=(f"換出金額 {decimal_str(from_amount)} {from_ccy.value} 超過 "
                     f"{account.name} 的 {from_ccy.value} 可用餘額 "
                     f"{decimal_str(before.balance)} — 換匯不可透支（不提供融資）"))]
    legs = fx_legs(account_id=account.account_id, on=on, from_ccy=from_ccy,
                   from_amount=from_amount, to_ccy=to_ccy, to_amount=to_amount)
    after = pool(account.account_id, from_ccy, include=[*siblings, *legs])
    if after.low < min(before.low, _ZERO):
        return [Issue(
            kind="fx_insufficient_balance",
            message=(f"此筆換匯會使 {account.name} 的 {from_ccy.value} 現金"
                     f"{dip_phrase(after.low_date)} {decimal_str(after.low)}"
                     "（換匯日早於資金到位）— 換匯不可透支，請先補登入金或換匯"))]
    return []


class _CellError(ValueError):
    """One unreadable cell, carrying a zh-TW sentence already fit for the 原因 column (H-2).

    A ``ValueError`` subclass, so nothing about the existing control flow changes — and a
    DISTINCT type, because that is the only way :func:`_parse_row`'s last arm can tell one of
    this module's own vetted sentences from a ``ValueError`` raised by something else. Without
    the distinction the choice is between echoing ``str(exc)`` (which is how CPython's English
    reached the owner in the first place) and swallowing a message that should have been
    shown; with it, the vetted sentence is rendered and anything else degrades to a zh
    fallback naming the row.
    """


def _cell(raw: dict[str, str], column: str, label: str) -> str:
    """One required, non-blank cell as text (H-2).

    Read by SUBSCRIPT on purpose: an absent header must raise ``KeyError`` into
    :func:`_parse_row`'s 缺少必填欄位 arm, because "you forgot the column" and "this cell is
    empty on row 4" are different owner mistakes with different fixes — the argument
    :func:`cash_import._date_cell` already makes for the date, applied to every required
    column of this door. ``build_fx_preview`` has already stripped every cell; stripping again
    costs nothing and makes a direct caller get the same answer.
    """
    text = raw[column].strip()
    if not text:
        raise _CellError(f"{label}（{column}）不可空白")
    return text


def _decimal_cell(raw: dict[str, str], column: str, label: str) -> Decimal:
    """One required Decimal cell, or a zh error naming the leg and echoing the cell (H-2).

    ``Decimal("abc")`` raises ``InvalidOperation``, whose ``str()`` is
    ``[<class 'decimal.ConversionSyntax'>]`` — a CPython class name, in the one column whose
    entire job is to tell the owner which cell to go and fix, and it did not even say which of
    the two amount legs held it. ``1,200`` (what Excel writes once the column is formatted)
    reached it by the same path. The RAW text is echoed rather than a re-formatted Decimal,
    for the same reason the non-finite guard echoes it: it is what the owner has to go and
    change.

    A NON-FINITE value (``NaN`` / ``±Infinity`` / ``sNaN``) constructs without raising and is
    deliberately NOT rejected here — QA-07 owns that verdict a few lines below, with its own
    sentence, and moving it would change a message two test files pin.
    """
    text = _cell(raw, column, label)
    try:
        return Decimal(text)
    except InvalidOperation:
        raise _CellError(f"{label}（{column}）格式不正確，目前是「{text}」") from None


def _currency_cell(raw: dict[str, str], column: str, label: str) -> Currency:
    """One currency cell, or a zh error naming the cell, the text and the supported set (H-2).

    ``Currency("GBP")`` raises ``'GBP' is not a valid Currency`` — English, and it names
    neither the column nor what this system does accept. The supported list is DERIVED from
    the enum, so a fourth currency cannot leave the sentence advertising three.

    ⚠ Upper-cased before the lookup, exactly as :func:`cash_import._currency` does it. This
    is the one place where H-2 changes what the door ACCEPTS rather than what it says: the
    cash door imported ``usd`` and this one answered ``'usd' is not a valid Currency``, and
    two bulk doors disagreeing about the same cell is precisely what ``architecture.md``'s C3
    seam exists to prevent. Refusing it would also have produced a self-contradictory
    sentence — 「無法辨識：usd（僅支援 TWD／USD／MYR）」, a message that refuses a code while
    listing it. The message echoes the RAW cell, not the upper-cased one.
    """
    text = _cell(raw, column, label)
    try:
        return Currency(text.upper())
    except ValueError:
        supported = "／".join(c.value for c in Currency)
        raise _CellError(
            f"{label}（{column}）無法辨識：{text}（僅支援 {supported}）") from None


def _date_cell(raw: dict[str, str], column: str, label: str) -> date:
    """One required ISO date cell, or a zh ``ValueError`` naming the column (G-1).

    ``date.fromisoformat`` raises a ``ValueError``, so :func:`_parse_row`'s
    ``except … str(exc)`` arm printed CPython's English into the import preview's 原因
    column: ``month must be in 1..12`` for ``2026-13-01``, ``Invalid isoformat string:
    '2026/07/01'`` for the shape Excel writes.

    Word for word the cash door's helper (:func:`cash_import._date_cell`), which carries the
    full argument — including why the column is read by SUBSCRIPT (an absent header must keep
    raising ``KeyError``) and why ``csv_import``'s equivalent is not imported instead. The two
    copies are held identical by ``tests/data_ingestion/test_m2_import_date_zh.py``, which
    compares the two doors' messages for equality rather than merely for Chinese.

    Both sentences are UNCHANGED by H-2; only the exception CLASS is now this module's
    :class:`_CellError` (still a ``ValueError``), and the blank check is the shared
    :func:`_cell` rather than a second copy of it.
    """
    text = _cell(raw, column, label)
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise _CellError(
            f"{label}（{column}）格式不正確，須為 YYYY-MM-DD，目前是「{text}」") from None


def _parse_row(raw: dict[str, str]) -> tuple[_ParsedFx | None, Issue | None]:
    """One CSV row -> a parsed conversion, or a hard ``parse_error`` issue.

    ``found`` is the parse_error when the conversion is None, and the (optional) legacy
    account-alias notice when it is not — one return, two meanings, as in ``cash_import``.

    **Every cell is read through a TYPED reader** (H-2). Until then only the date and the
    non-finite amounts had one, and the single ``except … str(exc)`` arm below put whatever
    Python happened to say into the import preview's 原因 column: ``'account'`` for a missing
    header, ``'GBP' is not a valid Currency`` for a bad code, ``[<class
    'decimal.ConversionSyntax'>]`` for ``abc`` — and the same class name for a blank cell and
    for ``1,200``, so three different mistakes were one unreadable sentence. The cash door has
    had the readers since E-1/G-1; this one had two of four, which is how the leak survived
    two waves of zh-TW work on this very function.
    """
    try:
        # Legacy Moomoo account id -> moomoo_my (+ soft info issue carried by the caller).
        # Read by SUBSCRIPT so an absent ``account`` header lands on the 缺少必填欄位 arm; a
        # BLANK cell deliberately does NOT become a parse error — it flows on to
        # ``unknown_account``, which is the finding it has always produced. Since L-1 that
        # finding says 「帳戶不可空白」 for the blank and 「帳戶 … 不存在」 for a named id, at
        # this door and the four beside it alike (``validate.unknown_account_issue``).
        account_id, alias_issue = alias_import_account(raw["account"].strip())
        parsed = _ParsedFx(
            account_id=account_id,
            on=_date_cell(raw, "date", "日期"),
            from_ccy=_currency_cell(raw, "from_ccy", "換出幣別"),
            from_amount=_decimal_cell(raw, "from_amount", "換出金額"),
            to_ccy=_currency_cell(raw, "to_ccy", "換入幣別"),
            to_amount=_decimal_cell(raw, "to_amount", "換入金額"),
        )
    except KeyError as exc:
        return None, Issue(kind="parse_error", message=f"缺少必填欄位 {exc.args[0]}")
    except _CellError as exc:
        # BEFORE the ``ValueError`` arm, because ``_CellError`` IS a ``ValueError``. Its text
        # is this module's own, already written for the owner, so it is rendered verbatim.
        return None, Issue(kind="parse_error", message=str(exc))
    except (ValueError, InvalidOperation):
        # ⚠ Deliberately does NOT echo ``str(exc)`` — that is the leak, and echoing it is how
        # every message above got into the 原因 column. No input the readers accept can reach
        # here today; the arm exists so the NEXT cell added to this parser cannot silently
        # re-open it. A row is the smallest thing this door can name without knowing which
        # cell failed, so that is what the sentence names.
        return None, Issue(
            kind="parse_error", message="此列有無法解析的欄位，請逐格檢查內容是否正確")
    # QA-07: ``Decimal("NaN")`` and ``Decimal("Infinity")`` CONSTRUCT — neither
    # ``_decimal_cell`` nor any except arm above fires for them — so the row was returned as
    # parsed and ``_structural_issues`` then evaluated
    # ``parsed.from_amount <= _ZERO``. A Decimal NaN comparison RAISES
    # ``InvalidOperation`` (a float NaN merely compares False), from outside every try in this
    # module: ``POST /api/import/preview`` answered HTTP 500 and a file holding one clean
    # conversion plus one NaN row lost BOTH. ``Infinity`` reached the balance guard instead
    # and produced 「換出金額 Infinity TWD 超過…可用餘額 0」, which reads as an ordinary
    # overdraft rather than as an unreadable cell; ``-Infinity`` was reported as 「必須大於 0」.
    #
    # Rejected as a hard ``parse_error`` — the same verdict the cash door reaches on the same
    # input (its pydantic model validates INSIDE its try) — so the row carries no payload, can
    # never be committed, and is excluded from the batch that funds its siblings. The raw cell
    # is echoed back rather than a re-formatted Decimal: it is what the owner has to go and fix.
    for label, amount, column in (("換出金額", parsed.from_amount, "from_amount"),
                                  ("換入金額", parsed.to_amount, "to_amount")):
        if not amount.is_finite():
            return None, Issue(
                kind="parse_error",
                message=f"{label}必須是有限數字，目前是「{raw.get(column, '')}」")
    return parsed, alias_issue


def _structural_issues(
    parsed: _ParsedFx, accounts: dict[str, Account]
) -> list[Issue]:
    """Every hard finding that needs no ledger — i.e. everything but the balance guard.

    Kept separate because it decides BATCH MEMBERSHIP: a row rejected here will never be
    written, so it must not fund a sibling. Unlike the movement validator (which returns at
    most one issue, reproducing the router's early returns) this door has always COLLECTED
    its findings — a preview shows a row, not the first error — so that is preserved.
    """
    issues: list[Issue] = []
    account = accounts.get(parsed.account_id)
    if account is None:
        # L-1: the shared sentence. A BLANK ``account`` cell rendered 「帳戶  不存在」 here —
        # two spaces, no name — while the cash door already said 「帳戶不可空白」.
        issues.append(unknown_account_issue(parsed.account_id))
    if parsed.from_amount <= _ZERO:
        issues.append(Issue(kind="non_positive_amount", message="換出金額必須大於 0"))
    if parsed.to_amount <= _ZERO:
        issues.append(Issue(kind="non_positive_amount", message="換入金額必須大於 0"))
    issues.extend(fx_amount_issues(parsed.from_amount, parsed.to_amount))  # M5-05
    if parsed.from_ccy == parsed.to_ccy:
        issues.append(Issue(
            kind="same_currency",
            message=f"換出與換入幣別不可相同（皆為 {parsed.from_ccy.value}）"))
    if account is not None:
        issues.extend(fx_ccy_issues(account, parsed.from_ccy, parsed.to_ccy))
    return issues


def _payload(parsed: _ParsedFx) -> dict[str, str]:
    """Commit data for :func:`write_fx_row` (every Decimal as a string)."""
    return {
        "account_id": parsed.account_id,
        "date": parsed.on.isoformat(),
        "from_ccy": parsed.from_ccy.value,
        "from_amount": str(parsed.from_amount),
        "to_ccy": parsed.to_ccy.value,
        "to_amount": str(parsed.to_amount),
    }


def build_fx_preview(
    conn: sqlite3.Connection,
    csv_text: str,
    *,
    pool: CashPoolFn,
    select: Collection[int] | None = None,
) -> ImportPreview:
    """Parse *csv_text* into an :class:`ImportPreview` of fx_conversions rows.

    Expected columns: account, date, from_ccy, from_amount, to_ccy, to_amount.

    Hard issues (block commit):
    - ``unknown_account``: account not found in accounts table.
    - ``parse_error``: required column missing or unparseable.
    - ``non_positive_amount``: from_amount or to_amount <= 0.
    - ``same_currency``: from_ccy == to_ccy.
    - ``ccy_not_allowed``: a leg outside the account's {settlement, funding} set (audit C2).
    - ``fx_insufficient_balance``: the from-pool cannot cover the sell amount, or the
      conversion introduces/deepens a below-zero dip in its timeline (FU-D34 + audit C3).

    *pool* is the injected pool arithmetic and has **no default**; see the module docstring.

    *select* is the set of row indices that will actually be COMMITTED (``None`` = all of
    them). It narrows the batch a row is validated against, never what the door permits: a
    deselected row still gets its own verdict, it simply stops funding its siblings.
    """
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("﻿")))  # tolerate a leading BOM
    parsed_rows: list[tuple[int, dict[str, str], _ParsedFx | None, list[Issue]]] = []
    for idx, raw0 in enumerate(reader):
        raw: dict[str, str] = {k.strip(): (v or "").strip()
                               for k, v in raw0.items() if k is not None}
        parsed, found = _parse_row(raw)
        parsed_rows.append((idx, raw, parsed, [found] if found is not None else []))

    # Hoisted ONCE for the whole file (trap #21): the per-row ``SELECT 1 FROM accounts`` this
    # replaces asked the same question N times, and the currency check needs the whole row
    # anyway, not just its existence.
    accounts = {a.account_id: a for a in list_accounts(conn)}

    # ⚠ The batch is the set of rows that will be WRITTEN — structurally valid AND selected.
    # A row this preview rejects, or one the caller deselected, is in no ledger afterwards and
    # must fund nothing. The balance verdict of a row is deliberately NOT part of this test:
    # excluding a row because its own guard failed would make the membership self-referential,
    # and the direction it errs in is the safe one (a debit that will not be written was
    # counted, so the remaining rows were judged with LESS headroom than they will have).
    structural: dict[int, list[Issue]] = {
        idx: _structural_issues(parsed, accounts)
        for idx, _raw, parsed, _issues in parsed_rows
        if parsed is not None
    }
    batch: list[tuple[int, _ParsedFx]] = [
        (idx, parsed)
        for idx, _raw, parsed, _issues in parsed_rows
        if parsed is not None
        and (select is None or idx in select)
        and not structural[idx]
    ]

    rows: list[PreviewRow] = []
    for idx, raw, parsed, issues in parsed_rows:
        if parsed is None:
            rows.append(PreviewRow(index=idx, raw=raw, issues=issues))
            continue
        all_issues = [*issues, *structural[idx]]
        account = accounts.get(parsed.account_id)
        if account is not None and not structural[idx]:
            # Only a row that is otherwise acceptable is priced: a conversion with a bad
            # amount or an impossible currency has no meaningful pool question to ask.
            siblings = [
                leg
                for sib_idx, sib in batch if sib_idx != idx
                for leg in fx_legs(
                    account_id=sib.account_id, on=sib.on, from_ccy=sib.from_ccy,
                    from_amount=sib.from_amount, to_ccy=sib.to_ccy, to_amount=sib.to_amount)
            ]
            all_issues.extend(fx_balance_issues(
                account=account, on=parsed.on, from_ccy=parsed.from_ccy,
                from_amount=parsed.from_amount, to_ccy=parsed.to_ccy,
                to_amount=parsed.to_amount, pool=pool, siblings=siblings))
        rows.append(PreviewRow(index=idx, raw=raw, payload=_payload(parsed),
                               issues=all_issues))

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
