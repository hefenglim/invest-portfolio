"""Five ledgers: reads (spec 11) + explicit row corrections (edit/delete, 2026-07-02).

Reads are thin over store.list_*. Corrections stay within the "append-only in
spirit" rule: they are EXPLICIT user actions via PUT/DELETE (never silent
mutation), validated by replaying the WOULD-BE ledger through build_book before
anything is written — an edit/delete that would strand a later sell (oversell)
is refused with 422 unless the user explicitly acks it (mirroring manual entry;
the dashboard degrades an acked oversold book to a flagged 賣超 holding).

Side/DividendType serialize lowercase (SR #1); Currency stays uppercase. The `total`
sign + `implied_rate` are presentation-level derived fields over stored ledger values.

**The 5th ledger — corporate actions (W7, spec §6.7 door 3).** It is deliberately here and
not on the input page: the input page is high-frequency capture, the ledger page is
low-frequency corrective record-keeping. Three things distinguish it from the other four
and all three are audit findings, not preferences:

* **Every write goes through ``validate_corporate_action`` with the FULL batch** (F-40).
  Before W7 that function had zero production callers, so every §5 rejection existed only
  in tests. E12/E13 are batch-level rules; a per-row call rejects a correct multi-account
  entry and accepts a partial one.
* **Delete and update RE-VALIDATE** through ``validate_corporate_action_change`` (F-32).
  ``split_factor``'s dedup key is ``(symbol, date, ratio)`` with no account, so removing
  one row of an N-account set leaves the GLOBAL price correction standing while that
  account's share count goes uncorrected — and the drawer footer prints ✓ 對帳一致 over it.
* **Every SPLIT write runs the price reconcile** (§5.1(c)) on the same connection, after
  the CRUD commits — both ends on a symbol-change edit.
"""

import sqlite3
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from portfolio_dash.api.deps import get_conn, get_now
from portfolio_dash.api.errors import error_body
from portfolio_dash.api.instrument_service import reconcile_price_basis
from portfolio_dash.api.wire import issue_wire, parse_side
from portfolio_dash.data_ingestion.config_seed import get_fee_rule_set
from portfolio_dash.data_ingestion.dividend_model import check_amounts
from portfolio_dash.data_ingestion.fees import FeeComputationError, compute_fees
from portfolio_dash.data_ingestion.fx_lookup import resolve_stamp_fx
from portfolio_dash.data_ingestion.holdings import load_action_index, shares_through
from portfolio_dash.data_ingestion.markets import MARKET_ZH, account_market
from portfolio_dash.data_ingestion.register import (
    autoregister_spinoff_child,
    spinoff_child_draft,
)
from portfolio_dash.data_ingestion.rules_binding import allowed_markets, fee_rule_for
from portfolio_dash.data_ingestion.store import (
    MovedBand,
    StoredCorporateAction,
    StoredDividend,
    StoredOpening,
    StoredTransaction,
    delete_corporate_action,
    delete_dividend,
    delete_fx_conversion,
    delete_opening,
    delete_transaction,
    get_corporate_action,
    get_dividend,
    get_fx_conversion,
    get_instrument,
    get_opening,
    get_transaction,
    insert_corporate_action,
    list_accounts,
    list_corporate_actions,
    list_dividends,
    list_fx_conversions,
    list_instruments,
    list_opening,
    list_transactions,
    load_ledger_bundle,
    move_target_band,
    pending_band_move,
    update_corporate_action,
    update_dividend,
    update_fx_conversion,
    update_transaction,
    upsert_opening,
)
from portfolio_dash.data_ingestion.validate import (
    IDENTIFIER_CHANGE_SUSPECTED,
    TARGET_BAND_PREDATES_SPLIT,
    CorporateActionInput,
    Issue,
    identifier_change_repair,
    restated_band,
    validate_corporate_action,
    validate_corporate_action_change,
    validate_opening_cost,
)
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.portfolio.results import Book, Holding
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.corporate_actions import ActionIndex, CorporateActionKind
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import LedgerBundle
from portfolio_dash.shared.wire import decimal_str

router = APIRouter()

_ZERO = Decimal("0")


def _names(conn: sqlite3.Connection) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    accts = {a.account_id: a.name for a in list_accounts(conn)}
    insts = list_instruments(conn)
    names = {i.symbol: i.name for i in insts}
    ccys = {i.symbol: i.quote_ccy.value for i in insts}
    return accts, names, ccys


def _page(rows: list[dict[str, Any]], limit: int, offset: int) -> dict[str, Any]:
    desc = list(reversed(rows))  # rows arrive ASC; present desc by recency
    return {"rows": desc[offset:offset + limit], "total_count": len(desc)}


def _check_dates(frm: str | None, to: str | None) -> JSONResponse | None:
    if frm and to and frm > to:
        return JSONResponse(status_code=400,
                            content=error_body("validation_error", "日期區間無效", field="from"))
    return None


def _in_range(d: date, frm: str | None, to: str | None) -> bool:
    if frm and d.isoformat() < frm:
        return False
    if to and d.isoformat() > to:
        return False
    return True


@router.get("/ledgers/transactions")
def transactions(
    account_id: str | None = None, symbol: str | None = None,
    frm: str | None = Query(None, alias="from"), to: str | None = None,
    limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    bad = _check_dates(frm, to)
    if bad is not None:
        return bad
    accts, names, ccys = _names(conn)
    out: list[dict[str, Any]] = []
    for t in list_transactions(conn, account_id=account_id, symbol=symbol):
        if not _in_range(t.trade_date, frm, to):
            continue
        gross = t.quantity * t.price
        total = -(gross + t.fees + t.tax) if t.side.value == "BUY" else (gross - t.fees - t.tax)
        out.append({
            "id": t.id, "date": t.trade_date.isoformat(), "account_id": t.account_id,
            "account": accts.get(t.account_id, t.account_id), "symbol": t.symbol,
            "name": names.get(t.symbol, ""), "side": t.side.value.lower(),
            "shares": decimal_str(t.quantity), "price": decimal_str(t.price),
            "fee": decimal_str(t.fees), "tax": decimal_str(t.tax),
            "total": decimal_str(total), "ccy": ccys.get(t.symbol, ""),
            "fee_snapshot": (t.fee_rule_snapshot or None), "note": t.note,
            # short_sale changes how the replay books this row, so the public read
            # surface must carry it — otherwise the ledger cannot be rebuilt from the
            # ledger (domain-ledger.md) and the trades page cannot tell a declared
            # short from an ordinary sell.
            "short_sale": t.short_sale,
        })
    return _page(out, limit, offset)


@router.get("/ledgers/dividends")
def dividends(
    account_id: str | None = None, symbol: str | None = None,
    frm: str | None = Query(None, alias="from"), to: str | None = None,
    limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    bad = _check_dates(frm, to)
    if bad is not None:
        return bad
    accts, names, ccys = _names(conn)
    out: list[dict[str, Any]] = []
    for d in list_dividends(conn, account_id=account_id, symbol=symbol):
        if not _in_range(d.date, frm, to):
            continue
        out.append({
            "id": d.id, "date": d.date.isoformat(), "account_id": d.account_id,
            "account": accts.get(d.account_id, d.account_id), "symbol": d.symbol,
            "name": names.get(d.symbol, ""), "type": d.type.lower(),
            "gross": decimal_str(d.gross), "withhold": decimal_str(d.withholding),
            "net": decimal_str(d.net),
            "reinvest_shares": (
                decimal_str(d.reinvest_shares) if d.reinvest_shares is not None else None
            ),
            "reinvest_price": (
                decimal_str(d.reinvest_price) if d.reinvest_price is not None else None
            ),
            "ccy": ccys.get(d.symbol, ""),
        })
    return _page(out, limit, offset)


@router.get("/ledgers/fx")
def fx(
    account_id: str | None = None,
    frm: str | None = Query(None, alias="from"), to: str | None = None,
    limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    bad = _check_dates(frm, to)
    if bad is not None:
        return bad
    accts, _names_map, _ccys = _names(conn)
    out: list[dict[str, Any]] = []
    for c in list_fx_conversions(conn, account_id=account_id):
        if not _in_range(c.date, frm, to):
            continue
        out.append({
            "id": c.id, "date": c.date.isoformat(), "account_id": c.account_id,
            "account": accts.get(c.account_id, c.account_id),
            "from_ccy": c.from_ccy.value, "from_amt": decimal_str(c.from_amount),
            "to_ccy": c.to_ccy.value, "to_amt": decimal_str(c.to_amount),
            "implied_rate": decimal_str(c.implied_rate),
        })
    return _page(out, limit, offset)


@router.get("/ledgers/openings")
def openings(
    account_id: str | None = None, symbol: str | None = None,
    limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    accts, names, ccys = _names(conn)
    out: list[dict[str, Any]] = []
    for o in list_opening(conn, account_id=account_id):
        if symbol is not None and o.symbol != symbol:
            continue
        out.append({
            "date": o.build_date.isoformat(), "account_id": o.account_id,
            "account": accts.get(o.account_id, o.account_id), "symbol": o.symbol,
            "name": names.get(o.symbol, ""), "shares": decimal_str(o.shares),
            "avg": decimal_str(o.original_avg),  # computed on read (total / shares) — A6
            "total": decimal_str(o.original_cost_total),
            "ccy": ccys.get(o.symbol, ""),
        })
    paged = _page(out, limit, offset)
    for i, row in enumerate(paged["rows"], start=1):
        row["id"] = i  # openings has no DB id; synthetic 1-based display key
    return paged


# ---------------------------------------------------------------------------
# Row corrections: edit / delete (2026-07-02)
# ---------------------------------------------------------------------------


class _ReplayBlock(BaseModel):
    """A reason a correction is refused: an ``oversell`` (ack-bypassable) or an
    ``orphan`` (a dividend/opening record stranded by the mutation — hard)."""

    code: str  # "oversell" | "orphan"
    message: str


def _to_models(
    conn: sqlite3.Connection,
    txs: list[StoredTransaction] | None = None,
    divs: list[StoredDividend] | None = None,
    opening: list[StoredOpening] | None = None,
) -> LedgerBundle:
    """The replay bundle for the mutated list(s); unspecified ledgers load from store.

    Rows whose symbol is unregistered are excluded (same degradation as the dashboard)
    so one legacy bad row cannot block corrections to healthy rows.
    """
    return load_ledger_bundle(
        conn, transactions=txs, dividends=divs, opening=opening
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


def _oversold_shares(bundle: LedgerBundle) -> dict[tuple[str, str], Decimal] | None:
    """Map of (account, symbol) → negative shares for oversold positions.

    ``None`` when the ledger is un-bookable (e.g. a pre-existing orphan) — the caller
    then declines to scope the oversell rather than block an unrelated correction."""
    try:
        book = build_book(bundle, allow_oversell=True)
    except (ValueError, KeyError):
        return None
    return {(h.account_id, h.symbol): h.shares for h in book.holdings if h.oversold}


def _replay_block(
    conn: sqlite3.Connection,
    *,
    txs: list[StoredTransaction] | None = None,
    divs: list[StoredDividend] | None = None,
    opening: list[StoredOpening] | None = None,
) -> _ReplayBlock | None:
    """Compare the CURRENT ledger to the WOULD-BE ledger; block only what this mutation
    introduces — a newly stranded dividend/opening (orphan, hard) or a new/worsened
    oversell (soft). A pre-existing, unrelated oversell/orphan never poisons the
    correction (audit H3 + H8)."""
    pre = _to_models(conn)
    post = _to_models(conn, txs, divs, opening)

    introduced_orphans = _orphan_keys(post) - _orphan_keys(pre)
    if introduced_orphans:
        sym = sorted(introduced_orphans)[0][1]
        return _ReplayBlock(
            code="orphan",
            message=(
                f"此更正會使 {sym} 的股利/期初紀錄失去對應持倉,請先處理該紀錄"
            ),
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
    for key, shares in post_over.items():
        prev = pre_over.get(key)
        if prev is None or shares < prev:  # newly oversold OR gone more negative
            return _ReplayBlock(
                code="oversell",
                message=f"{key[1]} 部位將為 {decimal_str(shares)} 股",
            )
    return None


def _account_exists(conn: sqlite3.Connection, account_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM accounts WHERE account_id=?", (account_id,)
    ).fetchone() is not None


def _mutation_guard(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    symbol: str | None,
    prev_account_id: str | None = None,
    prev_symbol: str | None = None,
) -> JSONResponse | None:
    """Shared field checks for row corrections: account known, symbol registered, and
    account↔instrument market coherence (audit H1).

    The coherence branch is applied ONLY when the edit re-keys the row — i.e. changes
    ``account_id`` or ``symbol`` vs the stored ``prev_*`` (audit LOW-3). A legacy
    incoherent row (e.g. a US stock booked in a TWD account before H1 existed) stays
    editable in place — fixing its amount/shares must not be blocked by a coherence
    check on a key the user is not changing; moving/re-keying still enforces coherence.
    When ``prev_*`` are omitted (a fresh mutation, or the FX path with ``symbol=None``),
    coherence is enforced as before. The account-exists + symbol-registered checks are
    always unconditional."""
    if not _account_exists(conn, account_id):
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", f"帳戶 {account_id} 不存在", field="account_id"))
    if symbol is not None:
        inst = get_instrument(conn, symbol)
        if inst is None:
            return JSONResponse(status_code=400, content=error_body(
                "validation_error",
                f"未註冊標的 {symbol} — 請先至「標的管理」註冊", field="symbol"))
        rekeyed = account_id != prev_account_id or symbol != prev_symbol
        if rekeyed:
            # Batch B: coherence relaxed to allowed-market SET membership (a merged Moomoo
            # account holds US + MY). ``acct_mkt`` (settlement-derived) stays the None-guard
            # + message label; for a single-market account the allowed set is that singleton,
            # so this is behavior-identical and the rejection message is byte-identical.
            acct_mkt = account_market(conn, account_id)
            if acct_mkt is not None and inst.market not in allowed_markets(conn, account_id):
                return JSONResponse(status_code=400, content=error_body(
                    "validation_error",
                    f"{symbol} 屬 {inst.market.value} 市場,"
                    f"不可登錄於 {MARKET_ZH.get(acct_mkt, acct_mkt.value)}帳戶",
                    field="symbol"))
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
) -> JSONResponse | None:
    """Replay the would-be ledger; 422 the caller when THIS mutation strands a record
    (orphan — hard) or introduces/worsens an oversell (soft, ack-bypassable)."""
    block = _replay_block(conn, txs=txs, divs=divs, opening=opening)
    if block is None:
        return None
    if block.code == "orphan":
        return JSONResponse(status_code=422, content=error_body(
            "orphan_correction", block.message))
    if not ack_oversell:
        return _oversell_response(block.message)
    return None


class TxEditBody(BaseModel):
    account_id: str
    symbol: str
    side: str
    date: date
    # shares/price bounded (audit M4) so an overflow-sized edit 400s before the fee
    # quantize can 500. fee/tax constrained >= 0 (audit H2).
    shares: Decimal = Field(le=Decimal("1e12"))
    price: Decimal = Field(le=Decimal("1e12"))
    fee: Decimal = Field(ge=0)
    tax: Decimal = Field(ge=0)
    note: str | None = None
    ack_oversell: bool = False
    # audit M6: whether the user explicitly edited fee/tax in the modal. When a core
    # field (account/side/qty/price/date) changes and these are False, the backend
    # recomputes fee/tax from the NEW account's rule set + regenerates the snapshot.
    fee_overridden: bool = False
    tax_overridden: bool = False
    # audit MED-1: same-day round-trip flag, persisted on the row so an edit-recompute
    # reproduces the TW sell-side day-trade tax rate. None = preserve the stored value
    # (the wire never carries daytrade this round; preservation via None is the contract).
    daytrade: bool | None = None


def _recompute_edit_fees(
    conn: sqlite3.Connection,
    body: TxEditBody,
    existing: StoredTransaction,
    daytrade: bool,
) -> tuple[Decimal, Decimal, dict[str, str] | None] | JSONResponse:
    """Resolve the fee/tax + snapshot to persist for a transaction edit (audit M6).

    Recomputes from the new account's rule set when a core field changed and the user
    did not explicitly edit fee/tax; explicit edits are honored as overrides (snapshot
    tagged ``override: true``). Returns a 400 JSONResponse on an overflow-sized notional.

    ``daytrade`` is the effective flag (preserved-or-changed); a change to it is a core
    change (it governs the TW sell-side tax rate) and it is fed into ``compute_fees`` so a
    recompute reproduces the day-trade rate instead of silently reverting to 現股 (MED-1).
    """
    side = parse_side(body.side)
    core_changed = (
        existing.account_id != body.account_id
        or existing.symbol != body.symbol
        or existing.side is not side
        or existing.quantity != body.shares
        or existing.price != body.price
        or existing.trade_date != body.date
        or existing.daytrade != daytrade
    )
    fee, tax = body.fee, body.tax
    snapshot: dict[str, str] | None = None
    recompute = core_changed and not (body.fee_overridden and body.tax_overridden)
    if recompute:
        inst = get_instrument(conn, body.symbol)
        if inst is not None:
            # Batch B: (account, market)-bound fee rule set; single-market -> account scalar.
            fee_rule_set: str | None = fee_rule_for(conn, body.account_id, inst.market)
        else:
            # Degradation: no instrument -> no market to bind; fall back to the account scalar.
            scalar = conn.execute(
                "SELECT fee_rule_set FROM accounts WHERE account_id=?", (body.account_id,)
            ).fetchone()
            fee_rule_set = scalar["fee_rule_set"] if scalar is not None else None
        if fee_rule_set is not None:
            rules = get_fee_rule_set(fee_rule_set, conn)
            # FE-D2: resolve the trade-date USD/MYR rate for the Moomoo US MY stamp. No rate
            # -> stamp 0 (recorded in the snapshot); the edit path has no soft-issue surface.
            stamp_fx = resolve_stamp_fx(conn, body.date) if rules.has_us_stamp else None
            try:
                fr = compute_fees(
                    rules, side, body.shares, body.price,
                    is_etf=inst.is_etf if inst is not None else False,
                    daytrade=daytrade, stamp_fx=stamp_fx,
                )
            except FeeComputationError as exc:
                return JSONResponse(status_code=400, content=error_body(
                    "validation_error", str(exc), field="shares"))
            snapshot = dict(fr.snapshot)
            if not body.fee_overridden:
                fee = fr.fee
            if not body.tax_overridden:
                tax = fr.tax
    if body.fee_overridden or body.tax_overridden:
        base = snapshot if snapshot is not None else dict(existing.fee_rule_snapshot or {})
        base["override"] = "true"
        snapshot = base
    return fee, tax, snapshot


@router.put("/ledgers/transactions/{txn_id}")
def edit_transaction(
    txn_id: int,
    body: TxEditBody,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    existing = get_transaction(conn, txn_id)
    if existing is None:
        return JSONResponse(status_code=404,
                            content=error_body("not_found", f"交易 #{txn_id} 不存在"))
    guard = _mutation_guard(
        conn, account_id=body.account_id, symbol=body.symbol,
        prev_account_id=existing.account_id, prev_symbol=existing.symbol)
    if guard is not None:
        return guard
    if body.shares <= 0 or body.price <= 0:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", "股數與價格必須大於 0", field="shares"))
    # None on the wire = preserve the stored daytrade flag (MED-1: the wire never carries it).
    effective_daytrade = body.daytrade if body.daytrade is not None else existing.daytrade
    resolved = _recompute_edit_fees(conn, body, existing, effective_daytrade)
    if isinstance(resolved, JSONResponse):
        return resolved
    fee, tax, snapshot = resolved
    edited = existing.model_copy(update={
        "account_id": body.account_id, "symbol": body.symbol,
        "side": parse_side(body.side), "quantity": body.shares, "price": body.price,
        "fees": fee, "tax": tax, "trade_date": body.date, "note": body.note,
        "daytrade": effective_daytrade,
    })
    would_be = [edited if t.id == txn_id else t for t in list_transactions(conn)]
    blocked = _replay_guard(conn, ack_oversell=body.ack_oversell, txs=would_be)
    if blocked is not None:
        return blocked
    update_transaction(
        conn, txn_id, account_id=body.account_id, symbol=body.symbol,
        side=parse_side(body.side), quantity=body.shares, price=body.price,
        fees=fee, tax=tax, trade_date=body.date, daytrade=effective_daytrade,
        note=body.note, fee_rule_snapshot=snapshot,
    )
    return {"ok": True, "id": txn_id, "fee": decimal_str(fee), "tax": decimal_str(tax)}


@router.delete("/ledgers/transactions/{txn_id}")
def remove_transaction(
    txn_id: int,
    ack_oversell: bool = False,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    if get_transaction(conn, txn_id) is None:
        return JSONResponse(status_code=404,
                            content=error_body("not_found", f"交易 #{txn_id} 不存在"))
    would_be = [t for t in list_transactions(conn) if t.id != txn_id]
    blocked = _replay_guard(conn, ack_oversell=ack_oversell, txs=would_be)
    if blocked is not None:
        return blocked
    delete_transaction(conn, txn_id)
    return {"ok": True, "id": txn_id}


_DIV_TYPES = {t.value for t in DividendType}


class DivEditBody(BaseModel):
    account_id: str
    symbol: str
    date: date
    type: str
    gross: Decimal
    withhold: Decimal
    net: Decimal
    reinvest_shares: Decimal | None = None
    reinvest_price: Decimal | None = None
    ack_oversell: bool = False


@router.put("/ledgers/dividends/{div_id}")
def edit_dividend(
    div_id: int,
    body: DivEditBody,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    existing = get_dividend(conn, div_id)
    if existing is None:
        return JSONResponse(status_code=404,
                            content=error_body("not_found", f"股利 #{div_id} 不存在"))
    guard = _mutation_guard(
        conn, account_id=body.account_id, symbol=body.symbol,
        prev_account_id=existing.account_id, prev_symbol=existing.symbol)
    if guard is not None:
        return guard
    div_type = body.type.strip().upper()
    if div_type not in _DIV_TYPES:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", f"未知股利類型 {body.type}", field="type"))
    # The SAME conservation gate the CSV/manual import path applies (audit M5): this endpoint
    # used to check only "not negative" and then store gross/withhold/net verbatim, so an edit
    # could leave a row where 預扣+淨額 exceeds 總額 — and since only `net` reaches the ledger,
    # the discrepancy was invisible afterwards.
    amount_issue = check_amounts(body.gross, body.withhold, body.net)
    if amount_issue is not None:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", amount_issue, field="net"))
    edited = existing.model_copy(update={
        "account_id": body.account_id, "symbol": body.symbol, "date": body.date,
        "type": div_type, "gross": body.gross, "withholding": body.withhold,
        "net": body.net, "reinvest_shares": body.reinvest_shares,
        "reinvest_price": body.reinvest_price,
    })
    would_be = [edited if d.id == div_id else d for d in list_dividends(conn)]
    blocked = _replay_guard(conn, ack_oversell=body.ack_oversell, divs=would_be)
    if blocked is not None:
        return blocked
    update_dividend(
        conn, div_id, account_id=body.account_id, symbol=body.symbol,
        div_date=body.date, div_type=div_type, gross=body.gross,
        withholding=body.withhold, net=body.net,
        reinvest_shares=body.reinvest_shares, reinvest_price=body.reinvest_price,
    )
    return {"ok": True, "id": div_id}


@router.delete("/ledgers/dividends/{div_id}")
def remove_dividend(
    div_id: int,
    ack_oversell: bool = False,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    if get_dividend(conn, div_id) is None:
        return JSONResponse(status_code=404,
                            content=error_body("not_found", f"股利 #{div_id} 不存在"))
    would_be = [d for d in list_dividends(conn) if d.id != div_id]
    blocked = _replay_guard(conn, ack_oversell=ack_oversell, divs=would_be)
    if blocked is not None:
        return blocked
    delete_dividend(conn, div_id)
    return {"ok": True, "id": div_id}


class FxEditBody(BaseModel):
    account_id: str
    date: date
    from_ccy: Currency
    from_amt: Decimal
    to_ccy: Currency
    to_amt: Decimal


@router.put("/ledgers/fx/{fx_id}")
def edit_fx(
    fx_id: int,
    body: FxEditBody,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    if get_fx_conversion(conn, fx_id) is None:
        return JSONResponse(status_code=404,
                            content=error_body("not_found", f"換匯 #{fx_id} 不存在"))
    guard = _mutation_guard(conn, account_id=body.account_id, symbol=None)
    if guard is not None:
        return guard
    if body.from_amt <= 0 or body.to_amt <= 0:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", "換匯金額必須大於 0", field="from_amt"))
    if body.from_ccy is body.to_ccy:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", "換出與換入幣別不可相同", field="to_ccy"))
    update_fx_conversion(
        conn, fx_id, account_id=body.account_id, date=body.date,
        from_ccy=body.from_ccy, from_amount=body.from_amt,
        to_ccy=body.to_ccy, to_amount=body.to_amt,
    )
    return {"ok": True, "id": fx_id}


@router.delete("/ledgers/fx/{fx_id}")
def remove_fx(
    fx_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    if get_fx_conversion(conn, fx_id) is None:
        return JSONResponse(status_code=404,
                            content=error_body("not_found", f"換匯 #{fx_id} 不存在"))
    delete_fx_conversion(conn, fx_id)
    return {"ok": True, "id": fx_id}


class OpeningEditBody(BaseModel):
    """Opening-inventory correction (A6). The authoritative money of record is
    ``total`` (原始總成本); ``avg`` is a legacy alias — when ``total`` is omitted the total is
    derived (avg * shares). One of ``total`` / ``avg`` is required."""

    shares: Decimal
    total: Decimal | None = None
    avg: Decimal | None = None  # legacy: total derived = avg * shares when total omitted
    date: date
    ack_oversell: bool = False


@router.put("/ledgers/openings/{account_id}/{symbol}")
def edit_opening(
    account_id: str,
    symbol: str,
    body: OpeningEditBody,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    existing = get_opening(conn, account_id, symbol)
    if existing is None:
        return JSONResponse(status_code=404, content=error_body(
            "not_found", f"期初 {account_id}/{symbol} 不存在"))
    # Resolve the authoritative total: prefer the explicit 原始總成本; fall back to the legacy
    # avg (total = avg * shares). A rounded average is NEVER stored as the authority.
    if body.total is not None:
        total = body.total
    elif body.avg is not None:
        total = body.avg * body.shares
    else:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", "請提供原始總成本", field="total"))
    if body.shares <= 0:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", "股數必須大於 0", field="shares"))
    # F-13 (D37): the total must be POSITIVE, not merely non-negative. This route already
    # refused a negative one, which is exactly what made zero look deliberate rather than
    # missed — and an edit to 0 is the shortcut D37 forbids, arriving through the door the
    # owner reaches for when the real figure cannot be found. Same rule, same message as the
    # import door (validate.validate_opening_cost); a rule enforced at one of several write
    # doors is how E13 came to be insert-only.
    if (bad_cost := validate_opening_cost(total)) is not None:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", bad_cost.message, field="total"))
    edited = existing.model_copy(update={
        "shares": body.shares,
        "original_cost_total": total, "build_date": body.date,
    })
    would_be = [edited if (o.account_id == account_id and o.symbol == symbol) else o
                for o in list_opening(conn)]
    blocked = _replay_guard(conn, ack_oversell=body.ack_oversell, opening=would_be)
    if blocked is not None:
        return blocked
    upsert_opening(
        conn, account_id=account_id, symbol=symbol, shares=body.shares,
        original_cost_total=total, build_date=body.date,
    )
    return {"ok": True}


@router.delete("/ledgers/openings/{account_id}/{symbol}")
def remove_opening(
    account_id: str,
    symbol: str,
    ack_oversell: bool = False,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    if get_opening(conn, account_id, symbol) is None:
        return JSONResponse(status_code=404, content=error_body(
            "not_found", f"期初 {account_id}/{symbol} 不存在"))
    would_be = [o for o in list_opening(conn)
                if not (o.account_id == account_id and o.symbol == symbol)]
    blocked = _replay_guard(conn, ack_oversell=ack_oversell, opening=would_be)
    if blocked is not None:
        return blocked
    delete_opening(conn, account_id, symbol)
    return {"ok": True}


# ---------------------------------------------------------------------------
# The 5th ledger — corporate actions (W7, spec §6.5 + §6.7)
# ---------------------------------------------------------------------------

_KIND_ZH = {"SPLIT": "分割", "EXCHANGE": "換股", "SPINOFF": "分拆"}
# Accepted on the wire so the form can post what the owner picked in their own words;
# the stored value is always the enum. Mirrors corporate_action_import._KIND_ALIASES —
# the CSV door and the form door must not disagree about what 「分割」 means.
_KIND_ALIASES = {"分割": "SPLIT", "股票分割": "SPLIT", "換股": "EXCHANGE", "分拆": "SPINOFF"}


def reconcile_split_prices(conn: sqlite3.Connection, symbols: Iterable[str]) -> int:
    """Run the §5.1(c) price-basis reconcile for *symbols*; returns rows restated.

    **Call on any insert / edit / delete of a corporate-action row**, after the CRUD has
    committed, on the same connection. Passing a non-SPLIT symbol is a provable no-op
    (``split_factor`` is SPLIT-scoped), which is why an edit may simply pass both ends of
    the row rather than discriminating — and why the OLD ``from_symbol`` must be included
    when an edit moves the action, or that symbol keeps a basis from an action which no
    longer references it.

    A database with no ``prices`` table (a ledger-only DB: the CSV/AI parse paths and most
    unit tests) has nothing to restate, so this degrades to 0 rather than raising an
    ``OperationalError`` out of a write path. Same table probe, same reason, as
    ``validate._has_prices``.
    """
    wanted = sorted({s for s in symbols if s})
    if not wanted:
        return 0
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='prices'"
    ).fetchone() is None:
        return 0
    return reconcile_price_basis(conn, wanted)


class ActionBody(BaseModel):
    """One corporate action as the form posts it.

    The ratio terms and ``cost_carry`` ride as **strings**, not Decimals: pydantic would
    reject a malformed one with an English message from its own validator, and D14's
    rejection has to be the zh one E6/E6a owns. Same reason
    :class:`~data_ingestion.validate.CorporateActionInput` is deliberately permissive.
    """

    account_id: str
    date: date
    kind: str
    from_symbol: str
    to_symbol: str
    ratio_to: str
    ratio_from: str
    cost_carry: str | None = None
    note: str | None = None
    ack_warnings: bool = False
    #: D48b — the SPINOFF child's first price, on the action date. A string for the same
    #: reason the ratio terms are: a malformed one gets this module's zh rejection, not
    #: pydantic's English one. Optional; blank means "wait for the next quote refresh".
    to_symbol_price: str | None = None


def _num(raw: str | None, label: str) -> Decimal | None | JSONResponse:
    """A wire string -> Decimal, or a zh 400. ``None``/blank -> ``None`` (optional field)."""
    if raw is None or not raw.strip():
        return None
    try:
        return Decimal(raw.strip())
    except InvalidOperation:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", f"{label} 必須是數字（目前是「{raw}」）", field=label))


def _action_input(body: ActionBody, account_id: str) -> CorporateActionInput | JSONResponse:
    """Build the validator input for ONE account of the batch."""
    to_term = _num(body.ratio_to, "ratio_to")
    if isinstance(to_term, JSONResponse):
        return to_term
    from_term = _num(body.ratio_from, "ratio_from")
    if isinstance(from_term, JSONResponse):
        return from_term
    carry = _num(body.cost_carry, "cost_carry")
    if isinstance(carry, JSONResponse):
        return carry
    if to_term is None or from_term is None:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", "請填寫比例的兩個整數（換出股數與換得股數）",
            field="ratio_from"))
    raw_kind = body.kind.strip()
    return CorporateActionInput(
        account_id=account_id,
        date=body.date,
        kind=_KIND_ALIASES.get(raw_kind, raw_kind.upper()),
        from_symbol=body.from_symbol.strip(),
        to_symbol=body.to_symbol.strip(),
        ratio_to=to_term,
        ratio_from=from_term,
        cost_carry=carry,
        note=(body.note.strip() if body.note else None) or None,
    )


def _holding_accounts(
    conn: sqlite3.Connection, symbol: str, on: date, *, index: ActionIndex
) -> list[str]:
    """Every account with a NON-ZERO position in *symbol* on *on* — E13's N.

    Enumerating all accounts (there are three or four) and filtering on the action-aware
    ``shares_through`` yields the SAME set as ``validate._accounts_holding_on``'s ledger
    union filtered the same way — a superset of candidates, identical filter — while using
    only the public share-walk API. It must agree with E13, or the form would build a batch
    the validator then rejects.
    """
    return [
        a.account_id for a in list_accounts(conn)
        if shares_through(conn, a.account_id, symbol, on=on, index=index) != _ZERO
    ]


def _later_holders(
    conn: sqlite3.Connection, symbol: str, on: date, *, covered: set[str],
    index: ActionIndex, today: date
) -> list[str]:
    """Accounts holding *symbol* LATER but not on *on* — §6.7's 「不受影響」 line.

    Naming them is not clutter: it is how the owner can tell the system read their ledger
    rather than merely applied a rule to the account they happened to be looking at.
    """
    return [
        a.account_id for a in list_accounts(conn)
        if a.account_id not in covered
        and shares_through(conn, a.account_id, symbol, on=today, index=index) != _ZERO
    ]


class _ActionBatch(BaseModel):
    """The N rows one submitted action becomes (D13/D28), plus who it does not reach."""

    rows: list[CorporateActionInput]
    accounts: list[str]
    not_affected: list[str]


def _build_batch(
    conn: sqlite3.Connection, body: ActionBody, *, index: ActionIndex, today: date
) -> _ActionBatch | JSONResponse:
    """One submitted action -> the COMPLETE E13 batch. Never a partial one.

    D13's all-accounts rule is met by construction here rather than by asking the owner to
    submit N rows: the partial state is what D13 exists to forbid, so a door that can
    express it is a door that will eventually be used to create it. The submitting account
    is always included even when it holds nothing on the date — E1a then rejects the row
    with the accurate reason instead of the batch quietly dropping it.
    """
    symbol = body.from_symbol.strip()
    holders = _holding_accounts(conn, symbol, body.date, index=index)
    accounts = sorted({*holders, body.account_id})
    rows: list[CorporateActionInput] = []
    for account_id in accounts:
        built = _action_input(body, account_id)
        if isinstance(built, JSONResponse):
            return built
        rows.append(built)
    return _ActionBatch(
        rows=rows, accounts=accounts,
        not_affected=_later_holders(
            conn, symbol, body.date, covered=set(accounts), index=index, today=today),
    )


def _stored_from(inp: CorporateActionInput, row_id: int) -> StoredCorporateAction:
    """A candidate row in stored shape, for the would-be replay. Negative id = not real."""
    return StoredCorporateAction(
        id=row_id, account_id=inp.account_id, date=inp.date, kind=inp.kind,
        from_symbol=inp.from_symbol, to_symbol=inp.to_symbol,
        ratio_to=inp.ratio_to, ratio_from=inp.ratio_from,
        cost_carry=inp.cost_carry, note=inp.note,
    )


def _position_wire(h: Holding | None, symbol: str) -> dict[str, Any]:
    """One before/after line. ``None`` (the EXCHANGE-emptied source, which ``build_book``
    drops at zero shares) renders as a real zero row, not as a missing one."""
    if h is None:
        return {"symbol": symbol, "shares": "0", "avg": "0", "cost_total": "0",
                "adjusted_avg": "0", "adjusted_cost_total": "0"}
    return {
        "symbol": h.symbol,
        "shares": decimal_str(h.shares),
        "avg": decimal_str(h.original_avg),
        "cost_total": decimal_str(h.original_cost_total),
        "adjusted_avg": decimal_str(h.adjusted_avg),
        "adjusted_cost_total": decimal_str(h.adjusted_cost_total),
    }


def _by_key(book: Book) -> dict[tuple[str, str], Holding]:
    return {(h.account_id, h.symbol): h for h in book.holdings}


def _fraction_of(shares: Decimal) -> Decimal:
    """The part of a post-action share count the broker pays out in cash (§3.2)."""
    return shares - shares.to_integral_value(rounding=ROUND_DOWN)


def _unblocked_sells(
    conn: sqlite3.Connection,
    *,
    accounts: Sequence[str],
    symbol: str,
    before: ActionIndex,
    after: ActionIndex,
) -> list[dict[str, str]]:
    """Sells that currently fail the date-aware 賣超 guard and would pass with the action.

    Said BEFORE saving (§6.7), because this is the sentence that tells the owner the repair
    they came for actually works — and door 1 arrives here from exactly such a sell.

    **The sell is already IN the ledger; the guard it mirrors judges a row that is not**
    (fixed 2026-08-12). ``validate.py``'s ``held_then`` is ``shares_through`` over a ledger
    that does not yet contain the row being validated, so it tests ``Q > H``. Read here over
    a ledger that DOES contain it, the same call returns ``H − Q``, and the test degenerated
    to ``Q > H − Q``: every sell of more than half a position was announced as an oversell
    (a legal 900-of-1,000 said 「目前為賣超」), while §1's own scenario — 100 shares, an
    oversold 400, a 7-for-1 that really does legalise it — failed the second clause
    (``400 <= 700 − 400``) and reported NOTHING, in the exact case the sentence exists for.

    So both counts add the sell back, recovering the **covering position** ``held_then``
    names. That addition is exact, not an approximation: a SELL contributes ``−quantity`` to
    ``shares_through`` on its own date, and no corporate action inside that window can see
    it — every action the walk includes is dated on or before ``trade_date``, and
    ``EventPriority`` evaluates an action at ``(date, CORPORATE_ACTION)``, strictly before
    the same day's ``(date, SELL)``. Equivalent readings (``was < 0 <= now``) are deliberately
    NOT used: this expression has to stay diff-able against the guard it mirrors.

    Scope, stated because it is narrower than the rendered sentence: the guard has a second
    leg (``inp.quantity > held``, the net across ALL dates via ``current_shares``) which is
    not mirrored here. Adding it would make ``ledgers.py`` a tenth production call site of
    the holdings wrappers — a registry ``tests/data_ingestion/test_holdings_containment.py``
    pins deliberately — and it can only diverge when a LATER sell also oversells, which is a
    state whose action preview is already ``blocking`` on E3 (成本基礎已被捨棄) with the save
    button disabled. If that leg is ever wanted, add the call site to the registry with it.
    """
    found: list[dict[str, str]] = []
    for t in list_transactions(conn, symbol=symbol):
        if t.side is not Side.SELL or t.short_sale or t.account_id not in accounts:
            continue
        held = shares_through(conn, t.account_id, symbol, on=t.trade_date, index=before)
        held_after = shares_through(conn, t.account_id, symbol, on=t.trade_date, index=after)
        was = held + t.quantity          # the covering position, as `held_then` means it
        now = held_after + t.quantity
        if t.quantity > was and t.quantity <= now:
            found.append({"account_id": t.account_id, "symbol": symbol,
                          "date": t.trade_date.isoformat(),
                          "shares": decimal_str(t.quantity)})
    return found


def _issue_wires(issues: Sequence[Issue]) -> list[dict[str, Any]]:
    """Issue wires, de-duplicated by (code, text) — N accounts repeat the same finding."""
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for i in issues:
        wire = issue_wire(i)
        key = (str(wire["code"]), str(wire["text"]))
        if key not in seen:
            seen.add(key)
            unique.append(wire)
    return unique


def _split_conversion(
    conn: sqlite3.Connection,
    source: CorporateActionInput,
    *,
    account_id: str,
    bundle: LedgerBundle,
    book_cache: dict[date, Book],
    index: ActionIndex,
    today: date,
) -> dict[str, Any]:
    """D22's one-click convert-to-SPLIT, **verified before it is offered**.

    E23 tells the owner their EXCHANGE looks like an identifier change; this turns that
    sentence into an action. The row itself is decided by
    :func:`~data_ingestion.validate.identifier_change_repair` — a pure function beside the
    check that raises the finding, so "which symbol survives" is stated once, in the
    module that owns the rule, and not re-derived in a router or in the browser.

    What is added HERE is the half the pure function cannot do: **run the converted row
    through the same validation a hand-typed row gets**, against this ledger, and attach
    the repair only when it would actually commit. E1a is the term that bites — after the
    conversion the SPLIT's source is ``to_symbol``, so the ledger must already record the
    position under the ticker. When it does not, the finding carries ``fix_blocked``: the
    reason in the validator's own words plus what the owner would have to do instead.
    Offering a button that ends in an error is the mistake §6.7 already refused once, when
    it made the multi-account list read-only rather than deselectable.

    A position still standing under the RETIRED symbol is reported as a caveat, never as a
    block: the SPLIT neither creates it nor touches it, it is a pre-existing ledger problem,
    and a repair that quietly leaves a holding behind would be the kind of half-answer this
    feature exists to refuse. Read through the same action-aware walk E13 and E1a use, so
    the caveat cannot disagree with the verdict beside it.

    Offered on the ENTRY path only (this preview), which is where E23 is raised and where
    D22's three options live. A row already stored as an EXCHANGE is repaired through the
    edit modal's own fields — 類型 → 分割 and 來源代號 → the ticker, which is two edits,
    not the "delete and re-enter" this exists to avoid — so a second one-click there would
    be a second surface for a case that already has one. ⚠ That route is single-account:
    the PUT re-validates ONE row, so E13 refuses a row-by-row change to a multi-account
    set, and a one-click would be refused for exactly the same reason. Converting a stored
    N-row set needs a set-level operation, which is an owner decision, not an implementation
    detail.

    Returns ``{}`` (not the shape E23 fires on), ``{"fix": …}`` or ``{"fix_blocked": …}``.
    """
    repaired = identifier_change_repair(source)
    if repaired is None:
        return {}
    converted = ActionBody(
        account_id=account_id,
        date=repaired.date,
        kind=repaired.kind,
        from_symbol=repaired.from_symbol,
        to_symbol=repaired.to_symbol,
        ratio_to=decimal_str(repaired.ratio_to),
        ratio_from=decimal_str(repaired.ratio_from),
        cost_carry=None,
        note=repaired.note,
    )
    # The COMPLETE E13 batch for the converted row, not the converted row alone: the ticker
    # may be held in more accounts than the identifier was, and validating one row of an
    # N-row event reports E13 against a partial set the form would never post. (*today*
    # reaches only `not_affected`, which this function discards — it cannot move the
    # verdict.) A malformed ratio is unreachable here: both terms came back as Decimals.
    built = _build_batch(conn, converted, index=index, today=today)
    if isinstance(built, JSONResponse):
        return {}
    rows = built.rows
    issues = [
        i
        for row in rows
        for i in validate_corporate_action(
            conn, row, batch=rows, bundle=bundle, book_cache=book_cache, index=index)
    ]
    ticker, retired = repaired.from_symbol, source.from_symbol.strip()
    hard = [i for i in issues if not i.needs_confirm]
    if hard:
        tail = ""
        if hard[0].kind == "no_position_on_action_date":
            tail = (f"　這表示帳本是把這檔證券記在 {retired} 之下。"
                    f"要改記為分割，必須先把 {retired} 的交易紀錄改成 {ticker}，"
                    "而不是改這一筆行動。")
        return {"fix_blocked": f"改記為「分割」後仍無法存檔:{hard[0].message}{tail}"}
    # Every key here is consumed by the shared form, and a contract test asserts exactly
    # that: an unread field is a repair half-wired, which is the defect this whole surface
    # exists to close. The finding's own `code` identifies WHICH repair this is — the
    # payload does not repeat it.
    return {"fix": {
        "label": "改記為分割(SPLIT)",
        "summary": (f"改為 {ticker} 在 {repaired.date.isoformat()} 的分割，"
                    f"每 {decimal_str(repaired.ratio_from)} 股 → "
                    f"{decimal_str(repaired.ratio_to)} 股"),
        # The exact body the form re-previews and posts. The browser decides nothing about
        # the converted row; it applies a patch the server derived and already validated.
        "body": converted.model_dump(mode="json"),
        "caveat": (f"{retired} 在 {repaired.date.isoformat()} 仍有持倉。"
                   "改記為分割後這筆行動不會處理它，該筆紀錄請另行確認"
                   if _holding_accounts(conn, retired, repaired.date, index=index)
                   else None),
    }}


def _with_fix(
    wires: list[dict[str, Any]], patch: dict[str, Any], *,
    code: str = IDENTIFIER_CHANGE_SUSPECTED,
) -> list[dict[str, Any]]:
    """Attach a finding's repair to that finding's own wire — or nothing carries it.

    Only the top-level issue list is patched. The per-account lists are a detail view of
    the SAME finding (D13 writes one event as N rows, each carrying it), and rendering the
    one-click N times would offer the same single repair once per account.

    ``code`` defaults to E23 (D22's convert-to-SPLIT), the case this was written for; D44's
    band restatement is the second user. Two findings, one attachment rule.
    """
    if patch:
        for w in wires:
            if w.get("code") == code:
                w.update(patch)
    return wires


def _band_restatement(
    conn: sqlite3.Connection, inp: CorporateActionInput
) -> dict[str, Any]:
    """D44's one-click: the owner's stale target band, restated across this SPLIT's ratio.

    Computed by :func:`~data_ingestion.validate.restated_band` — the same expression the
    finding's message quotes — so the number on screen, the number in this payload and the
    number the button writes are one number. Two of them would be §5.1's "two numbers on
    one screen", here in the one place the owner is being asked to choose between them.

    **``apply`` is a ready-made body for ``PUT /api/instruments/{symbol}``**, carrying only
    the legs that are actually set. That endpoint's ``exclude_unset`` is what makes the
    partial body safe: an absent key is "unchanged", while an explicit null CLEARS the
    level — so naming only the set legs cannot wipe the other one.

    No verification step, unlike E23's :func:`_split_conversion`. That one had to prove the
    converted ROW would validate before offering a button that writes to the ledger; this
    writes an alert threshold on an instrument the owner already owns, through the ordinary
    edit endpoint, and is undone by typing the old number back. The asymmetry is deliberate:
    the cost of being wrong here is one wrong alert level, not a discarded cost basis.
    """
    inst = get_instrument(conn, inp.from_symbol)
    if inst is None or inst.target_set_at is None:
        return {}
    legs = restated_band(inst, ratio_to=inp.ratio_to, ratio_from=inp.ratio_from)
    if not legs:
        return {}
    return {"restate": {
        "symbol": inst.symbol,
        "set_at": inst.target_set_at.isoformat(),
        "label": f"改為換算後的目標價（{inp.ratio_to} 比 {inp.ratio_from}）",
        "levels": [{"field": f, "label": lbl,
                    "current": decimal_str(cur), "restated": decimal_str(new)}
                   for f, lbl, cur, new in legs],
        "apply": {f: decimal_str(new) for f, _lbl, _cur, new in legs},
    }}


def _spinoff_child_draft(
    conn: sqlite3.Connection, inp: CorporateActionInput
) -> Instrument | None:
    """The not-yet-registered SPINOFF child, for the preview's in-memory bundle (D48a)."""
    if inp.kind.strip().upper() != CorporateActionKind.SPINOFF.value:
        return None
    if get_instrument(conn, inp.to_symbol) is not None:
        return None
    parent = get_instrument(conn, inp.from_symbol)
    return None if parent is None else spinoff_child_draft(parent, inp.to_symbol)


def _preview_payload(
    conn: sqlite3.Connection, body: ActionBody, today: date
) -> dict[str, Any] | JSONResponse:
    """The always-on form preview: the conservation law made visible (§6.7).

    Both sides come from the REAL replay — the current book, and the book with the
    candidate rows appended. Nothing here re-derives a share count or a cost, so the
    preview cannot disagree with what saving does; that disagreement is the failure mode
    §5.1 calls the worst kind (two numbers on one screen).
    """
    index = load_action_index(conn)
    batch = _build_batch(conn, body, index=index, today=today)
    if isinstance(batch, JSONResponse):
        return batch
    stored = list_corporate_actions(conn)
    candidates = [_stored_from(inp, -(i + 1)) for i, inp in enumerate(batch.rows)]
    book_cache: dict[date, Book] = {}
    try:
        # `full_bundle` is the hoist: validation scopes it PER ACTION DATE itself, because
        # the four book-derived rejections must not see trades dated after the action they
        # are judging (2026-08-11). `pre` / `post` stay whole-ledger — they are the
        # before/after PREVIEW the owner reads, which is a different question.
        full_bundle = load_ledger_bundle(conn)
        pre = build_book(full_bundle, allow_oversell=True)
        post_bundle = load_ledger_bundle(conn, actions=[*stored, *candidates])
        # D48a: the child does not exist until save, and the replay needs its quote currency
        # to value the position the SPINOFF creates. Added to the POST bundle IN MEMORY —
        # previewing must never write, and this runs on every keystroke. Built by the same
        # inheritance rule the save uses, so the ✓ 成本不變 shown here is computed under the
        # currency the save will actually assign.
        if (draft := _spinoff_child_draft(conn, batch.rows[0])) is not None:
            post_bundle.instruments[draft.symbol] = draft
        post = build_book(post_bundle, allow_oversell=True)
    except (ValueError, KeyError) as exc:
        return JSONResponse(status_code=422, content=error_body(
            "ledger_unbookable",
            f"目前的帳本無法重播,因此無法試算這筆公司行動({exc})。請先修正帳本紀錄"))

    accts, _names_map, _ccys = _names(conn)
    issues: list[Issue] = []
    accounts_wire: list[dict[str, Any]] = []
    cost_before = cost_after = adj_before = adj_after = _ZERO
    fractions: list[dict[str, str]] = []
    pre_map, post_map = _by_key(pre), _by_key(post)
    symbols = [body.from_symbol.strip()]
    if body.to_symbol.strip() != body.from_symbol.strip():
        symbols.append(body.to_symbol.strip())

    for inp in batch.rows:
        row_issues = validate_corporate_action(
            conn, inp, batch=batch.rows, bundle=full_bundle,
            book_cache=book_cache, index=index)
        issues.extend(row_issues)
        before_rows = [_position_wire(pre_map.get((inp.account_id, s)), s)
                       for s in symbols]
        after_rows = [_position_wire(post_map.get((inp.account_id, s)), s)
                      for s in symbols]
        acct_before = sum((Decimal(r["cost_total"]) for r in before_rows), _ZERO)
        acct_after = sum((Decimal(r["cost_total"]) for r in after_rows), _ZERO)
        cost_before += acct_before
        cost_after += acct_after
        adj_before += sum((Decimal(r["adjusted_cost_total"]) for r in before_rows), _ZERO)
        adj_after += sum((Decimal(r["adjusted_cost_total"]) for r in after_rows), _ZERO)
        for r in after_rows:
            frac = _fraction_of(Decimal(r["shares"]))
            if frac != _ZERO:
                fractions.append({"account_id": inp.account_id, "symbol": r["symbol"],
                                  "shares": decimal_str(frac)})
        accounts_wire.append({
            "account_id": inp.account_id,
            "account": accts.get(inp.account_id, inp.account_id),
            "before": before_rows,
            "after": after_rows,
            "cost_before": decimal_str(acct_before),
            "cost_after": decimal_str(acct_after),
            "conserved": acct_before == acct_after,
            "issues": _issue_wires(row_issues),
        })

    inst = get_instrument(conn, body.from_symbol.strip())
    kind = batch.rows[0].kind
    # E23's repair, computed only when E23 actually fired — the finding and its one-click
    # travel together, so a warning can never outlive the fix that clears it, and the extra
    # replay-and-validate it costs is never paid by an ordinary merger, which does not warn.
    fix = (
        _split_conversion(conn, batch.rows[0], account_id=body.account_id,
                          bundle=full_bundle, book_cache=book_cache, index=index,
                          today=today)
        if any(i.kind == IDENTIFIER_CHANGE_SUSPECTED for i in issues) else {}
    )
    # D44's restate, on the same terms: computed only when the finding actually fired, so an
    # ordinary split — which does not warn — never pays for the instrument read.
    restate = (
        _band_restatement(conn, batch.rows[0])
        if any(i.kind == TARGET_BAND_PREDATES_SPLIT for i in issues) else {}
    )
    return {
        "ccy": inst.quote_ccy.value if inst is not None else "",
        "kind": kind,
        "kind_label": _KIND_ZH.get(kind, kind),
        "accounts": accounts_wire,
        "not_affected": [{"account_id": a, "account": accts.get(a, a),
                          "reason": "部位在行動日之後才建立,這筆行動不會套用"}
                         for a in batch.not_affected],
        "rows_to_write": len(batch.rows),
        "cost_before_total": decimal_str(cost_before),
        "cost_after_total": decimal_str(cost_after),
        # BOTH basis legs (§2.1): original is the conservation law's own statement, and
        # adjusted is what P&L is computed against. A carve that conserved one and not the
        # other would print 成本不變 ✓ over a moved number.
        "conserved": cost_before == cost_after and adj_before == adj_after,
        "issues": _with_fix(
            _with_fix(_issue_wires(issues), fix), restate,
            code=TARGET_BAND_PREDATES_SPLIT),
        "blocking": any(not i.needs_confirm for i in issues),
        "needs_confirm": any(i.needs_confirm for i in issues),
        "fractions": fractions,
        "unpriced_symbols": sorted({
            body.to_symbol.strip() for i in issues if i.kind == "to_symbol_unpriced"}),
        # D47: said BEFORE saving, for the same reason `unblocks` is — the owner should not
        # discover that their alert band followed the ticker by noticing it later. Read
        # through the SAME predicate the commit uses, so the promise cannot outlive the rule.
        "band_move": _band_moved_wire(
            pending_band_move(conn, from_symbol=batch.rows[0].from_symbol,
                              to_symbol=batch.rows[0].to_symbol)
            if kind.strip().upper() == CorporateActionKind.EXCHANGE.value else None),
        "unblocks": _unblocked_sells(
            conn, accounts=batch.accounts, symbol=body.from_symbol.strip(),
            before=index, after=ActionIndex.from_stored([*stored, *candidates])),
    }


@router.get("/ledgers/corporate-actions")
def corporate_actions(
    account_id: str | None = None, symbol: str | None = None,
    frm: str | None = Query(None, alias="from"), to: str | None = None,
    limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    bad = _check_dates(frm, to)
    if bad is not None:
        return bad
    accts, names, ccys = _names(conn)
    out: list[dict[str, Any]] = []
    for a in list_corporate_actions(conn, account_id=account_id, symbol=symbol):
        if not _in_range(a.date, frm, to):
            continue
        out.append({
            "id": a.id, "date": a.date.isoformat(), "account_id": a.account_id,
            "account": accts.get(a.account_id, a.account_id),
            # `symbol` (not `from_symbol`) so the page's shared keyword filter and the
            # symbol-cell renderer work on this table with no per-tab special case.
            "symbol": a.from_symbol, "name": names.get(a.from_symbol, ""),
            "to_symbol": a.to_symbol, "to_name": names.get(a.to_symbol, ""),
            "kind": a.kind, "kind_label": _KIND_ZH.get(a.kind, a.kind),
            "ratio_to": decimal_str(a.ratio_to),
            "ratio_from": decimal_str(a.ratio_from),
            # Rendered server-side in the owner's own phrasing (§6.7) so the two integer
            # terms never have to be recombined in the browser.
            "ratio_label": (f"每 {decimal_str(a.ratio_from)} 股 → "
                            f"{decimal_str(a.ratio_to)} 股"),
            "cost_carry": (decimal_str(a.cost_carry)
                           if a.cost_carry is not None else None),
            "note": a.note, "ccy": ccys.get(a.from_symbol, ""),
        })
    return _page(out, limit, offset)


@router.post("/ledgers/corporate-actions/preview")
def preview_corporate_action(
    body: ActionBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    return _preview_payload(conn, body, now.date())


@router.post("/ledgers/corporate-actions", status_code=201)
def add_corporate_action(
    body: ActionBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    """Write the COMPLETE E13 batch for one submitted action, then reconcile prices.

    F-40's obligation in one place: the batch is validated by
    ``validate_corporate_action`` **with every sibling row visible**, which is the only way
    E12/E13 can be right. Hard issues 400 with the issue list; soft ones 422 until
    ``ack_warnings`` (the 賣超 tier).
    """
    index = load_action_index(conn)
    batch = _build_batch(conn, body, index=index, today=now.date())
    if isinstance(batch, JSONResponse):
        return batch
    book_cache: dict[date, Book] = {}
    try:
        full_bundle = load_ledger_bundle(conn)
        build_book(full_bundle, allow_oversell=True)   # reachability check only
    except (ValueError, KeyError) as exc:
        return JSONResponse(status_code=422, content=error_body(
            "ledger_unbookable",
            f"目前的帳本無法重播,因此無法登錄公司行動({exc})。請先修正帳本紀錄"))
    # D48b: the optional child price is refused LOUDLY rather than dropped. A value the
    # owner typed that silently does not arrive is the failure mode this whole module is
    # written against — and here it would be invisible, because the field's only effect is
    # a price row nobody looks at until the XIRR stays dark.
    if (bad := _child_price_refusal(batch.rows[0].kind, body.to_symbol_price)) is not None:
        return bad
    issues: list[Issue] = []
    for inp in batch.rows:
        issues.extend(validate_corporate_action(
            conn, inp, batch=batch.rows, bundle=full_bundle,
            book_cache=book_cache, index=index))
    hard = [i for i in issues if not i.needs_confirm]
    if hard:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", hard[0].message, issues=_issue_wires(issues)))
    if issues and not body.ack_warnings:
        return JSONResponse(status_code=422, content=error_body(
            "warnings_unacknowledged", issues[0].message, issues=_issue_wires(issues)))
    # ALL-OR-NOTHING. The N rows are one event (D13), so a batch that half-lands is the
    # partial state E13 exists to forbid, created by the writer instead of by the owner.
    # Every insert defers its commit and one rollback covers the lot.
    try:
        # D48a: created BEFORE the rows that reference it, so nothing between here and the
        # reconcile reads an action pointing at a symbol the registry does not have — and
        # INSIDE the same try, with ``commit=False``, so the one rollback below covers it.
        # An instrument left behind by a failed save would be a phantom the owner never
        # asked for. Idempotent and self-limiting: it returns None once the child exists.
        if batch.rows[0].kind.strip().upper() == CorporateActionKind.SPINOFF.value:
            autoregister_spinoff_child(conn, parent_symbol=batch.rows[0].from_symbol,
                                       child_symbol=batch.rows[0].to_symbol, commit=False)
        written = [
            insert_corporate_action(
                conn, account_id=inp.account_id, action_date=inp.date,
                kind=CorporateActionKind(inp.kind), from_symbol=inp.from_symbol,
                to_symbol=inp.to_symbol, ratio_to=inp.ratio_to,
                ratio_from=inp.ratio_from, cost_carry=inp.cost_carry, note=inp.note,
                commit=False)
            for inp in batch.rows
        ]
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    restated = reconcile_split_prices(
        conn, {body.from_symbol.strip(), body.to_symbol.strip()})
    # D47: an EXCHANGE re-keys the position, so the owner's alert band follows the ticker.
    # After the rows land, never before — a band moved onto a symbol whose action then
    # failed to commit would be an alert on a security this ledger does not hold.
    moved = (
        move_target_band(conn, from_symbol=batch.rows[0].from_symbol,
                         to_symbol=batch.rows[0].to_symbol)
        if batch.rows[0].kind.strip().upper() == CorporateActionKind.EXCHANGE.value
        else None
    )
    priced = _seed_child_price(conn, batch.rows[0], body.to_symbol_price, now=now)
    return {"ok": True, "written": len(written), "ids": written,
            "accounts": batch.accounts, "prices_restated": restated,
            "band_moved": _band_moved_wire(moved),
            "child_priced": priced,
            # D48b: a seeded price answers the very warning this field reports, so a symbol
            # that just got one is no longer unpriced. Leaving it listed would send the owner
            # to 更新報價 for a price they typed a second ago.
            "unpriced_symbols": sorted({
                body.to_symbol.strip() for i in issues
                if i.kind == "to_symbol_unpriced"} - ({priced} if priced else set()))}


def _child_price_refusal(kind: str, raw: str | None) -> JSONResponse | None:
    """D48b's entry guard: reject a child price that is unusable or inapplicable.

    Mirrors ``cost_carry_not_applicable`` — a value supplied for the wrong kind is a
    misunderstanding worth naming, not something to ignore. Returns ``None`` when the field
    is blank (the ordinary case) or valid.

    Takes *kind* from the BUILT batch rather than from the raw body, so it reads the same
    normalised value every other check in this request does.
    """
    text = (raw or "").strip()
    if not text:
        return None
    if kind.strip().upper() != CorporateActionKind.SPINOFF.value:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error",
            f"子公司起始價僅適用於分拆,{kind} 不需填寫",
            field="to_symbol_price"))
    try:
        close = Decimal(text)
    except InvalidOperation:
        close = Decimal(0)
    if close <= 0:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error",
            f"子公司起始價必須是正數,目前是 {text}",
            field="to_symbol_price"))
    return None


def _seed_child_price(
    conn: sqlite3.Connection, inp: CorporateActionInput, raw: str | None, *, now: datetime
) -> str | None:
    """D48b: store the SPINOFF child's first price, dated the action day. Returns the symbol
    priced, or ``None`` when there was nothing to write.

    **Why it is worth a field at all.** ``returns.py`` is all-or-nothing on the terminal
    value: ONE unpriced holding makes the WHOLE portfolio's XIRR ``None``, not just that
    symbol's. And a SPINOFF is guaranteed to create an unpriced holding — the child did not
    exist to be quoted. So between saving the action and the next successful refresh, the
    headline return goes dark for a reason nothing on the page explains. The owner reading
    the child's opening price off the same statement can end that in one box.

    **Written through** ``pricing.store.upsert_prices``, from ``api/`` — ``pricing/`` owns
    every write to ``prices`` (``architecture.md``) and ``data_ingestion`` may not reach in.
    ``factor_of`` is left at the identity: the row is dated the action day and the child is
    brand new, so no split can exist between that date and now to un-adjust. That is a
    derivation from the child's age, not a shortcut.

    **Degrades rather than raises.** ``bootstrap_db`` does not create ``prices`` (only
    ``pricing.schema.create_tables`` does), so a ledger-only database has no such table —
    the same condition ``validate._has_prices`` already absorbs. A corporate action must not
    become unrecordable because an optional convenience has nowhere to go.
    """
    text = (raw or "").strip()
    if not text or inp.kind.strip().upper() != CorporateActionKind.SPINOFF.value:
        return None
    try:
        close = Decimal(text)
    except InvalidOperation:
        return None
    if close <= 0:
        return None
    inst = get_instrument(conn, inp.to_symbol)
    if inst is None:
        return None
    try:
        upsert_prices(
            conn,
            [PriceRow(instrument=inst.symbol, market=inst.market, as_of=inp.date,
                      close=close, source="manual")],
            fetched_at=now,
        )
    except sqlite3.OperationalError:
        return None
    return inst.symbol


def _band_moved_wire(moved: MovedBand | None) -> dict[str, Any] | None:
    """D47's outcome on the wire — money as Decimal STRINGS, formatted here rather than by
    Pydantic's JSON mode, so this payload uses the same canonical form as every other
    number the frontend receives (``decimal_str``)."""
    if moved is None:
        return None
    return {
        "from_symbol": moved.from_symbol,
        "to_symbol": moved.to_symbol,
        "target_low": (decimal_str(moved.target_low)
                       if moved.target_low is not None else None),
        "target_high": (decimal_str(moved.target_high)
                        if moved.target_high is not None else None),
        "set_at": moved.set_at.isoformat() if moved.set_at is not None else None,
    }


def _change_block(issues: list[Issue]) -> JSONResponse | None:
    """F-32's refusal, mapped onto the error envelope under its own code."""
    hard = [i for i in issues if not i.needs_confirm]
    if not hard:
        return None
    return JSONResponse(status_code=422, content=error_body(
        hard[0].kind, hard[0].message, issues=_issue_wires(issues)))


@router.put("/ledgers/corporate-actions/{action_id}")
def edit_corporate_action(
    action_id: int, body: ActionBody, conn: sqlite3.Connection = Depends(get_conn)
) -> Any:
    """Edit one row — re-validated on the way OUT (F-32) and IN, then both ends reconciled.

    E16 / domain-ledger N2: an edit RE-COMPUTES history, nothing is snapshotted. The
    before-image goes to ``ledger_audit`` (the store does that), and the price basis of the
    OLD symbol has to be restated too — otherwise it keeps a basis from an action that no
    longer references it.
    """
    existing = get_corporate_action(conn, action_id)
    if existing is None:
        return JSONResponse(status_code=404, content=error_body(
            "not_found", f"公司行動 #{action_id} 不存在"))
    replacement = _action_input(body, body.account_id)
    if isinstance(replacement, JSONResponse):
        return replacement
    blocked = _change_block(
        validate_corporate_action_change(conn, action_id, replacement=replacement))
    if blocked is not None:
        return blocked
    # Re-validate the row's own §5 rules against the ledger WITHOUT it, so an edit that
    # leaves a field alone is not rejected as a duplicate of itself.
    siblings = [a for a in list_corporate_actions(conn) if a.id != action_id]
    index = ActionIndex.from_stored(siblings)
    try:
        sibling_bundle = load_ledger_bundle(conn, actions=siblings)
        build_book(sibling_bundle, allow_oversell=True)   # reachability check only
    except (ValueError, KeyError) as exc:
        return JSONResponse(status_code=422, content=error_body(
            "ledger_unbookable", f"帳本無法重播,無法修改這筆公司行動({exc})"))
    issues = [
        i for i in validate_corporate_action(
            conn, replacement, batch=[replacement], bundle=sibling_bundle,
            book_cache={}, index=index)
        # Three rules must be dropped, and E13 must NOT be. `validate_corporate_action`
        # reads the STORED ledger, which still holds the row being edited — so a no-op
        # edit matches itself and would be refused as its own duplicate, its own same-date
        # conflict, and (once the ratio moves) its own conflicting ratio. Those three are
        # re-checked against the SET by `validate_corporate_action_change` above, the guard
        # written for the edit path.
        #
        # E13 stays, and it is the one that earns its place here: an edit that moves the
        # row onto a DIFFERENT `from_symbol` is a fresh all-accounts question that the
        # change guard never asks, and the stored row (carrying the OLD symbol) does not
        # answer it. Dropping it too would leave the symbol-change edit as an unguarded
        # door onto exactly the partial state D13 forbids.
        if i.kind not in {"duplicate_action", "same_date_action_conflict",
                          "conflicting_ratio"}
    ]
    hard = [i for i in issues if not i.needs_confirm]
    if hard:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", hard[0].message, issues=_issue_wires(issues)))
    if issues and not body.ack_warnings:
        return JSONResponse(status_code=422, content=error_body(
            "warnings_unacknowledged", issues[0].message, issues=_issue_wires(issues)))
    update_corporate_action(
        conn, action_id, account_id=replacement.account_id,
        action_date=replacement.date, kind=CorporateActionKind(replacement.kind),
        from_symbol=replacement.from_symbol, to_symbol=replacement.to_symbol,
        ratio_to=replacement.ratio_to, ratio_from=replacement.ratio_from,
        cost_carry=replacement.cost_carry, note=replacement.note)
    # BOTH ends of BOTH shapes — the row may have moved symbol, and the ABANDONED one is
    # the half a symbol-blind reconcile leaves behind holding a basis nothing references.
    restated = reconcile_split_prices(conn, {
        existing.from_symbol, existing.to_symbol,
        replacement.from_symbol, replacement.to_symbol})
    return {"ok": True, "id": action_id, "prices_restated": restated}


@router.delete("/ledgers/corporate-actions/set")
def remove_corporate_action_set(
    from_symbol: str,
    on: str = Query(..., alias="date"),
    kind: str = Query(...),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    """Delete a whole ``(from_symbol, date, kind)`` set — the ONLY way to leave one.

    「Leaving the set requires taking the set」 (F-32). Offering this beside the per-row
    refusal is what keeps that refusal from being a dead end: an owner who really does want
    the action gone has a correct action available, so the guard informs rather than traps.

    Declared BEFORE the ``/{action_id}`` route: FastAPI matches in declaration order and
    ``set`` would otherwise be parsed as an int path param (a 400 on the literal path).
    """
    wanted = kind.strip().upper()
    try:
        action_date = date.fromisoformat(on)
    except ValueError:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", f"日期格式無效:{on}", field="date"))
    rows = [a for a in list_corporate_actions(conn)
            if a.from_symbol == from_symbol and a.date == action_date
            and a.kind == wanted]
    if not rows:
        return JSONResponse(status_code=404, content=error_body(
            "not_found", f"找不到 {from_symbol} 在 {on} 的{_KIND_ZH.get(wanted, wanted)}"))
    symbols = {a.from_symbol for a in rows} | {a.to_symbol for a in rows}
    for a in rows:
        delete_corporate_action(conn, a.id)
    restated = reconcile_split_prices(conn, symbols)
    return {"ok": True, "deleted": len(rows), "prices_restated": restated}


@router.delete("/ledgers/corporate-actions/{action_id}")
def remove_corporate_action(
    action_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> Any:
    """Delete one row — refused when it belongs to a multi-account set (F-32)."""
    existing = get_corporate_action(conn, action_id)
    if existing is None:
        return JSONResponse(status_code=404, content=error_body(
            "not_found", f"公司行動 #{action_id} 不存在"))
    blocked = _change_block(validate_corporate_action_change(conn, action_id))
    if blocked is not None:
        return blocked
    delete_corporate_action(conn, action_id)
    restated = reconcile_split_prices(conn, {existing.from_symbol, existing.to_symbol})
    return {"ok": True, "id": action_id, "prices_restated": restated}
