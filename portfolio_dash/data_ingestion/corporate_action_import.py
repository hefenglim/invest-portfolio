"""CSV import for corporate_actions — the 5th ledger kind (spec §6.5, W7).

**This module is what makes W2's validation real.** ``validate_corporate_action`` had zero
production callers (audit F-40), so every §5 rejection and every soft warning existed only
in tests; nothing enforced D15, and ``insert_corporate_action`` was a plain INSERT with no
coupling to any rule. Every corporate action that is not typed into the §6.7 form arrives
through here, so this file is the guard.

Two obligations no other CSV kind has (audit F-29):

1. **``validate_corporate_action`` is called with the FULL batch.** E12, E13 and the
   conflicting-ratio guard are batch-level rules. A per-row call would reject a correct
   multi-account entry (each row sees a holder account it does not itself cover) and accept
   a partial one — and a corporate-action CSV is multi-symbol, multi-account by
   construction, because D13 requires N rows for N holding accounts.
2. **``book`` and the ``ActionIndex`` are hoisted ONCE for the file.** Both replay or
   re-group the entire ledger; building them per row is trap #21 with a different object
   (a 1,400-row import would replay the whole ledger 1,400 times).

**Two ratio columns, never one** (E6a). A single ``ratio`` column is refused at the header
with its own message rather than parsed: silently accepting ``0.2857`` is exactly the
failure §3.1(ii) documents — 700 shares become 199.9900, the later sell of 200 trips the
賣超 guard, and acknowledging it discards the position's cost basis permanently. Both
columns are read as Decimals and handed to E6/E6a, which owns the positive-integer rule and
its zh message; parsing them as ``int`` here would produce a second, English rejection for
the same defect (one rule, one message).
"""

import csv
import io
import sqlite3
from datetime import date
from decimal import Decimal, InvalidOperation

from portfolio_dash.data_ingestion.holdings import load_action_index
from portfolio_dash.data_ingestion.preview import ImportPreview, PreviewRow
from portfolio_dash.data_ingestion.register import autoregister_spinoff_child
from portfolio_dash.data_ingestion.store import (
    insert_corporate_action,
    load_ledger_bundle,
    move_target_band,
)
from portfolio_dash.data_ingestion.validate import (
    CorporateActionInput,
    Issue,
    alias_import_account,
    validate_corporate_action,
)
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.portfolio.results import Book
from portfolio_dash.shared.corporate_actions import ActionIndex, CorporateActionKind
from portfolio_dash.shared.models.ledger import LedgerBundle

# Canonical CSV column order — SINGLE SOURCE for the downloadable template header
# (see data_ingestion.import_templates) and for scripts/verify_corporate_actions.py's
# ``--actions`` file. Kept in lockstep with the DictReader keys below by the round-trip
# guard test.
CORPORATE_ACTION_COLUMNS: list[str] = [
    "account", "date", "kind", "from_symbol", "to_symbol",
    "ratio_to", "ratio_from", "cost_carry", "note",
]

# The owner reads 分割 / 換股 / 分拆 on a statement; SPLIT / EXCHANGE / SPINOFF is OUR
# vocabulary (§6.7). Both spellings are accepted here for the same reason the entry form
# asks what the statement shows instead of asking for a classification. An unrecognised
# label is passed through UNCHANGED so E's ``unknown_action_kind`` message can name what
# the owner actually typed.
_KIND_ALIASES: dict[str, CorporateActionKind] = {
    "分割": CorporateActionKind.SPLIT,
    "股票分割": CorporateActionKind.SPLIT,
    "換股": CorporateActionKind.EXCHANGE,
    "分拆": CorporateActionKind.SPINOFF,
}


def _canonical_kind(raw: str) -> str:
    """Map a zh label onto the stored enum value; pass anything else through unchanged."""
    aliased = _KIND_ALIASES.get(raw.strip())
    return aliased.value if aliased is not None else raw.strip().upper()


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


_SINGLE_RATIO = Issue(
    kind="single_ratio_column",
    message=(
        "這個檔案只有一個 ratio 欄位。公司行動的比例必須拆成 ratio_to 與 ratio_from "
        "兩個整數欄位（例如 3 換 1 就填 ratio_to=3、ratio_from=1）。"
        "用一個算好的小數（例如 0.2857）會讓股數短少 — 700 股的 2 換 7 會算成 "
        "199.99 股，之後賣出 200 股時會被誤判為賣超，成本基礎會被永久捨棄。"
        "請下載最新的匯入範本，或自行把欄位改成兩欄再上傳"
    ),
)


def _parse_row(raw: dict[str, str]) -> tuple[CorporateActionInput | None, Issue | None]:
    """One CSV row -> a validator input, or a hard ``parse_error`` issue."""
    try:
        account_id, alias_issue = alias_import_account(raw.get("account", "").strip())
        action_date = date.fromisoformat(raw["date"].strip())
        inp = CorporateActionInput(
            account_id=account_id,
            date=action_date,
            kind=_canonical_kind(raw.get("kind", "")),
            from_symbol=raw.get("from_symbol", "").strip(),
            to_symbol=raw.get("to_symbol", "").strip(),
            ratio_to=_decimal(raw, "ratio_to", "換得股數"),
            ratio_from=_decimal(raw, "ratio_from", "換出股數"),
            cost_carry=_optional_decimal(raw, "cost_carry", "成本分攤比例"),
            note=(raw.get("note", "").strip() or None),
        )
    except KeyError as exc:
        return None, Issue(kind="parse_error", message=f"缺少必填欄位 {exc.args[0]}")
    except ValueError as exc:
        return None, Issue(kind="parse_error", message=str(exc))
    return inp, alias_issue


def _payload(inp: CorporateActionInput) -> dict[str, str]:
    """Commit data for :func:`write_corporate_action_row` (all Decimals as strings)."""
    payload = {
        "account_id": inp.account_id,
        "date": inp.date.isoformat(),
        "kind": inp.kind,
        "from_symbol": inp.from_symbol,
        "to_symbol": inp.to_symbol,
        "ratio_to": str(inp.ratio_to),
        "ratio_from": str(inp.ratio_from),
    }
    if inp.cost_carry is not None:
        payload["cost_carry"] = str(inp.cost_carry)
    if inp.note:
        payload["note"] = inp.note
    return payload


def parse_action_batch(csv_text: str) -> list[CorporateActionInput]:
    """The rows of a corporate-action CSV that PARSE, for use as a pending batch.

    Exists so the widened :func:`~data_ingestion.holdings.load_action_index` has exactly one
    way to be fed. The broker import door needs it for a different reason than this module
    does: its TRADES have to be validated against the actions arriving in the same run, or a
    post-split sell raises 賣超 for an action the owner is importing seconds later — and a
    賣超 confirmation permanently discards a cost basis.

    Rows that do not parse are dropped rather than reported. They are not this function's to
    report: whoever imports that file gets the ``parse_error`` on the row itself, in its own
    preview. Here the question is only "what actions are about to exist", and a row that
    cannot be read is not one of them.
    """
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("﻿")))
    out: list[CorporateActionInput] = []
    for raw0 in reader:
        raw = {k.strip(): (v or "").strip() for k, v in raw0.items() if k is not None}
        inp, _found = _parse_row(raw)
        if inp is not None:
            out.append(inp)
    return out


def build_corporate_action_preview(
    conn: sqlite3.Connection, csv_text: str
) -> ImportPreview:
    """Parse *csv_text* into an :class:`ImportPreview` of corporate_actions rows.

    Expected columns: account, date, kind, from_symbol, to_symbol, ratio_to, ratio_from,
    cost_carry (SPINOFF only), note.

    Every §5 finding of :func:`~data_ingestion.validate.validate_corporate_action` is
    surfaced per row in its own tier — hard issues block the row, soft ones ride the
    existing ``ack_warnings`` confirmation. Nothing is re-implemented here.
    """
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("﻿")))  # tolerate a BOM
    header = {(h or "").strip() for h in (reader.fieldnames or [])}
    single_ratio = "ratio" in header and not {"ratio_to", "ratio_from"} <= header

    parsed: list[tuple[int, dict[str, str], CorporateActionInput | None, list[Issue]]] = []
    for idx, raw0 in enumerate(reader):
        raw: dict[str, str] = {k.strip(): (v or "").strip()
                               for k, v in raw0.items() if k is not None}
        if single_ratio:
            parsed.append((idx, raw, None, [_SINGLE_RATIO]))
            continue
        # ``found`` is the parse_error when ``inp`` is None, and the (optional) legacy
        # account-alias notice when it is not — one return, two meanings, per _parse_row.
        inp, found = _parse_row(raw)
        parsed.append((idx, raw, inp, [found] if found is not None else []))

    batch = [inp for _idx, _raw, inp, _issues in parsed if inp is not None]

    # --- hoisted ONCE for the whole file (F-29 / trap #21) ---------------------------
    # `validate_corporate_action` builds both itself when they are omitted, i.e. once per
    # row. Reading every ledger out of SQLite and re-grouping the whole corporate-action
    # table are the expensive parts, so an N-row file would pay N times for one answer.
    #
    # ⚠ What is hoisted is the BUNDLE, not a replayed book (changed 2026-08-11). A single
    # book for the whole file was the wrong object: the four book-derived rejections
    # (E3/E22/E5/E18) must see the ledger **at each action's own date**, and a whole-ledger
    # book shows them a future in which the post-action trades have already happened — so
    # E3 rejected the very split that made those trades legal. `book_cache` keeps trap
    # #21's saving: one replay per distinct action DATE, not per row.
    #
    # ⚠ The index is STORED + THIS BATCH (2026-08-14). Measured on the demo corpus
    # 2026-08-12: a SPLIT whose symbol's only shares arrive from an EXCHANGE **earlier in
    # the same file** was hard-rejected `no_position_on_action_date`, because the sibling
    # EXCHANGE was not in the index and the share walk therefore reached a position that
    # did not exist yet. Both of the owner's real chains have that shape (a de-SPAC then a
    # rename; a de-SPAC then a reverse split), and the failure was invisible: the row
    # landed in `summary.skipped` and the import reported success having dropped it.
    #
    # A batch row cannot justify ITSELF. The walk's cut is
    # `(action.date, EventPriority.CORPORATE_ACTION)` and it applies only actions sorting
    # STRICTLY before it, so the row being validated is excluded from its own history by
    # the ordering rather than by a rule anyone has to remember — and D15/E12 already
    # forbids two same-date actions whose symbol sets intersect, which is the only case
    # that could reach the boundary. `test_a_lone_split_onto_an_empty_position_is_still
    # _rejected` is the paired proof.
    #
    # A malformed batch row excludes itself too: `convert_stored` cannot build a
    # `CorporateAction` from it, so it lands on `unreadable` and never reaches the walk.
    #
    # ⚠ Since 2026-08-29 (FIX-A2, QA BUG-04) the BOOK replay inside the validator honours
    # the same rule: `validate_corporate_action` hands its strictly-earlier batch siblings
    # to `_book_before`, so the four book-derived rejections (E3/E22/E5/E18) see the same
    # predecessors the index does. Before that, a chain whose prerequisite action was
    # same-batch and whose post-action trades were already stored (the owner's real GGR
    # shape) passed E1a but hard-rejected `oversold_source` — and a byte-identical second
    # upload wrote it, because pass 1 had stored the prerequisite. One file, one pass.
    index: ActionIndex = load_action_index(conn, pending=batch)
    bundle: LedgerBundle | None
    book_cache: dict[date, Book] = {}
    try:
        bundle = load_ledger_bundle(conn)
        # Replay once here purely as the reachability check the degradation below needs:
        # if the ledger cannot replay at all, say so once rather than N times.
        build_book(bundle, allow_oversell=True)
    except (ValueError, KeyError) as exc:
        # A ledger that cannot be replayed at all (e.g. a dividend stranded by an earlier
        # correction). Degrade to a hard issue on every row instead of a 500: this door
        # must never be the thing that crashes, and the owner needs to be told which
        # problem to fix first. Same posture as `ledgers._oversold_shares` returning None.
        return ImportPreview(rows=[
            PreviewRow(index=idx, raw=raw, issues=[Issue(
                kind="ledger_unbookable",
                message=("目前的帳本無法重播，因此無法檢核公司行動"
                         f"（{exc}）。請先修正帳本中的錯誤紀錄再匯入"))])
            for idx, raw, _inp, _issues in parsed
        ])

    rows: list[PreviewRow] = []
    for idx, raw, inp, issues in parsed:
        if inp is None:
            rows.append(PreviewRow(index=idx, raw=raw, issues=issues))
            continue
        all_issues: list[Issue] = [
            *issues,
            *validate_corporate_action(conn, inp, batch=batch, bundle=bundle,
                                       book_cache=book_cache, index=index),
        ]
        rows.append(
            PreviewRow(index=idx, raw=raw, payload=_payload(inp), issues=all_issues))
    return ImportPreview(rows=rows)


def write_corporate_action_row(
    conn: sqlite3.Connection, row: PreviewRow, *, commit: bool = True
) -> int:
    """Persist one accepted corporate_actions row and return its autoincrement id.

    ``commit`` is forwarded to the store insert; the batch path passes ``commit=False`` so
    the whole batch commits once (all-or-nothing, #1). **The caller must run the price
    reconcile afterwards** (``api.instrument_service.reconcile_price_basis``) — a SPLIT
    moves the stored closes, and the writer has no business reaching into ``pricing/``.

    D47's band move happens HERE rather than at the caller, so the bulk door and the form
    reach it by the same rule (``architecture.md``'s cash-guard asymmetry). It is idempotent
    across an N-account set: the first row clears the source, and every row after it finds
    nothing to move.
    """
    p = row.payload
    # Both defer their commit to *commit*, i.e. to the batch's single one. Committing here
    # would defeat ``commit_preview``'s all-or-nothing gate SILENTLY: a later row's failure
    # rolls back every action, and a band that had already moved — or a child instrument
    # already created — would survive an event that never happened.
    if p["kind"] == CorporateActionKind.EXCHANGE.value:
        move_target_band(conn, from_symbol=p["from_symbol"], to_symbol=p["to_symbol"],
                         commit=commit)
    if p["kind"] == CorporateActionKind.SPINOFF.value:
        autoregister_spinoff_child(conn, parent_symbol=p["from_symbol"],
                                   child_symbol=p["to_symbol"], commit=commit)
    return insert_corporate_action(
        conn,
        account_id=p["account_id"],
        action_date=date.fromisoformat(p["date"]),
        kind=CorporateActionKind(p["kind"]),
        from_symbol=p["from_symbol"],
        to_symbol=p["to_symbol"],
        ratio_to=Decimal(p["ratio_to"]),
        ratio_from=Decimal(p["ratio_from"]),
        cost_carry=(Decimal(p["cost_carry"]) if "cost_carry" in p else None),
        note=p.get("note"),
    )
