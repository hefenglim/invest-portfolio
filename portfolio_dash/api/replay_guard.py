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
from typing import Any

from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from portfolio_dash.api.errors import error_body

# The cash doors' own ``negative_cash`` sentence (a sibling router, same layer — the shape
# ``ledgers.py`` already uses for ``fx_delete_guard``), so the batch undo cannot word the
# overdraft differently from the 刪除 button beside each of its rows.
from portfolio_dash.api.routers.cash import _negative_response
from portfolio_dash.data_ingestion.provenance import BatchRows
from portfolio_dash.data_ingestion.store import (
    StoredCorporateAction,
    StoredDividend,
    StoredOpening,
    StoredTransaction,
    list_cash_movements,
    list_corporate_actions,
    list_dividends,
    list_fx_conversions,
    list_instruments,
    list_transactions,
    load_ledger_bundle,
)
from portfolio_dash.portfolio.cash import pool_lines, running_low
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.portfolio.results import Holding
from portfolio_dash.shared.account_ref import account_ref
from portfolio_dash.shared.models.ledger import LedgerBundle
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
    oversold: list[Holding] = Field(default_factory=list)


def _to_models(
    conn: sqlite3.Connection,
    txs: list[StoredTransaction] | None = None,
    divs: list[StoredDividend] | None = None,
    opening: list[StoredOpening] | None = None,
    actions: list[StoredCorporateAction] | None = None,
) -> LedgerBundle:
    """The replay bundle for the mutated list(s); unspecified ledgers load from store.

    Rows whose symbol is unregistered are excluded (same degradation as the dashboard)
    so one legacy bad row cannot block corrections to healthy rows.

    ``actions`` (DEF-049) lets a batch undo replay the ledger WITHOUT the corporate actions it
    is about to remove; ``None`` loads them from the store, exactly as before.
    """
    return load_ledger_bundle(
        conn, transactions=txs, dividends=divs, opening=opening, actions=actions
    ).without_unregistered()


def _orphan_keys(bundle: LedgerBundle) -> set[tuple[str, str]]:
    """(account, symbol) dividend keys with NO buy/sell/opening on-or-before the div date.

    These are exactly the rows on which ``build_book`` raises ``ValueError`` ('dividend
    for unknown position') — computing the set directly (rather than catching) lets the
    caller scope the block to orphans the mutation INTRODUCES (audit H3)."""
    orphans: set[tuple[str, str]] = set()
    for dv in bundle.dividends:
        covered = any(
            o.account_id == dv.account_id and o.symbol == dv.symbol
            and o.build_date <= dv.date for o in bundle.opening
        ) or any(
            t.account_id == dv.account_id and t.symbol == dv.symbol
            and t.trade_date <= dv.date for t in bundle.transactions
        )
        if not covered:
            orphans.add((dv.account_id, dv.symbol))
    return orphans


def _oversold_shares(bundle: LedgerBundle) -> dict[tuple[str, str], Holding] | None:
    """Map of (account, symbol) → the oversold Holding.

    The whole Holding, not just ``shares``: the guard is DATE-AWARE, so the position's final
    net quantity is not evidence of the problem — it can even be positive, since a later buy
    nets it back up without restoring the discarded basis (F-16). ``oversold_on`` /
    ``oversold_sold`` / ``oversold_held`` carry the day that actually broke.

    ``None`` when the ledger is un-bookable (e.g. a pre-existing orphan) — the caller
    then declines to scope the oversell rather than block an unrelated correction."""
    try:
        book = build_book(bundle, allow_oversell=True)
    except (ValueError, KeyError):
        return None
    return {(h.account_id, h.symbol): h for h in book.holdings if h.oversold}


def _oversell_phrase(symbol: str, held: Holding) -> str:
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
) -> _ReplayBlock | None:
    """Compare the CURRENT ledger to the WOULD-BE ledger; block only what this mutation
    introduces — a newly stranded dividend/opening (orphan, hard) or a new/worsened
    oversell (soft). A pre-existing, unrelated oversell/orphan never poisons the
    correction (audit H3 + H8).

    DEF-049: the block records EVERY stranded position (``orphans`` / ``oversold``), not
    only the first; ``message`` still names the first, so the row doors are unchanged."""
    pre = _to_models(conn)
    post = _to_models(conn, txs, divs, opening, actions)

    introduced_orphans = _orphan_keys(post) - _orphan_keys(pre)
    if introduced_orphans:
        ordered = sorted(introduced_orphans)
        sym = ordered[0][1]
        return _ReplayBlock(
            code="orphan",
            message=(
                f"此更正會使 {sym} 的股利/期初紀錄失去對應持倉，請先處理該紀錄"
            ),
            orphans=ordered,
        )

    post_over = _oversold_shares(post)
    pre_over_raw = _oversold_shares(pre)
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
    stranded: list[Holding] = []
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


def _stranded_sell(held: Holding) -> str:
    """One affected sell, account first: a batch can span accounts, a row door cannot."""
    return f"{account_ref(held.account_id)} {_oversell_phrase(held.symbol, held)}"


def _stranded_wire(held: Holding) -> dict[str, Any]:
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
            msg = (f"{what}會使 {names} 的股利紀錄失去對應持倉（沒有任何買進或期初能支撐它）"
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
    conn: sqlite3.Connection,
    *,
    leaving: set[int],
    gone_fx: frozenset[int],
    would_txs: list[StoredTransaction],
    would_divs: list[StoredDividend],
) -> JSONResponse | None:
    """The cash doors' ack-able ``negative_cash`` check, over the pools the leaving cash
    movements (*leaving*) and FX conversions (*gone_fx*) fed — the check
    ``DELETE /api/cash/movements/{id}`` and ``DELETE /api/ledgers/fx/{id}`` run on each of
    those rows (audit C3 / QA-10), made once against the would-be ledger, with the same
    sentence and the same ack. Trades and dividends are not a trigger here, exactly as their
    own row doors are not.

    ⚠ SCOPED like the replay block above, which the row doors' cash check is not: a pool is
    refused only when the mutation makes its running low WORSE (post low < 0 and below the
    current low). A pool that is already short for reasons that have nothing to do with the
    mutation — the golden ledger's TWD pool sits at −500,000 from its first buy, and a 1,000
    deposit undone months later funds nothing — must not ask the owner to acknowledge a dip
    the mutation did not cause (audit H3/H8's rule, applied to cash).
    """
    movements = list_cash_movements(conn)
    fxs = list_fx_conversions(conn)
    pools = ({(m.account_id, m.ccy) for m in movements if m.id in leaving}
             | {(f.account_id, c) for f in fxs if f.id in gone_fx
                for c in (f.to_ccy, f.from_ccy)})
    if not pools:
        return None
    would_moves = [m for m in movements if m.id not in leaving]
    would_fx = [f for f in fxs if f.id not in gone_fx]
    all_txs = list_transactions(conn)
    all_divs = list_dividends(conn)
    insts = {i.symbol: i for i in list_instruments(conn)}
    for account_id, ccy in sorted(pools, key=lambda p: (p[0], p[1].value)):
        low, on = running_low(pool_lines(
            account_id, ccy, would_moves, would_fx, would_txs, would_divs, insts))
        if low >= 0:
            continue
        pre_low, _ = running_low(pool_lines(
            account_id, ccy, movements, fxs, all_txs, all_divs, insts))
        if low < pre_low:
            return _negative_response(account_id, ccy, low, on)
    return None


def _batch_cash_refusal(
    conn: sqlite3.Connection,
    rows: BatchRows,
    would_txs: list[StoredTransaction],
    would_divs: list[StoredDividend],
) -> JSONResponse | None:
    """:func:`_pool_refusal` over the batch's cash / FX rows. A corporate action takes its
    linked fee with it (DEF-020), so that movement leaves the would-be pool too."""
    action_ids = rows.of("corporate_actions")
    leaving = {m.id for m in list_cash_movements(conn)
               if m.id in rows.of("cash_movements")
               or (m.corporate_action_id is not None and m.corporate_action_id in action_ids)}
    return _pool_refusal(conn, leaving=leaving, gone_fx=rows.of("fx_conversions"),
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
    would_txs = [t for t in list_transactions(conn) if t.id not in rows.of("transactions")]
    would_divs = [d for d in list_dividends(conn) if d.id not in rows.of("dividends")]
    would_acts = [a for a in list_corporate_actions(conn)
                  if a.id not in rows.of("corporate_actions")]
    verdict = BatchUndoVerdict()
    block = _replay_block(conn, txs=would_txs, divs=would_divs, actions=would_acts)
    if block is not None:
        if block.code == "orphan" or not ack_oversell:
            return BatchUndoVerdict(
                refusal=_batch_refusal(block),
                code="orphan_correction" if block.code == "orphan" else "oversell")
        verdict.acked_oversell = True
    negative = _batch_cash_refusal(conn, rows, would_txs, would_divs)
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
    block = _replay_block(conn, actions=would_be)
    if block is not None and (block.code == "orphan" or not ack_oversell):
        return _named_refusal(block, what=what, then=then)
    if leaving and not ack_negative:
        ids = {a.id for a in leaving}
        fees = {m.id for m in list_cash_movements(conn)
                if m.corporate_action_id is not None and m.corporate_action_id in ids}
        if fees:
            return _pool_refusal(conn, leaving=fees, gone_fx=frozenset(),
                                 would_txs=list_transactions(conn),
                                 would_divs=list_dividends(conn))
    return None
