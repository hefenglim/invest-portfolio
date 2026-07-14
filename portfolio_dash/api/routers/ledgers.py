"""Four ledgers: reads (spec 11) + explicit row corrections (edit/delete, 2026-07-02).

Reads are thin over store.list_*. Corrections stay within the "append-only in
spirit" rule: they are EXPLICIT user actions via PUT/DELETE (never silent
mutation), validated by replaying the WOULD-BE ledger through build_book before
anything is written — an edit/delete that would strand a later sell (oversell)
is refused with 422 unless the user explicitly acks it (mirroring manual entry;
the dashboard degrades an acked oversold book to a flagged 賣超 holding).

Side/DividendType serialize lowercase (SR #1); Currency stays uppercase. The `total`
sign + `implied_rate` are presentation-level derived fields over stored ledger values.
"""

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from portfolio_dash.api.deps import get_conn
from portfolio_dash.api.errors import error_body
from portfolio_dash.api.wire import parse_side
from portfolio_dash.data_ingestion.config_seed import get_fee_rule_set
from portfolio_dash.data_ingestion.fees import FeeComputationError, compute_fees
from portfolio_dash.data_ingestion.markets import MARKET_ZH, account_market
from portfolio_dash.data_ingestion.store import (
    StoredDividend,
    StoredOpening,
    StoredTransaction,
    delete_dividend,
    delete_fx_conversion,
    delete_opening,
    delete_transaction,
    get_dividend,
    get_fx_conversion,
    get_instrument,
    get_opening,
    get_transaction,
    list_accounts,
    list_dividends,
    list_fx_conversions,
    list_instruments,
    list_opening,
    list_transactions,
    update_dividend,
    update_fx_conversion,
    update_transaction,
    upsert_opening,
)
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType
from portfolio_dash.shared.models.ledger import (
    Dividend,
    OpeningInventory,
    Transaction,
)
from portfolio_dash.shared.wire import decimal_str

router = APIRouter()


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
            "avg": decimal_str(o.original_avg_cost),
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


_Models = tuple[list[Transaction], list[Dividend], list[OpeningInventory]]


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
) -> _Models:
    """Build ledger models from the mutated list(s); unspecified ledgers load from store.

    Rows whose symbol is unregistered are excluded (same degradation as the dashboard)
    so one legacy bad row cannot block corrections to healthy rows.
    """
    s_txs = txs if txs is not None else list_transactions(conn)
    s_divs = divs if divs is not None else list_dividends(conn)
    s_open = opening if opening is not None else list_opening(conn)
    instruments = {i.symbol: i for i in list_instruments(conn)}
    t_models = [
        Transaction(account_id=s.account_id, symbol=s.symbol, side=s.side,
                    quantity=s.quantity, price=s.price, fees=s.fees, tax=s.tax,
                    trade_date=s.trade_date)
        for s in s_txs if s.symbol in instruments
    ]
    d_models = [
        Dividend(account_id=s.account_id, symbol=s.symbol, date=s.date,
                 type=DividendType(s.type), gross=s.gross, withholding=s.withholding,
                 net=s.net, reinvest_shares=s.reinvest_shares,
                 reinvest_price=s.reinvest_price)
        for s in s_divs if s.symbol in instruments
    ]
    o_models = [
        OpeningInventory(account_id=s.account_id, symbol=s.symbol, shares=s.shares,
                         original_avg_cost=s.original_avg_cost,
                         original_cost_total=s.original_cost_total,
                         build_date=s.build_date)
        for s in s_open if s.symbol in instruments
    ]
    return t_models, d_models, o_models


def _orphan_keys(models: _Models) -> set[tuple[str, str]]:
    """(account, symbol) dividend keys with NO buy/sell/opening on-or-before the div date.

    These are exactly the rows on which ``build_book`` raises ``ValueError`` ('dividend
    for unknown position') — computing the set directly (rather than catching) lets the
    caller scope the block to orphans the mutation INTRODUCES (audit H3)."""
    t_models, d_models, o_models = models
    orphans: set[tuple[str, str]] = set()
    for dv in d_models:
        covered = any(
            o.account_id == dv.account_id and o.symbol == dv.symbol
            and o.build_date <= dv.date for o in o_models
        ) or any(
            t.account_id == dv.account_id and t.symbol == dv.symbol
            and t.trade_date <= dv.date for t in t_models
        )
        if not covered:
            orphans.add((dv.account_id, dv.symbol))
    return orphans


def _oversold_shares(
    models: _Models, instruments: dict[str, Instrument]
) -> dict[tuple[str, str], Decimal] | None:
    """Map of (account, symbol) → negative shares for oversold positions.

    ``None`` when the ledger is un-bookable (e.g. a pre-existing orphan) — the caller
    then declines to scope the oversell rather than block an unrelated correction."""
    t_models, d_models, o_models = models
    try:
        book = build_book(t_models, d_models, o_models, instruments, allow_oversell=True)
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
    instruments = {i.symbol: i for i in list_instruments(conn)}
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

    post_over = _oversold_shares(post, instruments)
    pre_over_raw = _oversold_shares(pre, instruments)
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
            acct_mkt = account_market(conn, account_id)
            if acct_mkt is not None and inst.market is not acct_mkt:
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
        row = conn.execute(
            "SELECT fee_rule_set FROM accounts WHERE account_id=?", (body.account_id,)
        ).fetchone()
        inst = get_instrument(conn, body.symbol)
        if row is not None:
            try:
                fr = compute_fees(
                    get_fee_rule_set(row["fee_rule_set"]), side, body.shares, body.price,
                    is_etf=inst.is_etf if inst is not None else False,
                    daytrade=daytrade,
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
    if body.gross < 0 or body.withhold < 0 or body.net < 0:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", "股利金額不可為負", field="gross"))
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
    shares: Decimal
    avg: Decimal
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
    if body.shares <= 0 or body.avg < 0:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", "股數必須大於 0、均價不可為負", field="shares"))
    total = body.avg * body.shares
    edited = existing.model_copy(update={
        "shares": body.shares, "original_avg_cost": body.avg,
        "original_cost_total": total, "build_date": body.date,
    })
    would_be = [edited if (o.account_id == account_id and o.symbol == symbol) else o
                for o in list_opening(conn)]
    blocked = _replay_guard(conn, ack_oversell=body.ack_oversell, opening=would_be)
    if blocked is not None:
        return blocked
    upsert_opening(
        conn, account_id=account_id, symbol=symbol, shares=body.shares,
        original_avg_cost=body.avg, original_cost_total=total, build_date=body.date,
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
