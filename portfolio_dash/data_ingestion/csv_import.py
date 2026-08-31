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
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from portfolio_dash.data_ingestion.config_seed import get_fee_rule_set
from portfolio_dash.data_ingestion.dateparse import DateCandidate, resolve_date_column
from portfolio_dash.data_ingestion.fees import (
    FeeComputationError,
    compute_fees,
    etf_flag_issue_applies,
    resolve_etf_flag,
    supplied_snapshot,
)
from portfolio_dash.data_ingestion.fx_lookup import resolve_stamp_fx
from portfolio_dash.data_ingestion.holdings import load_action_index
from portfolio_dash.data_ingestion.preview import ImportPreview, PreviewRow
from portfolio_dash.data_ingestion.resolve import (
    ResolutionStatus,
    resolve,
    suggestion_tail,
)
from portfolio_dash.data_ingestion.rules_binding import fee_rule_for
from portfolio_dash.data_ingestion.store import insert_transaction
from portfolio_dash.data_ingestion.validate import (
    Issue,
    TxnInput,
    alias_import_account,
    transaction_structural_issues,
    validate_transaction,
)
from portfolio_dash.shared.corporate_actions import ActionIndex
from portfolio_dash.shared.models.enums import Side

# Canonical CSV column order for the transactions import — the SINGLE SOURCE the downloadable
# template header is built from (see data_ingestion.import_templates). These names MUST match
# the keys the DictReader in build_transaction_preview reads below (required: account, symbol,
# side, date, shares, price; optional: fee, tax, daytrade, note). The round-trip guard test
# re-parses the generated template to prove header ↔ parser stay in lockstep.
TRANSACTION_COLUMNS: list[str] = [
    "account", "symbol", "side", "date", "shares", "price",
    "fee", "tax", "daytrade", "short_sale", "note",
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
    *,
    batch: Sequence[TxnInput] = (),
    action_index: ActionIndex | None = None,
) -> PreviewRow:
    """Build a :class:`PreviewRow` for a single transaction input.

    Runs validation, symbol resolution, and fee/tax auto-fill from the account's
    FeeRuleSet.  Reusable by both the CSV importer and the AI agents input path.

    Args:
        conn:  Active SQLite connection (schema in place, accounts seeded).
        index: Row index (0-based) used to identify the row in the preview.
        raw:   Original raw key/value mapping for display purposes.
        inp:   Parsed and typed transaction input.
        batch: Every row committed together, INCLUDING *inp* — the oversell guard counts
               the siblings so a sell covered by a buy earlier in the same file is not
               flagged 賣超. Empty (the default) is the single-row behaviour.
        action_index: One :class:`ActionIndex` for the whole file (D23 rule 2 / trap #21).
               Omitted, ``validate_transaction`` reads one PER ROW.

    Returns:
        A fully populated :class:`PreviewRow`.
    """
    issues: list[Issue] = list(
        validate_transaction(conn, inp, index=action_index, batch=batch))

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
        # Registry-authoritative ETF flag + its third state — ONE definition, shared with
        # manual.py (fees.resolve_etf_flag). Stress-audit 2026-07-15 made the registry win
        # over the input flag; AI-D40 (2026-08-24) closed the hole underneath it, where the
        # value SEEDING the registry was an unanswered False.
        is_etf, etf_unknown = resolve_etf_flag(res.instrument, inp.is_etf)
        # Market-aware fee rule (Batch B): the resolved instrument selects the rule set bound
        # to (account, its market); an unregistered symbol (res.instrument None) keeps the
        # account scalar. Snapshot semantics unchanged (binding mirrors the scalar today).
        rule_name = (
            fee_rule_for(conn, inp.account_id, res.instrument.market)
            if res.instrument is not None else acc["fee_rule_set"]
        )
        rules = get_fee_rule_set(rule_name, conn)
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
            if etf_flag_issue_applies(rules, inp.side, etf_unknown):
                # Narrow on purpose: only when the unresolved flag actually MOVES the tax
                # (see manual.py — the bulk door must not ship a weaker guard than the
                # single-row form, nor a noisier one).
                issues.append(Issue(
                    kind="etf_flag_unknown", needs_confirm=True,
                    message="無法判定是否為 ETF,賣出稅率待確認"
                            "(暫以現股稅率試算,請至標的管理設定)"))
            if fee is None:
                fee = fr.fee
            if tax is None:
                tax = fr.tax
            snap = dict(fr.snapshot)
            if etf_unknown:
                snap["etf_flag"] = "unknown"
            supplied = [k for k, v in (("fee", inp.fee), ("tax", inp.tax))
                        if v is not None]
            if supplied:
                # PARTIAL auto-fill: the snapshot describes the regime, but one of the
                # two numbers came from the caller, not from it. Say which.
                snap["supplied"] = ",".join(supplied)
    elif fee is not None and tax is not None:
        # Both supplied (the broker-import shape) — record the provenance instead of
        # leaving {}, which reads as "no rule applied". See fees.supplied_snapshot.
        snap = supplied_snapshot(fee, tax)

    # Build payload for the writer (string dict + prefixed snapshot entries)
    payload: dict[str, str] = {
        "account_id": inp.account_id,
        "symbol": symbol,
        "side": inp.side.value,
        "quantity": str(inp.quantity),
        "price": str(inp.price),
        "trade_date": inp.trade_date.isoformat(),
        "daytrade": "1" if inp.daytrade else "0",  # persisted through the writer (MED-1)
        "short_sale": "1" if inp.short_sale else "0",  # same seam as daytrade
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


class _CellError(Exception):
    """One CSV cell that could not be read, carrying the COLUMN and the VALUE that broke it.

    The two attributes are the whole point. ``str(exc)`` on the underlying Python exception
    was what reached the import preview's 原因 column — the one column whose entire job is to
    tell the owner why a row was rejected — and it said, verbatim (QA-23):

        ``[<class 'decimal.ConversionSyntax'>]``   for ``1,200`` typed into ``shares``
        ``'shares'``                                for a header missing that column
        ``Invalid isoformat string: '2026/01/05'``  for a slash-separated date

    A CPython class name is not a reason, and none of the three names a column the owner can
    go and look at. The static zh-TW guard cannot catch this class either: it scans
    ``Issue(message=<literal>)`` and skips non-literals, and ``str(exc)`` is a call — so the
    rule is enforced by the typed readers below, at the point where the column name is still
    in scope, rather than by inspection afterwards.
    """

    def __init__(self, column: str, value: str, reason: str) -> None:
        self.column = column
        self.value = value
        self.reason = reason
        super().__init__(f"{column}: {reason}")

    @property
    def message(self) -> str:
        """The 原因 column's text: what is wrong, which column, and what was typed."""
        if not self.value:
            return f"{self.reason}（欄位 {self.column}）"
        return f"欄位 {self.column} 的內容「{self.value}」{self.reason}"


_MISSING_COLUMN = "缺少必要欄位"
_BLANK_CELL = "必填欄位不可空白"
_NOT_A_NUMBER = "不是有效的數字（請移除千分位逗號、貨幣符號與空白）"
_NOT_A_DATE = "不是有效的日期（請用 YYYY-MM-DD）"
_NOT_A_SIDE = "不是有效的買賣別（請填 BUY 或 SELL）"


def _cell(raw: dict[str, str], column: str, *, required: bool = True) -> str:
    """Read one column, distinguishing an ABSENT column from a blank cell.

    They are different owner mistakes — a missing header versus an empty cell on one row —
    and ``KeyError`` used to render both as the bare quoted word ``'shares'``.
    """
    if column not in raw:
        raise _CellError(column, "", _MISSING_COLUMN)
    value = raw[column]
    if required and not value:
        raise _CellError(column, "", _BLANK_CELL)
    return value


def _decimal_cell(raw: dict[str, str], column: str) -> Decimal:
    value = _cell(raw, column)
    try:
        return Decimal(value)
    except InvalidOperation:
        raise _CellError(column, value, _NOT_A_NUMBER) from None


def _optional_decimal_cell(raw: dict[str, str], column: str) -> Decimal | None:
    """``fee`` / ``tax``: absent or blank means "not supplied", which is not an error.

    Blank is meaningful here — ``data-and-pricing.md``'s provenance rule turns a supplied
    fee into a ``"source": "supplied"`` snapshot and an unsupplied one into a computed
    figure, so the two must stay distinguishable. Only a non-empty, unparseable cell fails.
    """
    value = raw.get(column, "")
    if not value:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        raise _CellError(column, value, _NOT_A_NUMBER) from None


def _date_cell(raw: dict[str, str], column: str) -> date:
    value = _cell(raw, column)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise _CellError(column, value, _NOT_A_DATE) from None


def _side_cell(raw: dict[str, str], column: str) -> Side:
    value = _cell(raw, column)
    try:
        return Side(value.upper())
    except ValueError:
        raise _CellError(column, value, _NOT_A_SIDE) from None


def build_transaction_preview(
    conn: sqlite3.Connection,
    csv_text: str,
    *,
    pending_actions: ActionIndex | None = None,
    select: Collection[int] | None = None,
) -> ImportPreview:
    """Parse *csv_text* into an :class:`ImportPreview` of transaction rows.

    Each row is validated, symbol-resolved, and auto-filled with fee/tax from
    the account's FeeRuleSet (unless the CSV already supplies those columns).
    Rows that fail to parse are captured with a ``parse_error`` issue.

    **The whole file is one batch** — the phrase, and the reasoning, are ``cash_import``'s.
    Rows are parsed in a FIRST pass so the oversell guard can see every sibling before it
    judges any single row; without that, a sell whose covering buy is three lines above it
    is flagged 賣超, and 賣超 is the one confirmation in this system that permanently
    discards a cost basis. One :class:`ActionIndex` is read for the whole file rather than
    per row (D23 rule 2 / trap #21): on a 1,375-row export that is 1,374 fewer full reads
    and regroupings of the corporate-action ledger.

    Args:
        conn:     Active SQLite connection (schema in place, accounts seeded).
        csv_text: Full CSV text including a header row.  Required columns:
                  ``account``, ``symbol``, ``side``, ``date``, ``shares``,
                  ``price``.  Optional: ``fee``, ``tax``, ``note``, ``daytrade``
                  (``1``/``true`` marks a TW same-day round trip → 0.15% sell tax),
                  ``short_sale`` (``1``/``true`` marks a DECLARED short sale — the only
                  sell allowed to exceed holdings without tripping the 賣超 guard).
        pending_actions: an :class:`ActionIndex` ALREADY widened with corporate actions that
                  are about to be imported alongside these trades (the broker one-click
                  flow, so a post-split sell does not demand the 賣超 ack for an action
                  that arrives seconds later). Omitted, the stored ledger's index is read.
        select: the row indices that will actually be COMMITTED (``None`` = all of them —
                  the preview door and every non-selecting caller). It narrows the sibling
                  BATCH the oversell guard counts, never what this door permits or which
                  rows appear in the output: every row keeps its index, raw text and
                  payload, so ``row_hashes`` and the wire stay aligned; a deselected row
                  still gets its own verdict, it simply stops covering its siblings
                  (QA-01, extended to transactions 2026-08-29 — a covering buy the caller
                  deselected let the sell it covered commit alone: 200, ``written: 1``,
                  holdings at −60 shares, and the 賣超 confirmation was never shown).

    Returns:
        :class:`ImportPreview` containing one :class:`PreviewRow` per data row.
    """
    # lstrip a leading UTF-8 BOM: the downloadable template ships WITH a BOM (Excel), so a
    # download->re-upload (or paste) round-trip must not turn the first header into a BOM+account.
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))
    rows: list[PreviewRow] = []
    #: Pass 1's output: ``(index, raw, parsed-or-None)``. Pass 2 validates each entry
    #: against ALL of it.
    parsed: list[tuple[int, dict[str, str], TxnInput | None, Issue | None]] = []

    for idx, raw_row in enumerate(reader):
        raw = {k.strip(): (v or "").strip() for k, v in raw_row.items()}

        # --- parse: build TxnInput from CSV columns ---
        try:
            # Legacy Moomoo account id -> moomoo_my (+ soft info issue appended below).
            account_id, alias_issue = alias_import_account(_cell(raw, "account"))
            inp = TxnInput(
                account_id=account_id,
                symbol=_cell(raw, "symbol"),
                side=_side_cell(raw, "side"),
                quantity=_decimal_cell(raw, "shares"),
                price=_decimal_cell(raw, "price"),
                trade_date=_date_cell(raw, "date"),
                fee=_optional_decimal_cell(raw, "fee"),
                tax=_optional_decimal_cell(raw, "tax"),
                daytrade=raw.get("daytrade", "").lower() in ("1", "true", "y", "yes"),
                # A DECLARED short sale — the ONLY way a sell may exceed holdings without the
                # 賣超 guard. Absent, blank or unrecognised means False, and False is the safe
                # default in both directions: a genuine short mis-imported as an ordinary sell
                # is loudly flagged 賣超 (待釐清), whereas inferring a short from an oversell
                # would turn a data-entry slip into a plausible-looking realized loss — the
                # dangerous failure mode domain-ledger.md names, a wrong number that looks right.
                short_sale=raw.get("short_sale", "").lower() in ("1", "true", "y", "yes"),
                note=raw.get("note") or None,
            )
        except _CellError as exc:
            parsed.append((idx, raw, None, Issue(kind="parse_error", message=exc.message)))
            continue
        except (KeyError, ValueError, InvalidOperation):
            # Belt and braces. Everything above is read through a typed cell reader, so this
            # arm is reachable only via ``TxnInput``'s own model validation (pydantic's
            # ValidationError IS a ValueError). Its text is English and structural, so the
            # owner gets a Chinese sentence pointing at the row instead — never ``str(exc)``,
            # which is exactly how the Python internals got onto the screen in the first place.
            parsed.append((idx, raw, None, Issue(
                kind="parse_error",
                message="這一列的內容無法解析，請對照範本檢查各欄位格式")))
            continue

        parsed.append((idx, raw, inp, alias_issue))

    # --- pass 2: validate each row against the ledger PLUS its siblings ---
    # Only rows that PARSED are siblings. An unparseable row has no account, symbol,
    # quantity or date, so it cannot cover anything; counting it would be inventing a flow
    # out of a defect. (``cash_import.py:260`` filters the same way, for the same reason.)
    #
    # And with *select*, only rows that will be WRITTEN are siblings (QA-01's rule, on the
    # share ledger): a buy the caller deselected is a flow no ledger will ever hold, so it
    # must not cover the sell that IS committed — the cash door's deselected-deposit
    # defect, replayed with shares instead of money.
    #
    # And a STRUCTURALLY invalid row is out too (FIX-A1b, Master probe V6, 2026-08-30) —
    # ``cash_import``'s other membership term, mirrored: its batch takes only rows clean of
    # ``_pool_free_issues`` (the shared validator with the money question neutralised), so
    # a deposit of −500 never funds a withdrawal. Here a buy priced −50.00 — a hard
    # ``error`` on its own line — still covered its sell, which then previewed ``ok`` and a
    # no-select commit wrote it ALONE: 200 / written 1 / a lone unacked oversold SELL.
    # Membership is the row-level structural prefix ONLY (decidable from the TxnInput,
    # no second validation pass); the ledger-dependent hard kinds stay in on the shared-key
    # argument — see :func:`~data_ingestion.validate.transaction_structural_issues`. The
    # row's own verdict is still rendered against the narrowed batch, exactly as cash does
    # for its excluded rows, and the exclusion errs conservative in both directions: a bad
    # buy stops lending shares it will never book, and a bad SELL stops draining shares it
    # will never book — either way siblings are judged against what the ledger will hold.
    batch = [
        parsed_in
        for row_idx, _raw, parsed_in, _issue in parsed
        if parsed_in is not None
        and (select is None or row_idx in select)
        and not transaction_structural_issues(parsed_in)
    ]
    action_index = pending_actions if pending_actions is not None else load_action_index(conn)
    for idx, raw, row_inp, extra in parsed:
        if row_inp is None:
            rows.append(PreviewRow(index=idx, raw=raw, issues=[extra] if extra else []))
            continue
        row = txn_preview_row(conn, idx, raw, row_inp, batch=batch, action_index=action_index)
        if extra is not None:
            row.issues.append(extra)
        rows.append(row)

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
        short_sale=p.get("short_sale") == "1",
        commit=commit,
    )
