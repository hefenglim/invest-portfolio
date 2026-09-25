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
from typing import Any, NamedTuple

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from portfolio_dash.api.deps import get_conn, get_now
from portfolio_dash.api.errors import error_body
from portfolio_dash.api.instrument_service import reconcile_price_basis

# DEF-049 (2026-09-25): the replay guard moved to ``api/replay_guard.py`` so the import-batch
# undo runs the SAME decision as the row doors below. Re-exported under the same names
# (``input_center`` imports ``_to_models`` from here).
from portfolio_dash.api.replay_guard import (
    _oversell_response as _oversell_response,
)
from portfolio_dash.api.replay_guard import (
    _replay_block as _replay_block,
)
from portfolio_dash.api.replay_guard import (
    _replay_guard as _replay_guard,
)
from portfolio_dash.api.replay_guard import (
    _to_models as _to_models,
)
from portfolio_dash.api.replay_guard import action_change_guard

# Sibling router, same layer, no cycle (``cash`` imports nothing from here) — the same shape
# ``rebates.py`` already uses for ``movement_guard``. The alternative is a second spelling of
# the 換匯 guard on the correction door, which is exactly the drift QA-10 found.
from portfolio_dash.api.routers.cash import cash_pool_fn, fx_change_guard, fx_delete_guard
from portfolio_dash.api.wire import issue_wire, parse_side
from portfolio_dash.data_ingestion.config_seed import get_fee_rule_set
from portfolio_dash.data_ingestion.dividend_model import apply_dividend_model
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
    BandRestoreVerdict,
    ChildSeedRecord,
    MovedBand,
    MovedWeight,
    StoredCashMovement,
    StoredCorporateAction,
    StoredDividend,
    StoredTransaction,
    delete_cash_movement,
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
    insert_cash_movement,
    insert_corporate_action,
    linked_cash_movements,
    list_accounts,
    list_cash_movements,
    list_corporate_actions,
    list_dividends,
    list_fx_conversions,
    list_instruments,
    list_opening,
    list_transactions,
    load_ledger_bundle,
    move_target_band,
    pending_band_move,
    pending_band_restore,
    restore_target_band,
    set_child_seed,
    update_cash_movement,
    update_corporate_action,
    update_dividend,
    update_fx_conversion,
    update_transaction,
    upsert_opening,
)
from portfolio_dash.data_ingestion.validate import (
    IDENTIFIER_CHANGE_SUSPECTED,
    TARGET_BAND_PREDATES_SPLIT,
    CashMovementInput,
    CorporateActionInput,
    DividendInput,
    Issue,
    TxnInput,
    identifier_change_repair,
    restated_band,
    unknown_account_message,
    validate_cash_movement,
    validate_corporate_action,
    validate_corporate_action_change,
    validate_dividend,
    validate_opening_cost,
    validate_transaction,
)
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.portfolio.results import Book, Holding
from portfolio_dash.pricing.seed import (
    SeedPriceVerdict,
    SeedSkip,
    occupied_slot,
    pending_seed_removal,
    remove_seed_price,
    write_seed_price,
)
from portfolio_dash.shared.account_ref import account_ref
from portfolio_dash.shared.cash_kinds import CASH_KIND_ZH, CashKind, movement_sign
from portfolio_dash.shared.corporate_actions import (
    KIND_ZH,
    ActionIndex,
    CorporateActionKind,
)
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.models.ledger import (
    LedgerBundle,
    dividend_effective_date,
    pending_from,
)
from portfolio_dash.shared.wire import decimal_str
from portfolio_dash.strategy import signal_history, signal_states
from portfolio_dash.strategy.target_weights import (
    WeightRestoreVerdict,
    move_target_weight,
    pending_weight_move,
    pending_weight_restore,
    restore_target_weight,
)

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


def _counts_from(counts_on: date, today: date) -> str | None:
    """The row's ``counts_from`` flag (DEF-056, owner ruling 2026-09-24): the ISO day a row
    dated after the SERVER's valuation day starts to count — rendered 「未來日期：YYYY-MM-DD
    起計入」 — or ``None`` when it already counts. ``today`` is the injected app clock, never
    the browser's, so the badge and the figures it explains are cut on the same day; the
    predicate is the valuation cut's own (``shared.models.ledger.pending_from``)."""
    pending = pending_from(counts_on, today)
    return pending.isoformat() if pending is not None else None


@router.get("/ledgers/transactions")
def transactions(
    account_id: str | None = None, symbol: str | None = None,
    frm: str | None = Query(None, alias="from"), to: str | None = None,
    limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    bad = _check_dates(frm, to)
    if bad is not None:
        return bad
    today = now.date()
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
            # DEF-013 (2026-09-24): the 當沖 flag is a persisted column (audit MED-1), so the
            # row's 「當沖」 badge reads the row itself — not the fee snapshot, which a
            # supplied fee/tax leaves without a rate — and the edit modal can preserve it.
            "daytrade": t.daytrade,
            # DEF-056: set only while the trade date is still ahead — it does not count yet.
            "counts_from": _counts_from(t.trade_date, today),
        })
    return _page(out, limit, offset)


@router.get("/ledgers/dividends")
def dividends(
    account_id: str | None = None, symbol: str | None = None,
    frm: str | None = Query(None, alias="from"), to: str | None = None,
    limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    bad = _check_dates(frm, to)
    if bad is not None:
        return bad
    today = now.date()
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
            # R6: the ex-dividend date. ``date`` above is the PAYMENT date; only a STOCK
            # dividend's replay uses this one (Dividend.effective_date). None on every
            # pre-R6 row — never guessed.
            "ex_date": d.ex_date.isoformat() if d.ex_date is not None else None,
            "ccy": ccys.get(d.symbol, ""),
            # DEF-016 / DEF-056: a dividend counts from its EFFECTIVE date — the payment
            # date, or a 配股's ex-date — the same date the valuation cut reads.
            "counts_from": _counts_from(
                dividend_effective_date(d.type, d.date, d.ex_date), today),
        })
    return _page(out, limit, offset)


@router.get("/ledgers/fx")
def fx(
    account_id: str | None = None,
    frm: str | None = Query(None, alias="from"), to: str | None = None,
    limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    bad = _check_dates(frm, to)
    if bad is not None:
        return bad
    today = now.date()
    accts, _names_map, _ccys = _names(conn)
    out: list[dict[str, Any]] = []
    for c in list_fx_conversions(conn, account_id=account_id):
        if not _in_range(c.date, frm, to):
            continue
        quote = c.implied_quote
        out.append({
            "id": c.id, "date": c.date.isoformat(), "account_id": c.account_id,
            "account": accts.get(c.account_id, c.account_id),
            "from_ccy": c.from_ccy.value, "from_amt": decimal_str(c.from_amount),
            "to_ccy": c.to_ccy.value, "to_amt": decimal_str(c.to_amount),
            # None when nothing was received (QA-10): `decimal_str` takes a Decimal, and
            # `web/format.js`'s `f.rate(null)` already renders 「—」.
            # Quoted the conventional way (L6, 2026-09-16): `implied_rate` is ≥ 1 and the two
            # ccy fields say which way — 「1 implied_unit_ccy = implied_rate implied_per_ccy」.
            # The from/to-bound figure is `StoredFxConversion.implied_rate`; the wire carries
            # the quote, so a USD→MYR row and a MYR→USD row print the same 4.0000.
            "implied_rate": (decimal_str(quote[2]) if quote is not None else None),
            "implied_unit_ccy": (quote[0].value if quote is not None else None),
            "implied_per_ccy": (quote[1].value if quote is not None else None),
            "counts_from": _counts_from(c.date, today),   # DEF-056
        })
    return _page(out, limit, offset)


@router.get("/ledgers/cash")
def cash_movements(
    account_id: str | None = None,
    frm: str | None = Query(None, alias="from"), to: str | None = None,
    limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    """The 6th ledger's page view (2026-08-16).

    Cash movements have been writable through ``/api/cash/movements`` and importable as a CSV
    kind since 2026-08-13, and readable **only** through the cash page's balance view — the
    ledger page listed five of the six. This route is the sixth tab's source, in the same
    shape as its neighbours (account/date filter, ``_page`` envelope) so the pager, the
    filter bar and the CSV export button work on it without a special case.

    ``signed_amount`` is computed here rather than on the client: amounts are stored unsigned
    with the direction living in the kind (``shared/cash_kinds.py``), and ``web/`` may not
    compute money. Sending only the raw amount would leave the frontend to either print a fee
    as a positive number or re-derive the sign from a kind table it would then own a copy of.
    """
    bad = _check_dates(frm, to)
    if bad is not None:
        return bad
    today = now.date()
    accts, _names_map, _ccys = _names(conn)
    out: list[dict[str, Any]] = []
    for m in list_cash_movements(conn, account_id=account_id):
        if not _in_range(m.date, frm, to):
            continue
        out.append({
            "id": m.id, "date": m.date.isoformat(), "account_id": m.account_id,
            "account": accts.get(m.account_id, m.account_id),
            "kind": m.kind, "kind_label": CASH_KIND_ZH.get(m.kind.upper(), m.kind),
            "ccy": m.ccy.value,
            "amount": decimal_str(m.amount),
            "signed_amount": decimal_str(m.amount * movement_sign(m.kind)),
            "acq_home_amount": (None if m.acq_home_amount is None
                                else decimal_str(m.acq_home_amount)),
            "note": m.note or "",
            # DEF-056: the pool counts a movement from its own date (M5-06), so a movement
            # dated ahead carries the same 「未來日期」 flag as the other five ledgers.
            "counts_from": _counts_from(m.date, today),
        })
    return _page(out, limit, offset)


@router.get("/ledgers/openings")
def openings(
    account_id: str | None = None, symbol: str | None = None,
    limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    today = now.date()
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
            "counts_from": _counts_from(o.build_date, today),   # DEF-056
        })
    paged = _page(out, limit, offset)
    for i, row in enumerate(paged["rows"], start=1):
        row["id"] = i  # openings has no DB id; synthetic 1-based display key
    return paged


# ---------------------------------------------------------------------------
# Row corrections: edit / delete (2026-07-02)
# ---------------------------------------------------------------------------


# The replay guard (``_replay_block`` / ``_replay_guard`` and their helpers) lives in
# ``api/replay_guard.py`` since DEF-049 — imported at the top under the same names.


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
            "validation_error", unknown_account_message(account_id), field="account_id"))
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
                    f"{symbol} 屬 {inst.market.value} 市場，"
                    f"不可登錄於 {MARKET_ZH.get(acct_mkt, acct_mkt.value)}帳戶",
                    field="symbol"))
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
    # (preservation via None is the contract; the edit modal sends it since DEF-013).
    daytrade: bool | None = None
    # DEF-013 (2026-09-24): the 放空 declaration, correctable here like 當沖. None = preserve.
    # Un-declaring a short that exceeds holdings turns it into a 賣超, which the replay guard
    # below answers with the ordinary 422 ``oversell`` + ack.
    short_sale: bool | None = None


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
    now: datetime = Depends(get_now),
) -> Any:
    """Correct one transaction row — through the SAME ``validate_transaction`` a new entry
    runs (DEF-042, owner ruling 2026-09-24).

    Until DEF-042 this door ran only its own field guard and the replay guard, so a date
    moved before the position's opening build date (DEF-014), into the future, or a sell
    moved onto a day it was not covered all saved without the findings the entry door
    raises. Now the replacement is validated as "the ledger without this row + the edited
    row" (``validate_transaction(replacing=...)``), and the outcome mirrors
    ``POST /api/input/manual/commit`` exactly:

    * a HARD finding the edit introduces → 400 with the issue list (a legacy row's OWN hard
      condition stays correctable — see ``validate_transaction``);
    * the edited sell itself exceeding its holdings → 422 ``oversell`` until
      ``ack_oversell``, alongside the replay guard's scoping of every OTHER row it strands;
    * soft warnings and advisories are acknowledged in the edit modal, the same UI contract
      as the entry door (whose server gates only the two above), and are returned in the
      200 body so a caller of this route sees what it saved over.
    """
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
    # None on the wire = preserve the stored flag (MED-1 for 當沖, DEF-013 for 放空).
    effective_daytrade = body.daytrade if body.daytrade is not None else existing.daytrade
    effective_short = body.short_sale if body.short_sale is not None else existing.short_sale
    resolved = _recompute_edit_fees(conn, body, existing, effective_daytrade)
    if isinstance(resolved, JSONResponse):
        return resolved
    fee, tax, snapshot = resolved
    side = parse_side(body.side)
    edited = existing.model_copy(update={
        "account_id": body.account_id, "symbol": body.symbol,
        "side": side, "quantity": body.shares, "price": body.price,
        "fees": fee, "tax": tax, "trade_date": body.date, "note": body.note,
        "daytrade": effective_daytrade,
        # A 放空 declaration only exists on a sell; an edit that turns the row into a buy
        # drops it rather than storing a flag the replay would then read on a buy.
        "short_sale": effective_short and side is Side.SELL,
    })
    findings = validate_transaction(
        conn,
        TxnInput(account_id=edited.account_id, symbol=edited.symbol, side=edited.side,
                 quantity=edited.quantity, price=edited.price, trade_date=edited.trade_date,
                 fee=fee, tax=tax, daytrade=edited.daytrade, short_sale=edited.short_sale,
                 note=edited.note),
        today=now.date(), replacing=existing)
    hard = [i for i in findings if not i.needs_confirm]
    if hard:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", hard[0].message,
            issues=[issue_wire(i) for i in findings]))
    would_be = [edited if t.id == txn_id else t for t in list_transactions(conn)]
    blocked = _replay_guard(conn, ack_oversell=body.ack_oversell, txs=would_be)
    if blocked is not None:
        return blocked
    own = next((i for i in findings if i.kind == "sell_exceeds_holdings"), None)
    if own is not None and not body.ack_oversell:
        # The row's OWN shortfall, which the replay guard may not report when the position
        # was already flagged — the entry door answers it with the same 422, so this does.
        return _oversell_response(own.message)
    update_transaction(
        conn, txn_id, account_id=body.account_id, symbol=body.symbol,
        side=side, quantity=body.shares, price=body.price,
        fees=fee, tax=tax, trade_date=body.date, daytrade=effective_daytrade,
        note=body.note, fee_rule_snapshot=snapshot, short_sale=edited.short_sale,
    )
    return {"ok": True, "id": txn_id, "fee": decimal_str(fee), "tax": decimal_str(tax),
            "issues": [issue_wire(i) for i in findings]}


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


class DivPreviewBody(BaseModel):
    """The dividend edit dialog's live preview: the correction as it stands, and the row it
    replaces (the transaction dialog's ``replaces_txn_id``, DEF-042)."""

    replaces_div_id: int
    account_id: str
    symbol: str
    date: date
    type: str
    gross: Decimal
    withhold: Decimal | None = None
    net: Decimal | None = None
    reinvest_shares: Decimal | None = None
    reinvest_price: Decimal | None = None


def _dividend_input(body: "DivEditBody | DivPreviewBody",
                    existing: StoredDividend) -> DividendInput:
    """A correction body as the ONE validator's input — blanks stay ``None`` (not stated)."""
    return DividendInput(
        account_id=body.account_id, symbol=body.symbol, div_date=body.date, type=body.type,
        gross=body.gross, withholding=body.withhold, net=body.net,
        reinvest_shares=body.reinvest_shares, reinvest_price=body.reinvest_price,
        ex_date=existing.ex_date)


@router.post("/ledgers/dividends/preview")
def preview_dividend_edit(
    body: DivPreviewBody, conn: sqlite3.Connection = Depends(get_conn)
) -> Any:
    """What saving this dividend correction WOULD report — the same ``validate_dividend``
    (with ``replacing=``) the PUT runs, computed and never written (DEF-061). The edit dialog
    shows it live and holds 儲存 until every soft finding is ticked: the ⑪a contract (the
    server refuses hard findings, the screen gates soft ones), as the trade dialog does."""
    existing = get_dividend(conn, body.replaces_div_id)
    if existing is None:
        return JSONResponse(status_code=404, content=error_body(
            "not_found", f"股利 #{body.replaces_div_id} 不存在"))
    findings = validate_dividend(conn, _dividend_input(body, existing), replacing=existing)
    return {"issues": [issue_wire(i) for i in findings]}


class DivEditBody(BaseModel):
    """A dividend correction. ``withhold`` / ``net`` / ``reinvest_shares`` may be ``None`` —
    "not stated", exactly like a blank CSV cell at the entry door (DEF-061): the dividend
    model then derives them, and ``validate_dividend`` raises the same findings the entry
    door raises for the same blank (a US cash dividend with no withholding stated)."""

    account_id: str
    symbol: str
    date: date
    type: str
    gross: Decimal
    withhold: Decimal | None = None
    net: Decimal | None = None
    reinvest_shares: Decimal | None = None
    reinvest_price: Decimal | None = None
    ack_oversell: bool = False


@router.put("/ledgers/dividends/{div_id}")
def edit_dividend(
    div_id: int,
    body: DivEditBody,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    """Correct one dividend row — through the SAME ``validate_dividend`` every entry door
    runs (DEF-061, owner ruling 2026-09-24; the dividend twin of DEF-042).

    Until DEF-061 this door checked the type string and amount conservation only, so a
    correction could store what the entry door refuses (a NET row with a withholding, a
    DRIP / STOCK row without its share count — which then broke every rebuild — or a type
    the account's model does not book). Now, mirroring ``edit_transaction``:

    * ``_mutation_guard`` first — this door's own structural rule (the row stays keyed to a
      known account and a REGISTERED instrument; market coherence when it is re-keyed);
    * a HARD finding the edit introduces → 400 with the full issue list (a legacy row's OWN
      hard condition stays correctable — ``validate_dividend(replacing=)``);
    * the replay guard (賣超 the edit would strand elsewhere);
    * the amounts STORED are the dividend model's — the entry door's ``apply_dividend_model``
      on the same input — so a blank withholding / net / share count is derived exactly as
      the entry door derives it (a DRIP given only a reinvest price gets its share count);
    * soft findings are returned in the 200 body and shown by the edit dialog (the ⑪a
      contract: the server gates hard findings, the screen gates soft ones).
    """
    existing = get_dividend(conn, div_id)
    if existing is None:
        return JSONResponse(status_code=404,
                            content=error_body("not_found", f"股利 #{div_id} 不存在"))
    guard = _mutation_guard(
        conn, account_id=body.account_id, symbol=body.symbol,
        prev_account_id=existing.account_id, prev_symbol=existing.symbol)
    if guard is not None:
        return guard
    findings = validate_dividend(conn, _dividend_input(body, existing), replacing=existing)
    hard = [i for i in findings if not i.needs_confirm]
    if hard:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", hard[0].message,
            issues=[issue_wire(i) for i in findings]))
    div_type = body.type.strip().upper()
    amounts = apply_dividend_model(
        div_type, gross=body.gross, withholding=body.withhold, net=body.net,
        reinvest_shares=body.reinvest_shares, reinvest_price=body.reinvest_price)
    edited = existing.model_copy(update={
        "account_id": body.account_id, "symbol": body.symbol, "date": body.date,
        "type": div_type, "gross": amounts.gross, "withholding": amounts.withholding,
        "net": amounts.net, "reinvest_shares": amounts.reinvest_shares,
        "reinvest_price": amounts.reinvest_price,
    })
    would_be = [edited if d.id == div_id else d for d in list_dividends(conn)]
    blocked = _replay_guard(conn, ack_oversell=body.ack_oversell, divs=would_be)
    if blocked is not None:
        return blocked
    update_dividend(
        conn, div_id, account_id=body.account_id, symbol=body.symbol,
        div_date=body.date, div_type=div_type, gross=amounts.gross,
        withholding=amounts.withholding, net=amounts.net,
        reinvest_shares=amounts.reinvest_shares, reinvest_price=amounts.reinvest_price,
    )
    return {"ok": True, "id": div_id, "issues": [issue_wire(i) for i in findings]}


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
    """Correct one fx_conversions row — through the SAME guard ``POST /api/cash/fx`` runs.

    Until 2026-08-29 (QA-10) this door validated only ``> 0`` and ``from_ccy != to_ccy``,
    while the entry door enforced currency↔account coherence (audit C2) and the hard
    no-overdraft rule (FU-D34). Two doors onto one ledger with two different rule sets is the
    C3 asymmetry stated the other way round, and the weaker one is reachable from the 交易帳本
    換匯 row, whose currency selects and amount inputs are free-form.
    """
    existing = get_fx_conversion(conn, fx_id)
    if existing is None:
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
    # ``exclude_fx_id``: the edited row's own prior effect is stripped from the pool first, so
    # a correction WITHIN the headroom the old amounts already consumed is not falsely
    # blocked — the ``exclude_id`` pattern ``edit_movement`` proved on the movement door.
    bad = fx_change_guard(
        conn, account_id=body.account_id, on=body.date, from_ccy=body.from_ccy,
        from_amt=body.from_amt, to_ccy=body.to_ccy, to_amt=body.to_amt,
        exclude_fx_id=fx_id)
    if bad is not None:
        return bad
    update_fx_conversion(
        conn, fx_id, account_id=body.account_id, date=body.date,
        from_ccy=body.from_ccy, from_amount=body.from_amt,
        to_ccy=body.to_ccy, to_amount=body.to_amt,
    )
    return {"ok": True, "id": fx_id}


@router.delete("/ledgers/fx/{fx_id}")
def remove_fx(
    fx_id: int,
    ack_negative: bool = False,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    """Delete one fx_conversions row, with the ack-able ``negative_cash`` guard (QA-10).

    Deleting a conversion removes the credit that funded whatever came after it, so the
    to-pool can drop below zero at some point in time — the same event ``remove_movement``
    has answered 422 ``negative_cash`` for since audit C3, on the control beside this one.
    ``ack_negative=true`` still deletes: this is a correction door, and a negative pool is a
    data problem to be fixed rather than a rule to be enforced.
    """
    existing = get_fx_conversion(conn, fx_id)
    if existing is None:
        return JSONResponse(status_code=404,
                            content=error_body("not_found", f"換匯 #{fx_id} 不存在"))
    blocked = fx_delete_guard(conn, existing, ack_negative=ack_negative)
    if blocked is not None:
        return blocked
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
            "not_found", f"期初 {account_ref(account_id)}／{symbol} 不存在"))
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
            "not_found", f"期初 {account_ref(account_id)}／{symbol} 不存在"))
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

    W6 (AI-D27): the restatement also invalidates the symbol's DERIVED signal rows, and
    that delete happens BEFORE the prices probe — it must run even in a prices-less DB
    (both stores degrade on a missing table). ``signal_history``: every stored evaluation
    is wrong under the new basis (the next scan's replay rebuilds it). ``signal_states``:
    the stale comparison row would diff against a post-restatement evaluation on the next
    scan and fire PHANTOM transition events into ``alert_events`` — a basis restatement is
    not a market event; with the row gone the next scan silently reseeds, zero events.
    """
    wanted = sorted({s for s in symbols if s})
    if not wanted:
        return 0
    for s in wanted:
        signal_history.delete_symbol(conn, s)
        signal_states.delete_symbol(conn, s)
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
    #: DEF-049 — PUT only: the owner confirmed the 賣超 the edited ledger would create (the
    #: replay guard's ack, the same flag the transaction edit carries). Ignored by the POST.
    ack_oversell: bool = False
    #: D48b — the SPINOFF child's first price, on the action date. A string for the same
    #: reason the ratio terms are: a malformed one gets this module's zh rejection, not
    #: pydantic's English one. Optional; blank means "wait for the next quote refresh".
    to_symbol_price: str | None = None
    #: DEF-020 — the reorganisation fee (§3.3 / D12), booked as a WITHDRAW cash movement on
    #: the submitting account IN THE SAME TRANSACTION as the action rows and linked to them
    #: (``cash_movements.corporate_action_id``). The form used to post it as a second
    #: request after the action had committed, which is the two-writer shape that left an
    #: action with no fee on a 502 and a fee with no action on a delete. A string for the
    #: same reason the ratio terms are. ``None`` / blank / ``"0"`` = no fee. On a PUT,
    #: ``None`` means "leave the linked fee as it is" and a value (or blank) SYNCS it.
    reorg_fee: str | None = None
    #: The fee's currency; defaults to the source instrument's quote currency.
    reorg_fee_ccy: str | None = None


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


def _reorg_fee_input(
    conn: sqlite3.Connection, body: ActionBody, *, inp: CorporateActionInput,
    default_ccy: Currency | None = None,
) -> CashMovementInput | None | JSONResponse:
    """DEF-020: the reorganisation fee as the shared cash validator's input, or ``None``
    when the body carries no fee (absent, blank or zero), or a zh 400.

    The fee is a WITHDRAW on the SUBMITTING account, dated the action day, in the source
    instrument's quote currency unless the body names one — the same row the form used to
    post to ``POST /api/cash/movements`` on its own, now built here so the action route can
    run the SAME guard that door runs (``validate_cash_movement``: kind, currency coherence,
    the date-aware overdraft block) before either row is written.
    """
    text = (body.reorg_fee or "").strip()
    if not text:
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", f"重組費用必須是數字（目前是「{text}」）",
            field="reorg_fee"))
    if amount == _ZERO:
        return None
    if amount < _ZERO:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", f"重組費用不可為負數（目前是 {text}）", field="reorg_fee"))
    ccy_text = (body.reorg_fee_ccy or "").strip().upper()
    if ccy_text:
        try:
            ccy = Currency(ccy_text)
        except ValueError:
            return JSONResponse(status_code=400, content=error_body(
                "validation_error", f"重組費用的幣別無法辨識：{ccy_text}",
                field="reorg_fee_ccy"))
    elif default_ccy is not None:
        ccy = default_ccy
    else:
        inst = get_instrument(conn, inp.from_symbol)
        if inst is None:
            return JSONResponse(status_code=400, content=error_body(
                "validation_error",
                f"無法決定重組費用的幣別：{inp.from_symbol} 未註冊，請指定幣別",
                field="reorg_fee_ccy"))
        ccy = inst.quote_ccy
    return CashMovementInput(
        account_id=inp.account_id, date=inp.date, kind=CashKind.WITHDRAW.value,
        ccy=ccy, amount=amount, note=f"重組費用 {inp.from_symbol} {inp.date.isoformat()}")


def _reorg_fee_refusal(
    conn: sqlite3.Connection, fee: CashMovementInput, *, exclude_id: int | None = None
) -> JSONResponse | None:
    """Run the shared cash-movement guard on the fee; the first HARD issue as a refusal
    that names BOTH consequences — no fee, and no action either — because the two are one
    write now. Same status mapping as ``cash.py::_movement_error`` (422 for the overdraft
    guard, which the frontend must not offer an ack for; 400 otherwise)."""
    issues = validate_cash_movement(
        conn, fee, pool=cash_pool_fn(conn), exclude_id=exclude_id,
        accounts={a.account_id: a for a in list_accounts(conn)})
    hard = next((i for i in issues if not i.needs_confirm), None)
    if hard is None:
        return None
    status = 422 if hard.kind == "withdraw_insufficient_balance" else 400
    code = hard.kind if status == 422 else "validation_error"
    return JSONResponse(status_code=status, content=error_body(
        code, f"重組費用無法登錄，公司行動也未寫入：{hard.message}", field="reorg_fee"))


def _fee_wire(m: StoredCashMovement) -> dict[str, Any]:
    """One linked reorganisation-fee movement on the wire — money as a Decimal STRING."""
    return {
        "movement_id": m.id, "account_id": m.account_id, "date": m.date.isoformat(),
        "kind": m.kind, "kind_label": CASH_KIND_ZH.get(m.kind, m.kind),
        "ccy": m.ccy.value, "amount": decimal_str(m.amount), "note": m.note,
    }


def _band_restore_wire(verdict: BandRestoreVerdict | None) -> dict[str, Any] | None:
    """DEF-021's outcome (or promise) on the wire: ``restorable`` is the predicate the
    delete confirm quotes, ``restored`` is what the delete actually did."""
    if verdict is None:
        return None
    return {
        "restorable": verdict.restorable,
        "restored": verdict.restored,
        "reason": verdict.reason,
        "band": _band_moved_wire(verdict.band),
    }


def _unapplied_index(conn: sqlite3.Connection) -> dict[tuple[str, str, str, str, str], str]:
    """DEF-023: the corporate-action rows the REPLAY refuses, keyed the way the ledger
    stores them, so the ledger page can mark 「未套用」 on the row itself.

    Read off ``Book.unapplied_actions`` — the same channel the dashboard's XIRR gate and
    the drawer footer read — through the dashboard path (``allow_oversell=True``), so the
    ledger tab marks exactly the rows those two surfaces complain about. Keyed by the
    tuple rather than an id because :class:`UnappliedAction` carries no row id (the replay
    reads converted rows); a hand-duplicated row therefore marks both copies, which is the
    honest answer. Degrades to "no marks" when the ledger cannot be replayed at all: this
    is a list page, and a replay failure must not blank the tab that exists to fix it.
    """
    try:
        book = build_book(load_ledger_bundle(conn), allow_oversell=True)
    except (ValueError, KeyError):
        return {}
    return {
        (u.account_id, u.date.isoformat(), str(u.kind).strip().upper(),
         u.from_symbol, u.to_symbol): u.reason
        for u in book.unapplied_actions
    }


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
        return {"fix_blocked": f"改記為「分割」後仍無法存檔：{hard[0].message}{tail}"}
    # Every key here is consumed by the shared form, and a contract test asserts exactly
    # that: an unread field is a repair half-wired, which is the defect this whole surface
    # exists to close. The finding's own `code` identifies WHICH repair this is — the
    # payload does not repeat it.
    return {"fix": {
        "label": "改記為分割（SPLIT）",
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
            f"目前的帳本無法重播，因此無法試算這筆公司行動（{exc}）。請先修正帳本紀錄"))

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
        "kind_label": KIND_ZH.get(kind, kind),
        "accounts": accounts_wire,
        "not_affected": [{"account_id": a, "account": accts.get(a, a),
                          "reason": "部位在行動日之後才建立，這筆行動不會套用"}
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
        # DEF-040 R4: said BEFORE saving, like `band_move` — the typed child price will NOT
        # be written, because the (child, day) slot already holds a row. The same plan the
        # save records, so the notice cannot disagree with what saving does.
        "child_price_skip": _seed_skip_wire(
            _plan_child_seed(conn, batch.rows[0], body.to_symbol_price)[1]),
    }


@router.get("/ledgers/corporate-actions")
def corporate_actions(
    account_id: str | None = None, symbol: str | None = None,
    frm: str | None = Query(None, alias="from"), to: str | None = None,
    limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    bad = _check_dates(frm, to)
    if bad is not None:
        return bad
    today = now.date()
    accts, names, ccys = _names(conn)
    # DEF-020 / DEF-021 / DEF-023: what leaves with the row, whether its band comes back,
    # and whether the replay applies it — read ONCE per page, not per row.
    fees_by_action: dict[int, StoredCashMovement] = {}
    for m in list_cash_movements(conn):
        if m.corporate_action_id is not None:
            fees_by_action.setdefault(m.corporate_action_id, m)
    unapplied = _unapplied_index(conn)
    # DEF-040: the seed verdict is a property of the SET (one event, one child, one day) —
    # a single row of a multi-account set cannot be deleted alone (F-32), so the promise is
    # computed over the set the delete will actually take.
    sets: dict[tuple[str, date, str], list[StoredCorporateAction]] = {}
    for x in list_corporate_actions(conn):
        sets.setdefault((x.from_symbol, x.date, x.kind), []).append(x)
    out: list[dict[str, Any]] = []
    for a in list_corporate_actions(conn, account_id=account_id, symbol=symbol):
        if not _in_range(a.date, frm, to):
            continue
        fee = fees_by_action.get(a.id)
        reason = unapplied.get((a.account_id, a.date.isoformat(),
                                a.kind.strip().upper(), a.from_symbol, a.to_symbol))
        out.append({
            "id": a.id, "date": a.date.isoformat(), "account_id": a.account_id,
            "account": accts.get(a.account_id, a.account_id),
            # `symbol` (not `from_symbol`) so the page's shared keyword filter and the
            # symbol-cell renderer work on this table with no per-tab special case.
            "symbol": a.from_symbol, "name": names.get(a.from_symbol, ""),
            "to_symbol": a.to_symbol, "to_name": names.get(a.to_symbol, ""),
            "kind": a.kind, "kind_label": KIND_ZH.get(a.kind, a.kind),
            "ratio_to": decimal_str(a.ratio_to),
            "ratio_from": decimal_str(a.ratio_from),
            # Rendered server-side in the owner's own phrasing (§6.7) so the two integer
            # terms never have to be recombined in the browser.
            "ratio_label": (f"每 {decimal_str(a.ratio_from)} 股 → "
                            f"{decimal_str(a.ratio_to)} 股"),
            "cost_carry": (decimal_str(a.cost_carry)
                           if a.cost_carry is not None else None),
            "note": a.note, "ccy": ccys.get(a.from_symbol, ""),
            # DEF-020: the linked reorganisation fee, so the delete confirm can say what
            # goes with the row (amount + currency) BEFORE the owner confirms.
            "reorg_fee": _fee_wire(fee) if fee is not None else None,
            # DEF-021: what this EXCHANGE moved, and whether deleting it moves it back —
            # the same predicate the delete runs, quoted beforehand.
            "band_move": _band_moved_wire(a.band_move),
            "band_restore": _band_restore_wire(pending_band_restore(conn, a.band_move)),
            # I-6 (F-3's list half): the same record and promise for the target WEIGHT, so
            # the delete confirm can say beforehand what the delete will do to both settings
            # — until now the weight's verdict appeared only in the delete RESPONSE.
            "weight_move": _weight_moved_wire(a.weight_move),
            "weight_restore": _weight_restore_wire(
                pending_weight_restore(conn, a.weight_move)),
            # DEF-040: whether deleting this SPINOFF takes the child's seed price with it —
            # the same predicate the delete runs, quoted beforehand (null on other kinds, and
            # when the child has no price row on the action day at all).
            "child_price_restore": _seed_wire(_pending_seed(
                conn, sets.get((a.from_symbol, a.date, a.kind), [a]))),
            # DEF-023: the replay's refusal, on the row that caused it.
            "unapplied": {"reason": reason} if reason is not None else None,
            # DEF-056: a corporate action dated after the server's valuation day is cut
            # from every valuation until then — said on the row, as on the other ledgers.
            "counts_from": _counts_from(a.date, today),
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
            f"目前的帳本無法重播，因此無法登錄公司行動（{exc}）。請先修正帳本紀錄"))
    # D48b: the optional child price is refused LOUDLY rather than dropped. A value the
    # owner typed that silently does not arrive is the failure mode this whole module is
    # written against — and here it would be invisible, because the field's only effect is
    # a price row nobody looks at until the XIRR stays dark.
    if (bad := _child_price_refusal(batch.rows[0].kind, body.to_symbol_price)) is not None:
        return bad
    # DEF-040 R4: what the seed WILL be, decided BEFORE anything is written — through the
    # same slot predicate the write re-checks — and recorded on every row of the set, so
    # the delete takes back exactly this and never a row that was there before the save.
    seed_plan, seed_skip = _plan_child_seed(conn, batch.rows[0], body.to_symbol_price)
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
    # DEF-020: the reorganisation fee is validated BEFORE anything is written, through the
    # same guard the cash door runs, so a refused fee refuses the whole request — no action
    # without its fee, no fee without its action.
    submitting = next((r for r in batch.rows if r.account_id == body.account_id),
                      batch.rows[0])
    fee = _reorg_fee_input(conn, body, inp=submitting)
    if isinstance(fee, JSONResponse):
        return fee
    if fee is not None and (refused := _reorg_fee_refusal(conn, fee)) is not None:
        return refused
    is_exchange = batch.rows[0].kind.strip().upper() == CorporateActionKind.EXCHANGE.value
    # DEF-021: what the band move WILL do, read through the same predicate the move uses,
    # recorded on every row of the set so the delete can offer the conditional reversal.
    pending = (
        pending_band_move(conn, from_symbol=batch.rows[0].from_symbol,
                          to_symbol=batch.rows[0].to_symbol)
        if is_exchange
        else None
    )
    # F-3: the same record for the target WEIGHT, through the move's own predicate.
    pending_weight = (
        pending_weight_move(conn, from_symbol=batch.rows[0].from_symbol,
                            to_symbol=batch.rows[0].to_symbol)
        if is_exchange
        else None
    )
    # ALL-OR-NOTHING. The N rows are one event (D13), so a batch that half-lands is the
    # partial state E13 exists to forbid, created by the writer instead of by the owner.
    # Every insert defers its commit and one rollback covers the lot — the fee movement and
    # the band move included (DEF-020 / DEF-021): a band moved onto a symbol whose action
    # then failed to commit would be an alert on a security this ledger does not hold.
    fee_row: StoredCashMovement | None = None
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
                band_move=pending, weight_move=pending_weight, child_seed=seed_plan,
                commit=False)
            for inp in batch.rows
        ]
        # D47: an EXCHANGE re-keys the position, so the owner's alert band follows the
        # ticker — inside the transaction now, so it lands with the rows or not at all.
        moved = (
            move_target_band(conn, from_symbol=batch.rows[0].from_symbol,
                             to_symbol=batch.rows[0].to_symbol, commit=False)
            if is_exchange
            else None
        )
        # The owner's OTHER per-symbol setting follows the ticker too — and inside the SAME
        # transaction since F-3 (DEF-020's class): it used to run after the commit below, so
        # a failure here left an EXCHANGE standing with its weight still on the dead ticker.
        # A target weight is config that nothing recomputes, so that state disagreed with
        # nothing and surfaced only as a rebalance entry that could never be satisfied.
        weight_moved = (
            move_target_weight(conn, from_symbol=batch.rows[0].from_symbol,
                               to_symbol=batch.rows[0].to_symbol, now=now, commit=False)
            if is_exchange
            else None
        )
        if fee is not None:
            owner_id = written[batch.rows.index(submitting)]
            fee_id = insert_cash_movement(
                conn, account_id=fee.account_id, move_date=fee.date, kind=fee.kind,
                ccy=fee.ccy, amount=fee.amount, note=fee.note,
                corporate_action_id=owner_id, commit=False)
            fee_row = StoredCashMovement(
                id=fee_id, account_id=fee.account_id, date=fee.date, kind=fee.kind,
                ccy=fee.ccy, amount=fee.amount, note=fee.note,
                corporate_action_id=owner_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    restated = reconcile_split_prices(
        conn, {body.from_symbol.strip(), body.to_symbol.strip()})
    priced, late_skip = _seed_child_price(conn, seed_plan, now=now)
    return {"ok": True, "written": len(written), "ids": written,
            "accounts": batch.accounts, "prices_restated": restated,
            "band_moved": _band_moved_wire(moved),
            "weight_moved": None if weight_moved is None else decimal_str(weight_moved),
            "child_priced": priced,
            # DEF-040 R4: the typed price was NOT written because the (child, day) slot
            # already held a row — said on the wire so the form's toast can say it too.
            "child_price_skipped": _seed_skip_wire(seed_skip or late_skip),
            # DEF-020: the fee that landed WITH the rows (one request, one transaction).
            "reorg_fee": _fee_wire(fee_row) if fee_row is not None else None,
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
            f"子公司起始價僅適用於分拆，{kind} 不需填寫",
            field="to_symbol_price"))
    try:
        close = Decimal(text)
    except InvalidOperation:
        close = Decimal(0)
    if close <= 0:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error",
            f"子公司起始價必須是正數，目前是 {text}",
            field="to_symbol_price"))
    return None


def _typed_child_price(kind: str, raw: str | None) -> Decimal | None:
    """The SPINOFF child price the owner typed, or ``None`` (blank, another kind, unusable —
    :func:`_child_price_refusal` has already answered the unusable ones with a 400)."""
    text = (raw or "").strip()
    if not text or kind.strip().upper() != CorporateActionKind.SPINOFF.value:
        return None
    try:
        close = Decimal(text)
    except InvalidOperation:
        return None
    return close if close.is_finite() and close > 0 else None


def _plan_child_seed(
    conn: sqlite3.Connection, inp: CorporateActionInput, raw: str | None
) -> tuple[ChildSeedRecord | None, SeedSkip | None]:
    """DEF-040 R4: what a SPINOFF's save will write into ``prices`` — decided BEFORE the
    rows are inserted, so the record lands with them (``corporate_actions.child_seed_json``).

    Returns ``(record, skip)``. ``record`` is ``None`` for every other kind (NULL column);
    for a SPINOFF it is the slot + typed close when the slot is free, or "wrote nothing"
    (``close=None``) when no price was typed or the slot is occupied — then ``skip`` says
    why, through the same :func:`pricing.seed.occupied_slot` the write re-checks. A plan the
    write later cannot honour (a quote landing in between) is harmless: the delete re-reads
    the row, and a provider's row never matches the seed signature.
    """
    if inp.kind.strip().upper() != CorporateActionKind.SPINOFF.value:
        return None, None
    close = _typed_child_price(inp.kind, raw)
    if close is None:
        return ChildSeedRecord(), None
    if (skip := occupied_slot(conn, symbol=inp.to_symbol, on=inp.date)) is not None:
        return ChildSeedRecord(), skip
    return ChildSeedRecord(close=close, symbol=inp.to_symbol, as_of=inp.date), None


def _seed_skip_wire(skip: SeedSkip | None) -> dict[str, Any] | None:
    """DEF-040 R4's refusal on the wire: ``{symbol, date, existing_close, source, reason}`` —
    the existing close is the stored Decimal TEXT verbatim, never re-scaled."""
    if skip is None:
        return None
    return {"symbol": skip.symbol, "date": skip.as_of.isoformat(),
            "existing_close": skip.existing_close, "source": skip.source,
            "reason": skip.reason}


def _seed_child_price(
    conn: sqlite3.Connection, plan: ChildSeedRecord | None, *, now: datetime
) -> tuple[str | None, SeedSkip | None]:
    """D48b: store the SPINOFF child's first price, dated the action day — the *plan*
    :func:`_plan_child_seed` recorded on the rows. Returns ``(symbol priced, None)``, or
    ``(None, skip)`` when the slot turned out to be occupied (DEF-040 R4: a seed never
    overwrites a row), or ``(None, None)`` when there was nothing to write.

    **Why it is worth a field at all.** ``returns.py`` is all-or-nothing on the terminal
    value: ONE unpriced holding makes the WHOLE portfolio's XIRR ``None``, not just that
    symbol's. And a SPINOFF is guaranteed to create an unpriced holding — the child did not
    exist to be quoted. So between saving the action and the next successful refresh, the
    headline return goes dark for a reason nothing on the page explains. The owner reading
    the child's opening price off the same statement can end that in one box.

    **Written through** ``pricing.seed.write_seed_price`` (DEF-040: the one owner of the
    seed's signature, over ``pricing.store.upsert_prices``), from ``api/`` — ``pricing/`` owns
    every write to ``prices`` (``architecture.md``) and ``data_ingestion`` may not reach in.

    **``fetched_at`` is the ACTION DAY, not ``now``** (QA-05). The owner typed an *as-traded*
    price and dated it themselves, so it was "observed" on its own date; stamping it with the
    wall clock made a claim about the row that is not true, and the claim is load-bearing.
    ``pricing/`` reads ``fetched_at`` as the upper bound of the window ``(as_of, fetched_at]``
    — "which splits had the provider already folded into this delivered number?" — and
    multiplies them back OUT (``data-and-pricing.md``, the as-traded invariant). With the row
    dated the action day, ``as_of == fetched_at.date()``, so that window is EMPTY by
    construction, on the write **and on every later**
    ``pricing.reconcile.reconcile_prices`` pass: ``split_basis`` can only ever be the
    identity, and ``factor_of`` may safely stay at ``_no_factor``.

    The earlier justification — 「the child is brand new, so no split can exist between that
    date and now」 — was a derivation from the child's *age*, and it is false for a
    **back-dated** spin-off, which this route fully permits and which backfilling broker
    history is the stated use case for. Measured: a SPINOFF dated 2023-01-15 with a typed
    50.00, then the child's own 2-for-1 SPLIT of 2024-06-01, stored ``close='100.00'`` over
    ``close_raw='50.00'`` / ``split_basis='2'``; the read divided the same split back out to
    50.00 and met a POST-split 100-share count — 5,000 where the economics say 2,500.
    A split changes the denomination, never the value.

    ``now`` is still the tz/precision convention this column is written in everywhere else
    (``upsert_prices`` stores ``fetched_at.isoformat()``), so only the DATE moves; the
    timezone comes from the request's own clock rather than being invented here.

    **Degrades rather than raises.** ``bootstrap_db`` does not create ``prices`` (only
    ``pricing.schema.create_tables`` does), so a ledger-only database has no such table —
    the same condition ``validate._has_prices`` already absorbs. A corporate action must not
    become unrecordable because an optional convenience has nowhere to go.
    """
    if plan is None or plan.close is None or plan.symbol is None or plan.as_of is None:
        return None, None
    inst = get_instrument(conn, plan.symbol)
    if inst is None:
        return None, None
    try:
        # DEF-040: through pricing's ONE owner of the seed signature (source + action-day
        # stamp), so the delete can recognise — and conditionally remove — exactly this row.
        # R4: it writes ONLY into an empty slot and reports the refusal otherwise.
        wrote = write_seed_price(conn, symbol=inst.symbol, market=inst.market,
                                 on=plan.as_of, close=plan.close, tz=now.tzinfo)
    except sqlite3.OperationalError:
        return None, None
    if not wrote.written:
        return None, wrote.skipped
    return inst.symbol, None


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
    action_id: int, body: ActionBody, conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    """Edit one row — re-validated on the way OUT (F-32) and IN, then both ends reconciled.

    E16 / domain-ledger N2: an edit RE-COMPUTES history, nothing is snapshotted. The
    before-image goes to ``ledger_audit`` (the store does that), and the price basis of the
    OLD symbol has to be restated too — otherwise it keeps a basis from an action that no
    longer references it.

    DEF-060 (owner ruling 2026-09-24): a SPINOFF's seed price follows the edit — to the new
    day / child, under DEF-040's rules on both ends (:func:`_move_child_seed`) — and a typed
    ``to_symbol_price`` is applied the same way instead of being dropped in silence.
    """
    existing = get_corporate_action(conn, action_id)
    if existing is None:
        return JSONResponse(status_code=404, content=error_body(
            "not_found", f"公司行動 #{action_id} 不存在"))
    replacement = _action_input(body, body.account_id)
    if isinstance(replacement, JSONResponse):
        return replacement
    # D48b's entry guard, on this door too: a child price typed for the wrong kind, or an
    # unusable one, is refused LOUDLY rather than dropped (the PUT used to ignore the field).
    if (bad := _child_price_refusal(replacement.kind, body.to_symbol_price)) is not None:
        return bad
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
            "ledger_unbookable", f"帳本無法重播，無法修改這筆公司行動（{exc}）"))
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
    # DEF-049: the edited ledger is REPLAYED like every other correction door — a new date,
    # ratio, symbol or kind can strand a later sell (a 10:1 split edited to 2:1 under a sell
    # of 5,000) or a dividend. Same decision as the transaction edit; the ack rides the body.
    replayed = action_change_guard(
        conn, would_be=[_stored_from(replacement, action_id) if a.id == action_id else a
                        for a in list_corporate_actions(conn)],
        ack_oversell=body.ack_oversell, what="此更正", then="再修改這筆公司行動")
    if replayed is not None:
        return replayed
    # DEF-020: the linked fee follows the row. `reorg_fee` absent (None) = leave it alone;
    # blank / "0" = remove it; a value = write it (date and account re-synced to the
    # edited action), validated by the cash guard BEFORE the row is touched, with the
    # existing fee excluded from its own overdraft check.
    linked = linked_cash_movements(conn, action_id)
    current = linked[0] if linked else None
    fee: CashMovementInput | None = None
    if body.reorg_fee is not None:
        built = _reorg_fee_input(
            conn, body, inp=replacement,
            default_ccy=current.ccy if current is not None else None)
        if isinstance(built, JSONResponse):
            return built
        fee = built
        if fee is not None and (refused := _reorg_fee_refusal(
                conn, fee, exclude_id=current.id if current is not None else None)
        ) is not None:
            return refused
    fee_after: StoredCashMovement | None = current
    try:
        update_corporate_action(
            conn, action_id, account_id=replacement.account_id,
            action_date=replacement.date, kind=CorporateActionKind(replacement.kind),
            from_symbol=replacement.from_symbol, to_symbol=replacement.to_symbol,
            ratio_to=replacement.ratio_to, ratio_from=replacement.ratio_from,
            cost_carry=replacement.cost_carry, note=replacement.note, commit=False)
        if body.reorg_fee is not None:
            if fee is None:
                for m in linked:
                    delete_cash_movement(conn, m.id, commit=False)
                fee_after = None
            elif current is not None:
                update_cash_movement(
                    conn, current.id, account_id=fee.account_id, move_date=fee.date,
                    kind=fee.kind, ccy=fee.ccy, amount=fee.amount, note=fee.note,
                    acq_home_amount=current.acq_home_amount, commit=False)
                fee_after = current.model_copy(update={
                    "account_id": fee.account_id, "date": fee.date, "kind": fee.kind,
                    "ccy": fee.ccy, "amount": fee.amount, "note": fee.note})
            else:
                fee_id = insert_cash_movement(
                    conn, account_id=fee.account_id, move_date=fee.date, kind=fee.kind,
                    ccy=fee.ccy, amount=fee.amount, note=fee.note,
                    corporate_action_id=action_id, commit=False)
                fee_after = StoredCashMovement(
                    id=fee_id, account_id=fee.account_id, date=fee.date, kind=fee.kind,
                    ccy=fee.ccy, amount=fee.amount, note=fee.note,
                    corporate_action_id=action_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    # BOTH ends of BOTH shapes — the row may have moved symbol, and the ABANDONED one is
    # the half a symbol-blind reconcile leaves behind holding a basis nothing references.
    restated = reconcile_split_prices(conn, {
        existing.from_symbol, existing.to_symbol,
        replacement.from_symbol, replacement.to_symbol})
    seed = _move_child_seed(conn, existing, replacement, body.to_symbol_price, now=now)
    return {"ok": True, "id": action_id, "prices_restated": restated,
            "reorg_fee": _fee_wire(fee_after) if fee_after is not None else None,
            **seed}


def _move_child_seed(
    conn: sqlite3.Connection, existing: StoredCorporateAction,
    replacement: CorporateActionInput, raw_price: str | None, *, now: datetime,
) -> dict[str, Any]:
    """DEF-060: what an edit does to the SPINOFF child's seed price — the wire fields
    ``child_priced`` / ``child_price_skipped`` / ``child_price_move``.

    The seed belongs to the action's ``(child, day)`` slot. An edit that moves the slot (a
    new date or child), types a new price (``to_symbol_price``), or turns the row into
    another kind runs DEF-040's two halves in order:

    1. **the old slot** — the seed the row OWNS (its record, or R3's signature rule for a
       row saved before the record existed) is removed only while intact, exactly as a
       delete would (:func:`pricing.seed.remove_seed_price`); a seed a quote has replaced,
       or one another row still owns, stays and the reason is returned;
    2. **the new slot** — the moved close (or the typed one) is written only into an EMPTY
       slot (:func:`pricing.seed.write_seed_price`); a quote already there is left alone
       and the refusal is returned, the same sentence the save gives.

    The row's record is then rewritten to what it now owns (``set_child_seed``), so a later
    delete takes back exactly that. An edit that changes none of the three leaves the seed
    — and the record, legacy NULL included — untouched.
    """
    out: dict[str, Any] = {"child_priced": None, "child_price_skipped": None,
                           "child_price_move": None}
    spin = CorporateActionKind.SPINOFF.value
    is_spin = replacement.kind.strip().upper() == spin
    typed = _typed_child_price(replacement.kind, raw_price)
    old = _owned_slot(existing)
    old_slot = None if old is None else (old[0], old[1])
    new_slot = (replacement.to_symbol, replacement.date) if is_spin else None
    if old_slot == new_slot and typed is None:
        return out
    if old is None and new_slot is None:
        return out
    verdict: SeedPriceVerdict | None = None
    if old is not None:
        still_used = any(
            a.id != existing.id and (other := _owned_slot(a)) is not None
            and (other[0], other[1]) == old_slot
            for a in list_corporate_actions(conn, symbol=old[0]))
        verdict = remove_seed_price(conn, symbol=old[0], on=old[1], still_used=still_used,
                                    expected_close=old[2])
    carry = typed if typed is not None else (
        verdict.close if verdict is not None and verdict.removed else None)
    record = ChildSeedRecord()
    skip: SeedSkip | None = None
    unregistered = False
    if new_slot is not None and carry is not None:
        inst = get_instrument(conn, new_slot[0])
        if inst is None:
            unregistered = True
        else:
            try:
                wrote = write_seed_price(conn, symbol=inst.symbol, market=inst.market,
                                         on=new_slot[1], close=carry, tz=now.tzinfo)
            except sqlite3.OperationalError:
                wrote = None
            if wrote is not None and wrote.written:
                record = ChildSeedRecord(close=carry, symbol=inst.symbol, as_of=new_slot[1])
                out["child_priced"] = inst.symbol
            elif wrote is not None:
                skip = wrote.skipped
    out["child_price_skipped"] = _seed_skip_wire(skip)
    set_child_seed(conn, [existing.id], record)
    if old is not None and verdict is not None:
        out["child_price_move"] = _seed_move_wire(
            old_slot=(old[0], old[1]), new_slot=new_slot, verdict=verdict,
            written=record.close, skip=skip, unregistered=unregistered)
    return out


def _seed_move_wire(
    *, old_slot: tuple[str, date], new_slot: tuple[str, date] | None,
    verdict: SeedPriceVerdict, written: Decimal | None, skip: SeedSkip | None,
    unregistered: bool,
) -> dict[str, Any]:
    """DEF-060's outcome for the OLD seed, as one zh sentence the edit toast shows.
    ``moved`` is True only when the seed now sits in the new slot."""
    old_label = f"{old_slot[0]} 在 {old_slot[1].isoformat()}"
    moved = verdict.removed and written is not None
    if moved and new_slot is not None:
        if new_slot == old_slot:
            message = f"{old_label} 的子公司起始價已改為 {decimal_str(written or _ZERO)}"
        else:
            message = (f"子公司起始價 {decimal_str(written or _ZERO)} 已由 {old_label} "
                       f"搬到 {new_slot[0]} 在 {new_slot[1].isoformat()}")
    elif not verdict.removed:
        message = f"子公司起始價未搬移：{verdict.reason or ''}"
    elif new_slot is None:
        message = f"已不是分拆，登錄時寫入的子公司起始價（{old_label}）已移除"
    elif skip is not None:
        message = f"{skip.reason}；原本 {old_label} 的起始價已移除"
    elif unregistered:
        message = (f"新子公司 {new_slot[0]} 尚未註冊，起始價未寫入；"
                   f"原本 {old_label} 的起始價已移除")
    else:
        message = f"原本 {old_label} 的子公司起始價已移除"
    return {
        "from": {"symbol": old_slot[0], "date": old_slot[1].isoformat()},
        "to": (None if new_slot is None
               else {"symbol": new_slot[0], "date": new_slot[1].isoformat()}),
        "moved": moved,
        "removed": verdict.removed,
        "message": message,
    }


@router.delete("/ledgers/corporate-actions/set")
def remove_corporate_action_set(
    from_symbol: str,
    on: str = Query(..., alias="date"),
    kind: str = Query(...),
    ack_oversell: bool = False,
    ack_negative: bool = False,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    """Delete a whole ``(from_symbol, date, kind)`` set — the ONLY way to leave one.

    DEF-049: replayed first, over the ledger without EVERY row of the set, like every other
    ledger delete (``replay_guard.action_change_guard``).

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
            "validation_error", f"日期格式無效：{on}", field="date"))
    rows = [a for a in list_corporate_actions(conn)
            if a.from_symbol == from_symbol and a.date == action_date
            and a.kind == wanted]
    if not rows:
        return JSONResponse(status_code=404, content=error_body(
            "not_found", f"找不到 {from_symbol} 在 {on} 的{KIND_ZH.get(wanted, wanted)}"))
    leaving = {a.id for a in rows}
    replayed = action_change_guard(
        conn, would_be=[a for a in list_corporate_actions(conn) if a.id not in leaving],
        leaving=rows, ack_oversell=ack_oversell, ack_negative=ack_negative,
        what="此刪除", then="再刪除這組公司行動")
    if replayed is not None:
        return replayed
    symbols = {a.from_symbol for a in rows} | {a.to_symbol for a in rows}
    outcome = _delete_actions(conn, rows, now=now)
    restated = reconcile_split_prices(conn, symbols)
    return {"ok": True, "deleted": len(rows), "prices_restated": restated, **outcome}


def _delete_actions(
    conn: sqlite3.Connection, rows: Sequence[StoredCorporateAction], *, now: datetime,
    commit: bool = True,
) -> dict[str, Any]:
    """Delete the rows, their linked fees (DEF-020) and — when the recorded band move is
    still intact at both ends — move the band back (DEF-021), under ONE commit.

    **The one delete path for a corporate-action set** — both ledger delete routes AND the
    import-batch undo (I-3: ``input_center.import_batch_delete`` binds this as
    ``provenance.delete_batch``'s ``delete_actions``) go through here, so an imported EXCHANGE
    undone by batch gives back its band, its weight and its linked fee exactly as the ledger
    tab's 刪除 does. ``commit=False`` hands the transaction to that caller (the batch's other
    tables are deleted in the same one).

    Returns the wire fields both delete routes report: ``fee_deleted`` (every linked
    movement that left with the rows), ``band_restored`` (``True`` / ``False`` / ``None``
    when nothing was recorded) and ``band_restore`` (the full verdict, reason included).
    The set is one event, so its band is restored ONCE, from the first row that recorded
    the move — every row of an API-written set carries the same record, and an imported set
    carries it on the row that performed the move.

    F-3: the target WEIGHT the EXCHANGE re-keyed comes back the same way, under the same
    commit (``weight_restored`` / ``weight_restore``) — once per set, from the first row that
    recorded a weight move.
    """
    fees = [m for a in rows for m in linked_cash_movements(conn, a.id)]
    recorded = next((a.band_move for a in rows if a.band_move is not None), None)
    recorded_weight = next((a.weight_move for a in rows if a.weight_move is not None), None)
    seed = _seed_target(conn, rows)
    try:
        for a in rows:
            delete_corporate_action(conn, a.id, commit=False)
        verdict = restore_target_band(conn, recorded, commit=False)
        weight_verdict = restore_target_weight(conn, recorded_weight, now=now, commit=False)
        # DEF-040: the child's seed price leaves with the SPINOFF that wrote it — through
        # pricing's function, bound here, under the same commit — unless a real quote has
        # replaced it since (or another action still creates the same child that day).
        seed_verdict = (None if seed is None else remove_seed_price(
            conn, symbol=seed.symbol, on=seed.on, still_used=seed.still_used,
            expected_close=seed.expected_close, commit=False))
        if commit:
            conn.commit()
    except Exception:
        if commit:
            conn.rollback()
        raise
    return {
        "fee_deleted": [_fee_wire(m) for m in fees],
        "band_restored": None if verdict is None else verdict.restored,
        "band_restore": _band_restore_wire(verdict),
        "weight_restored": None if weight_verdict is None else weight_verdict.restored,
        "weight_restore": _weight_restore_wire(weight_verdict),
        "child_price_removed": None if seed_verdict is None else seed_verdict.removed,
        "child_price_restore": _seed_wire(seed_verdict),
    }


class _SeedTarget(NamedTuple):
    """The seed a delete (or an edit's move) of a SPINOFF set may take back (DEF-040)."""

    symbol: str
    on: date
    still_used: bool
    #: The close the save RECORDED writing; ``None`` for a row saved before the record
    #: existed (the signature alone then decides — R3's rule).
    expected_close: Decimal | None


def _owned_slot(a: StoredCorporateAction) -> tuple[str, date, Decimal | None] | None:
    """The ``(child, day, expected close)`` seed slot SPINOFF row *a* owns, or ``None``.

    DEF-040 R4: read off the row's own record (``child_seed_json``). A record that says the
    save wrote NOTHING — no price typed, the slot was already occupied, or an import door
    that takes no price — owns nothing, so neither its delete nor its batch undo can take a
    row it never wrote. A row with no record at all (saved before the record existed) falls
    back to R3's rule: its own ``(to_symbol, date)``, signature only.

    The rule itself is :meth:`StoredCorporateAction.owned_seed_slot` (DEF-063): the
    orphan-seed cleanup script asks the same question, so there is one answer, not two.
    """
    return a.owned_seed_slot()


def _seed_target(
    conn: sqlite3.Connection, rows: Sequence[StoredCorporateAction]
) -> _SeedTarget | None:
    """The seed a delete of *rows* may take with it (DEF-040) — ``None`` when *rows* hold no
    SPINOFF, or when their SPINOFF owns no seed (R4: it recorded writing nothing). A set is
    one event, so it has one child and one day; ``still_used`` is True when a SPINOFF that
    is NOT being deleted owns the same slot (the seed then still belongs to that one)."""
    spin = next((a for a in rows
                 if a.kind.strip().upper() == CorporateActionKind.SPINOFF.value), None)
    if spin is None or (slot := _owned_slot(spin)) is None:
        return None
    symbol, on, expected = slot
    leaving = {a.id for a in rows}
    still_used = any(
        a.id not in leaving and (other := _owned_slot(a)) is not None
        and (other[0], other[1]) == (symbol, on)
        for a in list_corporate_actions(conn, symbol=symbol)
    )
    return _SeedTarget(symbol, on, still_used, expected)


def _pending_seed(
    conn: sqlite3.Connection, rows: Sequence[StoredCorporateAction]
) -> SeedPriceVerdict | None:
    """What deleting *rows* WOULD do to the child's seed price — the confirm's read of the
    predicate :func:`_delete_actions` then runs."""
    seed = _seed_target(conn, rows)
    if seed is None:
        return None
    return pending_seed_removal(conn, symbol=seed.symbol, on=seed.on,
                                still_used=seed.still_used,
                                expected_close=seed.expected_close)


def _seed_wire(verdict: SeedPriceVerdict | None) -> dict[str, Any] | None:
    """DEF-040's promise (list) / outcome (delete) on the wire — the band/weight shape."""
    if verdict is None:
        return None
    return {
        "symbol": verdict.symbol,
        "date": verdict.as_of.isoformat(),
        "restorable": verdict.removable,
        "restored": verdict.removed,
        "reason": verdict.reason,
    }


def _weight_moved_wire(moved: MovedWeight | None) -> dict[str, Any] | None:
    """F-3's record on the wire (I-6) — the weight as a Decimal STRING, like ``band_move``'s
    levels; ``None`` on every row that recorded no weight move."""
    if moved is None:
        return None
    return {"from_symbol": moved.from_symbol, "to_symbol": moved.to_symbol,
            "weight": decimal_str(moved.weight)}


def _weight_restore_wire(verdict: WeightRestoreVerdict | None) -> dict[str, Any] | None:
    """F-3's outcome on the wire — the weight as a Decimal STRING (``decimal_str``), like
    ``weight_moved`` on the save; ``None`` when the deleted rows recorded no weight move."""
    if verdict is None:
        return None
    return {
        "from_symbol": verdict.weight.from_symbol,
        "to_symbol": verdict.weight.to_symbol,
        "weight": decimal_str(verdict.weight.weight),
        "restorable": verdict.restorable,
        "restored": verdict.restored,
        "reason": verdict.reason,
    }


@router.delete("/ledgers/corporate-actions/{action_id}")
def remove_corporate_action(
    action_id: int, ack_oversell: bool = False, ack_negative: bool = False,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    """Delete one row — refused when it belongs to a multi-account set (F-32), and replayed
    first like every other ledger delete (DEF-049: ``replay_guard.action_change_guard``)."""
    existing = get_corporate_action(conn, action_id)
    if existing is None:
        return JSONResponse(status_code=404, content=error_body(
            "not_found", f"公司行動 #{action_id} 不存在"))
    blocked = _change_block(validate_corporate_action_change(conn, action_id))
    if blocked is not None:
        return blocked
    replayed = action_change_guard(
        conn, would_be=[a for a in list_corporate_actions(conn) if a.id != action_id],
        leaving=[existing], ack_oversell=ack_oversell, ack_negative=ack_negative,
        what="此刪除", then="再刪除這筆公司行動")
    if replayed is not None:
        return replayed
    outcome = _delete_actions(conn, [existing], now=now)
    restated = reconcile_split_prices(conn, {existing.from_symbol, existing.to_symbol})
    return {"ok": True, "id": action_id, "prices_restated": restated, **outcome}
