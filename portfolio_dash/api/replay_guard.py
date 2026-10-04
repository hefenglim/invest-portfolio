"""The ledger REPLAY GUARD — one decision for every door that removes or rewrites rows.

Moved out of ``api/routers/ledgers.py`` (DEF-049, 2026-09-25 — a verbatim move first, then
two additive parameters: the would-be corporate actions, and the full list of stranded
positions) so a second door can call the SAME function instead of a copy of it: the
import-batch undo
(``api/routers/input_center.py::import_batch_delete``) removes rows from four ledgers at once
and used to run a bare ``DELETE … WHERE import_batch_id=?`` with no replay at all — a later
sell went 賣超, its cost basis was discarded, and nothing asked first. The ledger tab's
per-row delete of the very same buy answered 422 ``oversell``.

``ledgers.py`` re-exports every name it used, under the same names, so the row-correction
routes (and anything that imports them from there) are unchanged.

L6 (``api/``): it reads the store and replays through ``portfolio.cost_basis.build_book`` —
both below it — and nothing below ``api/`` may import it. ``data_ingestion`` reaches it only
through the injected ``provenance.BatchUndoGuard`` seam.
"""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from portfolio_dash.api.errors import error_body

# The cash doors' own ``negative_cash`` sentence and scoped dip rule (a sibling router, same
# layer — the shape ``ledgers.py`` already uses for ``fx_delete_guard``; ``cash`` imports
# nothing from here, so no cycle), so the batch undo can neither word the overdraft
# differently from the 刪除 button beside each of its rows nor decide it differently (CR-13).
from portfolio_dash.api.routers.cash import (
    CashLedgers,
    _negative_response,
    _pool_dip,
    load_cash_ledgers,
)
from portfolio_dash.data_ingestion.provenance import BatchRows
from portfolio_dash.data_ingestion.store import (
    StoredCorporateAction,
    StoredDividend,
    StoredOpening,
    StoredTransaction,
    corporate_action_order_key,
    dividend_order_key,
    list_cash_movements,
    list_corporate_actions,
    list_dividends,
    list_instruments,
    list_opening,
    list_transactions,
    load_ledger_bundle,
    opening_order_key,
    transaction_order_key,
)
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.portfolio.results import Book, OversoldPosition
from portfolio_dash.shared.account_ref import account_ref
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.ledger import LedgerBundle, dividend_effective_date
from portfolio_dash.shared.wire import decimal_str


class _ReplayBlock(BaseModel):
    """A reason a correction is refused: an ``oversell`` (ack-bypassable) or an
    ``orphan`` (a dividend/opening record stranded by the mutation — hard).

    ``message`` names the FIRST stranded position — the sentence the row doors have always
    shown. ``orphans`` / ``oversold`` (DEF-049) carry EVERY position the mutation strands, so
    a door that removes many rows at once (the batch undo) can name each one; the decision
    that produced them is the same.
    """

    code: str  # "oversell" | "orphan"
    message: str
    orphans: list[tuple[str, str]] = Field(default_factory=list)   # (account, symbol)
    # CR-11: the BOOK's record, not ``Holding`` — a 賣超 netted back to exactly zero shares has
    # no Holding at all, and it is still a 賣超 the owner must be asked about.
    oversold: list[OversoldPosition] = Field(default_factory=list)


def _to_models(
    conn: sqlite3.Connection,
    txs: list[StoredTransaction] | None = None,
    divs: list[StoredDividend] | None = None,
    opening: list[StoredOpening] | None = None,
    actions: list[StoredCorporateAction] | None = None,
    *,
    instruments: dict[str, Instrument] | None = None,
) -> LedgerBundle:
    """The replay bundle for the mutated list(s); unspecified ledgers load from store.

    Rows whose symbol is unregistered are excluded (same degradation as the dashboard)
    so one legacy bad row cannot block corrections to healthy rows.

    ``actions`` (DEF-049) lets a batch undo replay the ledger WITHOUT the corporate actions it
    is about to remove; ``None`` loads them from the store, exactly as before.

    **Every list handed in is put back into the STORE's order first** (CR-03, 2026-10-04).
    ``build_book`` books one day's trades in list order — ledger-id order, DEF-012 — and an edit
    door builds its would-be list by replacing the row IN PLACE, which leaves it at its OLD
    position. Moving a back-dated buy onto the day of a sell entered before it therefore
    replayed buy-then-sell here and sell-then-buy in the ledger after the write: 200, no
    question, and a sticky 賣超. Sorting by the twins of the store's own ``ORDER BY``
    (``store.*_order_key``) makes the would-be ledger exactly the ledger the next read returns,
    for every door that comes through here — transaction / dividend / opening / corporate-
    action edits and deletes, and the import-batch undo. Every row here is a STORED row with
    its real id (an edit keeps its id), which is what the keys sort on.
    """
    return load_ledger_bundle(
        conn,
        transactions=None if txs is None else sorted(txs, key=transaction_order_key),
        dividends=None if divs is None else sorted(divs, key=dividend_order_key),
        opening=None if opening is None else sorted(opening, key=opening_order_key),
        actions=None if actions is None else sorted(actions, key=corporate_action_order_key),
        instruments=instruments,
    ).without_unregistered()


@dataclass(frozen=True)
class _LedgerRead:
    """Every ledger a replay guard reads, read ONCE per guard call (R10, 2026-10-04).

    :func:`_replay_block` built its "before" and its "after" with two ``load_ledger_bundle``
    calls, so every ledger the door did not override was read twice; the batch undo then read
    the trades and dividends a third time for its cash check, and the corporate-action guard
    read the cash movements twice. One read, handed to every step instead. These are the
    STORED rows in the store's own order — exactly what each step used to read for itself, so
    the answer cannot change (``_to_models`` re-sorts them with the store's order keys, which
    leaves an already-ordered list as it is).
    """

    transactions: list[StoredTransaction]
    dividends: list[StoredDividend]
    opening: list[StoredOpening]
    actions: list[StoredCorporateAction]
    instruments: dict[str, Instrument]


def _read_ledgers(conn: sqlite3.Connection) -> _LedgerRead:
    return _LedgerRead(
        transactions=list_transactions(conn),
        dividends=list_dividends(conn),
        opening=list_opening(conn),
        actions=list_corporate_actions(conn),
        instruments={i.symbol: i for i in list_instruments(conn)},
    )


def _replayed(bundle: LedgerBundle) -> Book | None:
    """The dashboard-path replay of *bundle*, or ``None`` when it cannot be booked at all.

    ``None`` is reached only by a ledger the replay refuses outright — a DRIP / 配股 row with no
    share count, a negative quantity, an uncomputable value. A 賣超 and a dividend with no
    position no longer are: since CR-02 / CR-11 both are recorded on the returned BOOK."""
    try:
        return build_book(bundle, allow_oversell=True)
    except (ValueError, KeyError):
        return None


#: A dividend's ORPHAN identity: (account, symbol, effective date). Exact, not an
#: approximation: whether a position exists when a dividend books depends on nothing else —
#: every event that can open one (opening, trade, corporate action) sorts BEFORE a same-day
#: dividend (``EventPriority``), so two dividends sharing the triple always share the verdict.
_OrphanTriple = tuple[str, str, date]


def _orphan_triples(bundle: LedgerBundle, book: Book | None) -> set[_OrphanTriple]:
    """Every (account, symbol, effective date) a dividend in *bundle* has no position for.

    Read off the REPLAY whenever it ran (CR-02): ``Book.orphan_dividends`` is the replay's own
    answer. The fallback — ``book is None``, a ledger the replay refuses outright — is NOT a
    second definition any more (R9, 2026-10-04): it dated a dividend by its PAYMENT date and
    ignored EXCHANGE / SPINOFF destinations, so it let an edit strand a 配股 booked on its
    ex-date and refused a delete over a position an exchange had created. It now asks
    :meth:`LedgerBundle.position_opened_by`, the rule ``holdings.position_exists_by`` (the
    dividend doors' check) applies in SQL — pinned four ways by
    ``tests/contract/test_r5_r9_orphan_per_dividend.py``.
    """
    if book is not None:
        return {(d.account_id, d.symbol, d.effective_date) for d in book.orphan_dividends}
    return {(d.account_id, d.symbol, d.effective_date) for d in bundle.dividends
            if not bundle.position_opened_by(d.account_id, d.symbol, d.effective_date)}


def _orphan_rows(
    rows: Sequence[StoredDividend], triples: set[_OrphanTriple]
) -> dict[int, StoredDividend]:
    """The stored dividend ROWS (by id) whose orphan identity is in *triples*.

    Rows, not (account, symbol) keys (R5, 2026-10-04): compared per key, a legacy orphan on a
    key MASKED a new one the mutation strands on the same key — a legacy orphan D1 (2330,
    2025-01-01) let the delete of the only buy behind a valid D2 through with 200 and no
    question. By id, a row that was already stranded stays exactly as correctable as before
    (the H3 rule), and every row the mutation newly strands is named. An edit keeps the row's
    id, so editing a legacy orphan in place is not "a new orphan" either.
    """
    return {r.id: r for r in rows
            if (r.account_id, r.symbol,
                dividend_effective_date(r.type, r.date, r.ex_date)) in triples}


def _oversold_shares(book: Book | None) -> dict[tuple[str, str], OversoldPosition] | None:
    """Map of (account, symbol) → the 賣超 position, read off ``Book.oversold_positions``.

    The whole record, not just ``shares``: the guard is DATE-AWARE, so the position's final
    net quantity is not evidence of the problem — it can even be positive, since a later buy
    nets it back up without restoring the discarded basis (F-16). ``oversold_on`` /
    ``oversold_sold`` / ``oversold_held`` carry the day that actually broke.

    CR-01 / CR-11 (2026-10-04): this read ``book.holdings`` and ``build_book`` drops every
    zero-share position — so a 賣超 that a later buy of the same size netted back to exactly 0
    was invisible here, and the row delete / edit, the batch undo and both corporate-action
    doors all answered 200 without asking. The BOOK keeps every one.

    ``None`` when the ledger is un-bookable — the caller then declines to scope the oversell
    rather than block an unrelated correction."""
    if book is None:
        return None
    return {(p.account_id, p.symbol): p for p in book.oversold_positions}


def _oversell_phrase(symbol: str, held: OversoldPosition) -> str:
    """Name the DAY the ledger broke, not the position's final net quantity.

    The date-aware guard exists because a back-dated sell can be uncovered on its own date
    and covered by a later buy (domain-ledger.md, 2026-07-31). Reporting the end-state
    therefore contradicts the finding it is reporting: 「部位將為 9.5 股」 is a positive number
    offered as proof of a shortfall, and it names no day to go and look at.

    Falls back to the old shape when the replay recorded no event — that arm is reachable for
    a position flagged only by ``shares < 0``, and a message with a stale format is better
    than one asserting a date that was never established.
    """
    if held.oversold_on is None or held.oversold_sold is None or held.oversold_held is None:
        return f"{symbol} 部位將為 {decimal_str(held.shares)} 股"
    return (f"{held.oversold_on.isoformat()} 的 {symbol} 賣出 "
            f"{decimal_str(held.oversold_sold)} 股，超過當日持股 "
            f"{decimal_str(held.oversold_held)} 股")


def _replay_block(
    conn: sqlite3.Connection,
    *,
    txs: list[StoredTransaction] | None = None,
    divs: list[StoredDividend] | None = None,
    opening: list[StoredOpening] | None = None,
    actions: list[StoredCorporateAction] | None = None,
    read: _LedgerRead | None = None,
) -> _ReplayBlock | None:
    """Compare the CURRENT ledger to the WOULD-BE ledger; block only what this mutation
    introduces — a newly stranded dividend/opening (orphan, hard) or a new/worsened
    oversell (soft). A pre-existing, unrelated oversell/orphan never poisons the
    correction (audit H3 + H8).

    DEF-049: the block records EVERY stranded position (``orphans`` / ``oversold``), not
    only the first; ``message`` still names the first, so the row doors are unchanged.

    *read* (R10): the stored ledgers, when the caller has read them for another step too;
    ``None`` reads them here — once, for both the "before" and the "after"."""
    base = read if read is not None else _read_ledgers(conn)
    pre = _to_models(conn, base.transactions, base.dividends, base.opening, base.actions,
                     instruments=base.instruments)
    post = _to_models(conn,
                      txs if txs is not None else base.transactions,
                      divs if divs is not None else base.dividends,
                      opening if opening is not None else base.opening,
                      actions if actions is not None else base.actions,
                      instruments=base.instruments)
    pre_book, post_book = _replayed(pre), _replayed(post)

    # ONE source on both sides of the difference: the replay's whenever both ledgers can be
    # booked, the fallback otherwise — never one of each. (Since R9 the two agree, so this is
    # belt and braces: a difference between them must never read as an introduced orphan.)
    both = pre_book is not None and post_book is not None
    pre_rows = base.dividends   # R10: the one read, not a second SELECT
    post_rows = divs if divs is not None else pre_rows
    stranded_now = _orphan_rows(post_rows, _orphan_triples(post, post_book if both else None))
    stranded_before = _orphan_rows(pre_rows, _orphan_triples(pre, pre_book if both else None))
    introduced = [r for i, r in stranded_now.items() if i not in stranded_before]
    if introduced:
        ordered = sorted({(r.account_id, r.symbol) for r in introduced})
        sym = ordered[0][1]
        return _ReplayBlock(
            code="orphan",
            message=(
                f"此更正會使 {sym} 的股利/期初紀錄失去對應持倉，請先處理該紀錄"
            ),
            orphans=ordered,
        )

    post_over = _oversold_shares(post_book)
    pre_over_raw = _oversold_shares(pre_book)
    if post_over is None:
        # The would-be ledger cannot be replayed (beyond the orphan-dividend case above,
        # e.g. a DRIP dividend stripped of its reinvest shares). Block hard when THIS
        # mutation introduced it; a pre-existing un-bookable ledger must not poison an
        # unrelated correction (mirrors the oversell scoping).
        if pre_over_raw is not None:
            return _ReplayBlock(
                code="orphan",
                message="此更正會使帳本無法重建，請檢查相關股利/期初紀錄")
        return None
    pre_over = pre_over_raw or {}
    stranded: list[OversoldPosition] = []
    for key, held in post_over.items():
        prev = pre_over.get(key)
        # Compared on `shares` exactly as before — the SCOPE of the block is unchanged; only
        # the sentence it produces is.
        if prev is None or held.shares < prev.shares:  # newly oversold OR gone more negative
            stranded.append(held)
    if stranded:
        first = stranded[0]
        return _ReplayBlock(code="oversell", message=_oversell_phrase(first.symbol, first),
                            oversold=stranded)
    return None


def _oversell_response(msg: str) -> JSONResponse:
    return JSONResponse(status_code=422, content=error_body(
        "oversell",
        f"此更正將造成賣超（{msg}）— 確認後可強制寫入（儀表板將標示賣超待釐清）"))


def _replay_guard(
    conn: sqlite3.Connection,
    *,
    ack_oversell: bool,
    txs: list[StoredTransaction] | None = None,
    divs: list[StoredDividend] | None = None,
    opening: list[StoredOpening] | None = None,
    actions: list[StoredCorporateAction] | None = None,
) -> JSONResponse | None:
    """Replay the would-be ledger; 422 the caller when THIS mutation strands a record
    (orphan — hard) or introduces/worsens an oversell (soft, ack-bypassable).

    ``actions`` (DEF-049, additive): the would-be corporate-action ledger, for a door that
    removes or rewrites corporate actions; ``None`` replays the stored ones, as before."""
    block = _replay_block(conn, txs=txs, divs=divs, opening=opening, actions=actions)
    if block is None:
        return None
    if block.code == "orphan":
        return JSONResponse(status_code=422, content=error_body(
            "orphan_correction", block.message))
    if not ack_oversell:
        return _oversell_response(block.message)
    return None


# ---------------------------------------------------------------------------
# DEF-049: the import-batch undo — the same guard, over every row the batch removes
# ---------------------------------------------------------------------------

#: The consequence an acknowledged 賣超 has, in the batch undo's own sentence. The ledger
#: tab's dialog names one row the owner just clicked; an undo can strand sells the owner is
#: not looking at, so it says what confirming will do to them.
_BASIS_DISCARDED = "成本基礎會被捨棄（待釐清），儀表板將標示賣超"


@dataclass
class BatchUndoVerdict:
    """The batch undo's guard outcome: a refusal to return, or which acknowledgements the
    undo actually USED (for the action log — an ack sent with nothing to acknowledge is not
    recorded as one)."""

    refusal: JSONResponse | None = None
    code: str | None = None          # the refusal's error code, when refused
    acked_oversell: bool = False
    acked_negative: bool = False


def _stranded_sell(held: OversoldPosition) -> str:
    """One affected sell, account first: a batch can span accounts, a row door cannot."""
    return f"{account_ref(held.account_id)} {_oversell_phrase(held.symbol, held)}"


def _stranded_wire(held: OversoldPosition) -> dict[str, Any]:
    return {
        "kind": "oversell", "account_id": held.account_id, "symbol": held.symbol,
        "date": held.oversold_on.isoformat() if held.oversold_on is not None else None,
        "sold": decimal_str(held.oversold_sold) if held.oversold_sold is not None else None,
        "held": decimal_str(held.oversold_held) if held.oversold_held is not None else None,
        "message": _stranded_sell(held),
    }


def _named_refusal(block: _ReplayBlock, *, what: str, then: str) -> JSONResponse:
    """A multi-row door's sentence for a replay block — every stranded position, by name.

    ``what`` is the mutation as the owner calls it (「此復原」／「此刪除」／「此更正」) and
    ``then`` the step to take once the stranded record is dealt with. The DECISION is
    :func:`_replay_block`'s; only the wording is per door.
    """
    if block.code == "orphan":
        if block.orphans:
            names = "、".join(f"{account_ref(a)} {s}" for a, s in block.orphans)
            msg = (f"{what}會使 {names} 的股利紀錄失去對應持倉"
                   "（沒有任何買進、期初或公司行動能支撐它）"
                   f"，請先刪除或更正該股利紀錄，{then}")
        else:
            msg = (f"{what}會使帳本無法重建（有股利或公司行動會失去對應持倉），"
                   f"請先處理相關紀錄，{then}")
        return JSONResponse(status_code=422, content=error_body("orphan_correction", msg))
    sells = "；".join(_stranded_sell(h) for h in block.oversold)
    which = "這個部位" if len(block.oversold) == 1 else "這些部位"
    return JSONResponse(status_code=422, content=error_body(
        "oversell",
        f"{what}將造成賣超：{sells}。確認後{which}的{_BASIS_DISCARDED}",
        issues=[_stranded_wire(h) for h in block.oversold]))


def _batch_refusal(block: _ReplayBlock) -> JSONResponse:
    """The batch undo's sentence for a replay block."""
    return _named_refusal(block, what="此復原", then="再復原這批匯入")


def _pool_refusal(
    ledgers: CashLedgers,
    *,
    leaving: set[int],
    gone_fx: frozenset[int],
    would_txs: Sequence[StoredTransaction],
    would_divs: Sequence[StoredDividend],
) -> JSONResponse | None:
    """The cash doors' ack-able ``negative_cash`` check, over the pools the leaving cash
    movements (*leaving*) and FX conversions (*gone_fx*) fed — the check
    ``DELETE /api/cash/movements/{id}`` and ``DELETE /api/ledgers/fx/{id}`` run on each of
    those rows (audit C3 / QA-10), made once against the would-be ledger, with the same
    sentence and the same ack. Trades and dividends are not a trigger here, exactly as their
    own row doors are not.

    ⚠ SCOPED like the replay block above — and, since DEF-092 (owner ruling A, 2026-10-02),
    like the single-row cash doors: a pool is refused only when the mutation makes a day
    negative that was not, or pushes a day below the pool's old lowest point, compared DAY BY
    DAY (DEF-090: comparing only the two timelines' lowest points let a new, shallower
    negative stretch through beside an older, deeper one). A pool that is already short for
    reasons that have nothing to do with the mutation — the golden ledger's TWD pool sits at
    −500,000 from its first buy, and a 1,000 deposit undone months later funds nothing — must
    not ask the owner to acknowledge a dip the mutation did not cause (audit H3/H8's rule,
    applied to cash).

    The rule itself is NOT restated here (CR-13): it is ``api/routers/cash.py::_pool_dip`` —
    the one copy every ack-able cash door calls — asked with this mutation's would-be movements,
    conversions, trades and dividends. *ledgers* is the request's one read of the pool ledgers
    (CR-12) and the "before".
    """
    pools = ({(m.account_id, m.ccy) for m in ledgers.movements if m.id in leaving}
             | {(f.account_id, c) for f in ledgers.fx if f.id in gone_fx
                for c in (f.to_ccy, f.from_ccy)})
    if not pools:
        return None
    would_moves = [m for m in ledgers.movements if m.id not in leaving]
    would_fx = [f for f in ledgers.fx if f.id not in gone_fx]
    for account_id, ccy in sorted(pools, key=lambda p: (p[0], p[1].value)):
        dip = _pool_dip(ledgers, account_id, ccy, movements=would_moves, fx=would_fx,
                        transactions=would_txs, dividends=would_divs)
        if dip is not None:
            return _negative_response(account_id, ccy, dip)
    return None


def _batch_cash_refusal(
    conn: sqlite3.Connection,
    rows: BatchRows,
    would_txs: list[StoredTransaction],
    would_divs: list[StoredDividend],
    *,
    read: _LedgerRead,
) -> JSONResponse | None:
    """:func:`_pool_refusal` over the batch's cash / FX rows. A corporate action takes its
    linked fee with it (DEF-020), so that movement leaves the would-be pool too.

    A batch that owns no cash movement, conversion or corporate action can feed no pool, so it
    reads nothing (CR-12); otherwise it reads the two cash ledgers, once, and takes the trades,
    dividends and registry from *read* — the guard's own read (R10)."""
    action_ids = rows.of("corporate_actions")
    if not (rows.of("cash_movements") or rows.of("fx_conversions") or action_ids):
        return None
    ledgers = load_cash_ledgers(conn, transactions=read.transactions, dividends=read.dividends,
                                instruments=read.instruments)
    leaving = {m.id for m in ledgers.movements
               if m.id in rows.of("cash_movements")
               or (m.corporate_action_id is not None and m.corporate_action_id in action_ids)}
    return _pool_refusal(ledgers, leaving=leaving, gone_fx=rows.of("fx_conversions"),
                         would_txs=would_txs, would_divs=would_divs)


def batch_undo_guard(
    conn: sqlite3.Connection, rows: BatchRows, *, ack_oversell: bool, ack_negative: bool,
) -> BatchUndoVerdict:
    """Judge one import-batch undo BEFORE anything is deleted (DEF-049).

    The would-be ledger is the current one minus EVERY row the batch still owns — trades,
    dividends, FX, cash AND its corporate actions — replayed through the SAME
    :func:`_replay_block` every ledger-tab delete runs:

    * a newly stranded dividend → 422 ``orphan_correction`` (hard, no ack);
    * a new or worsened 賣超 → 422 ``oversell`` naming EACH affected sell (account, date,
      symbol, shares sold vs held) and the consequence, until ``ack_oversell``;
    * a pre-existing, unrelated 賣超 never blocks (the pre/post comparison).

    Then the cash rows' own guard: a pool the batch's cash / FX rows funded dipping below
    zero → 422 ``negative_cash`` until ``ack_negative`` — what the per-row cash and FX delete
    doors answer for each of those rows.
    """
    read = _read_ledgers(conn)   # R10: the replay and the cash check share ONE read
    would_txs = [t for t in read.transactions if t.id not in rows.of("transactions")]
    would_divs = [d for d in read.dividends if d.id not in rows.of("dividends")]
    would_acts = [a for a in read.actions if a.id not in rows.of("corporate_actions")]
    verdict = BatchUndoVerdict()
    block = _replay_block(conn, txs=would_txs, divs=would_divs, actions=would_acts, read=read)
    if block is not None:
        if block.code == "orphan" or not ack_oversell:
            return BatchUndoVerdict(
                refusal=_batch_refusal(block),
                code="orphan_correction" if block.code == "orphan" else "oversell")
        verdict.acked_oversell = True
    negative = _batch_cash_refusal(conn, rows, would_txs, would_divs, read=read)
    if negative is not None:
        if not ack_negative:
            return BatchUndoVerdict(refusal=negative, code="negative_cash")
        verdict.acked_negative = True
    return verdict


# ---------------------------------------------------------------------------
# DEF-049 (class fix): the corporate-action ledger's delete and edit doors
# ---------------------------------------------------------------------------


def action_change_guard(
    conn: sqlite3.Connection,
    *,
    would_be: list[StoredCorporateAction],
    leaving: Sequence[StoredCorporateAction] = (),
    ack_oversell: bool,
    ack_negative: bool = False,
    what: str,
    then: str,
) -> JSONResponse | None:
    """Judge a corporate-action DELETE or EDIT before anything is written (DEF-049).

    Same class as the batch undo: ``DELETE /api/ledgers/corporate-actions/{id}`` and
    ``…/set`` went straight to ``_delete_actions``, and the PUT straight to
    ``update_corporate_action`` — none of them replayed the would-be ledger. Deleting a 10:1
    SPLIT under a later sell of 5,000 answered 200 and left the position at −4,000, 賣超,
    basis discarded (measured 2026-09-25); editing the ratio or moving the date past the sell
    did the same. Every other ledger door had asked first.

    *would_be* is the whole corporate-action ledger as it WOULD be (the deleted rows gone /
    the edited row replaced), replayed through the SAME :func:`_replay_block`: a newly
    stranded dividend → 422 ``orphan_correction`` (hard); a new or worsened 賣超 → 422
    ``oversell`` naming each affected sell, until ``ack_oversell``; a pre-existing unrelated
    one never blocks.

    *leaving* (deletes only): the rows being removed. A corporate action takes its linked
    reorganisation fee with it (DEF-020), so those movements go through the batch undo's own
    scoped ``negative_cash`` check (:func:`_pool_refusal`) — today the linked fee is always a
    WITHDRAW, whose removal can only raise a pool, so the check cannot fire; it is run anyway
    so a future credit-type link is not the first unguarded one.
    """
    read = _read_ledgers(conn)   # R10: the replay and the fee's pool check share ONE read
    block = _replay_block(conn, actions=would_be, read=read)
    if block is not None and (block.code == "orphan" or not ack_oversell):
        return _named_refusal(block, what=what, then=then)
    if leaving and not ack_negative:
        ids = {a.id for a in leaving}
        movements = list_cash_movements(conn)
        fees = {m.id for m in movements
                if m.corporate_action_id is not None and m.corporate_action_id in ids}
        if fees:
            ledgers = load_cash_ledgers(conn, movements=movements,
                                        transactions=read.transactions,
                                        dividends=read.dividends,
                                        instruments=read.instruments)
            return _pool_refusal(ledgers, leaving=fees, gone_fx=frozenset(),
                                 would_txs=ledgers.transactions,
                                 would_divs=ledgers.dividends)
    return None
