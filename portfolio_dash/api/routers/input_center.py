"""Input center API (spec 12): read context + manual/CSV/AI write paths (12a: context+manual)."""

import base64
import binascii
import re
import sqlite3
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from portfolio_dash.api.deps import get_conn, get_now
from portfolio_dash.api.errors import error_body
from portfolio_dash.api.instrument_service import QuickRegisterError, quick_register
from portfolio_dash.api.routers.cash import cash_pool_fn
from portfolio_dash.api.routers.ledgers import _to_models, reconcile_split_prices
from portfolio_dash.api.wire import (
    account_markets_wire,
    div_model_wire,
    fee_rules_wire,
    issue_wire,
    parse_side,
)
from portfolio_dash.data_ingestion.agents import ai_agents_input
from portfolio_dash.data_ingestion.cash_import import (
    build_cash_movement_preview,
    write_cash_movement_row,
)
from portfolio_dash.data_ingestion.config_seed import FeeRuleSet, get_fee_rule_set
from portfolio_dash.data_ingestion.corporate_action_import import (
    build_corporate_action_preview,
    parse_action_batch,
    write_corporate_action_row,
)
from portfolio_dash.data_ingestion.csv_import import (
    DateAmbiguity,
    build_transaction_preview,
    normalize_import_csv,
    write_transaction_row,
)
from portfolio_dash.data_ingestion.dateparse import FORMAT_IDS
from portfolio_dash.data_ingestion.dividend_import import (
    build_dividend_preview,
    write_dividend_row,
)
from portfolio_dash.data_ingestion.fees import forecast_tw_rebate
from portfolio_dash.data_ingestion.fx_import import build_fx_preview, write_fx_row
from portfolio_dash.data_ingestion.holdings import current_shares, load_action_index
from portfolio_dash.data_ingestion.import_templates import (
    DATE_COLUMN_BY_KIND,
    TEMPLATE_KINDS,
    render_import_template,
    template_filename,
)
from portfolio_dash.data_ingestion.manual import enter_transaction
from portfolio_dash.data_ingestion.markets import account_market as _account_market
from portfolio_dash.data_ingestion.opening_import import (
    build_opening_preview,
    write_opening_row,
)
from portfolio_dash.data_ingestion.preview import (
    BatchContext,
    ImportPreview,
    PreviewRow,
    commit_preview,
)
from portfolio_dash.data_ingestion.provenance import (
    delete_batch,
    existing_hashes,
    open_batch,
    row_hashes,
)
from portfolio_dash.data_ingestion.rules_binding import allowed_markets, fee_rule_for
from portfolio_dash.data_ingestion.store import (
    list_accounts,
    list_cash_movements,
    list_dividends,
    list_fx_conversions,
    list_instruments,
    list_transactions,
)
from portfolio_dash.data_ingestion.validate import Issue, TxnInput
from portfolio_dash.export.artifact import content_disposition
from portfolio_dash.portfolio.cash import cash_balances
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.portfolio.results import Book, Holding
from portfolio_dash.shared.enums import Market
from portfolio_dash.shared.llm_config import get_model
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.wire import decimal_str
from portfolio_dash.strategy.target_weights import move_target_weight

router = APIRouter()

_ZERO = Decimal("0")
_BOM = "\ufeff"


@router.get("/input/context")
def context(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT account_id, fee_rule_set, dividend_model FROM accounts ORDER BY account_id"
    ).fetchall()
    meta = {r["account_id"]: r for r in rows}
    accts = list_accounts(conn)
    # `ccy` stays the settlement currency (back-compat); `funding_ccy` is added so the
    # cash page can constrain its currency dropdowns to {settlement, funding} (audit C2).
    accounts_out = [
        {
            "id": a.account_id,
            "name": a.name,
            "ccy": a.settlement_ccy.value,
            "settlement_ccy": a.settlement_ccy.value,
            "funding_ccy": a.funding_ccy.value,
            "div_model": div_model_wire(meta[a.account_id]["dividend_model"]),
            # Batch B (additive): per-market fee-rule + dividend-model bundle, so the input
            # forms can follow the ENTERED SYMBOL's market on a merged account (F01/F05). A
            # single-market account gets one mirrored entry -> legacy behaviour is unchanged.
            "markets": account_markets_wire(a.market_rules, conn),
        }
        for a in accts
    ]
    fee_rules = {
        aid: fee_rules_wire(get_fee_rule_set(m["fee_rule_set"], conn))
        for aid, m in meta.items()
    }
    insts = list_instruments(conn)
    # `archived` is wired (Fable F7) so the shared 代號 picker excludes archived symbols from
    # its 未持有 candidate group (a stealth 缺價 risk — an archived symbol is off the fetch
    # scopes, so registering a trade against it silently ships without a price). Held positions
    # are unaffected: they come from the holdings read, not this registry list, and the
    # "held => not archived" booking invariant keeps a live symbol un-archived anyway.
    instruments = [
        {
            "symbol": i.symbol,
            "name": i.name,
            "market": i.market.value,
            "ccy": i.quote_ccy.value,
            "etf": i.is_etf,
            "archived": i.archived,
        }
        for i in insts
    ]
    holdings: dict[str, dict[str, str]] = {}
    for a in accts:
        per = {
            inst.symbol: decimal_str(sh)
            for inst in insts
            if (sh := current_shares(conn, a.account_id, inst.symbol)) != 0
        }
        if per:
            holdings[a.account_id] = per
    return {
        "accounts": accounts_out,
        "fee_rules": fee_rules,
        "instruments": instruments,
        "holdings": holdings,
    }


def _unclear(h: Holding) -> bool:
    """True when a position's own numbers are 待釐清 and no what-if may be quoted on them.

    ONE predicate, two questions, because they must never diverge: "may I use this Holding?"
    and "is this key missing because it is withheld, or because nothing is held?" A second
    hand-maintained copy of the flag list is how ``unbookable_action`` reached four consumers
    and was wired into one.

    * ``oversold`` — negative shares, and the cost basis was deliberately discarded.
    * ``unbookable_action`` — a corporate action was skipped, so ``shares`` is still in
      PRE-action terms while ``adjusted_cost_total`` is post-action money. Every figure on
      this path is ``total / shares`` or ``qty / shares``, so the mismatch lands inside the
      answer and it is wrong by the action's whole ratio.

    NOT ``unbookable_dividend`` (shares and the share-derived math are right; only the cost
    is short one payout) and NOT ``short_open`` (a real position with a signed quantity) —
    the same call ``pnl.py`` documents, made against the same question.
    """
    return h.oversold or h.unbookable_action


def _book_or_none(conn: sqlite3.Connection) -> Book | None:
    """The cost-basis replay over the current ledger, or ``None`` when it cannot be booked.

    Never-500 (lesson: degrade at EVERY ``build_book`` call site). Split out of
    :func:`_holdings_or_none` so :func:`_position_preview` can tell a WITHHELD position from
    an absent one off the SAME replay — ``Book.holdings`` is the only source that answers
    both, and asking a second share path instead is how two answers to one question appear.
    """
    try:
        return build_book(_to_models(conn), allow_oversell=True)
    except (ValueError, KeyError):
        return None


def _holdings_or_none(conn: sqlite3.Connection) -> dict[tuple[str, str], Holding] | None:
    """(account_id, symbol) → open Holding from the VERIFIED cost-basis replay (build_book),
    or ``None`` when the ledger is un-bookable.

    The single source for every position what-if on the input path (FU-D44 sell hints +
    R6-E 草稿預覽): the same adjusted-cost engine every dashboard/report number comes from
    (``adjusted_avg = adjusted_total / shares``, computed on read, never a stored rounded
    average — domain-ledger.md), over the ledger models loaded by the ledgers-router
    ``_to_models`` seam (which already excludes unregistered-symbol rows, the dashboard's
    degradation). NO cost-basis math is duplicated here.

    Never-500 (lesson: degrade at EVERY ``build_book`` call site): an un-bookable ledger
    (e.g. a legacy orphan dividend → ValueError) returns ``None`` — callers then HIDE the
    position what-if entirely rather than mis-read an unvaluable book as an empty portfolio
    (which would show fresh-position math for an actually-held symbol). ``allow_oversell=True``
    keeps an acked 賣超 book from crashing; 待釐清 holdings carry no usable basis and are
    EXCLUDED — see :func:`_unclear` for which flags qualify and why ``unbookable_action`` now
    does. This is a PER-POSITION exclusion and stays one: every other symbol's hint keeps
    working (D38 invariant 1 containment).

    ``Book.unapplied_actions`` is deliberately NOT read here; see the note on
    :func:`_position_preview` for why the two silent refusal paths leave nothing for this
    seam to withhold.
    """
    book = _book_or_none(conn)
    if book is None:
        return None
    return {(h.account_id, h.symbol): h for h in book.holdings if not _unclear(h)}


def _adjusted_avg_by_position(conn: sqlite3.Connection) -> dict[tuple[str, str], Decimal]:
    """(account_id, symbol) → adjusted_avg for the FU-D44 sell hints — a thin projection of
    :func:`_holdings_or_none` (one build_book replay; un-bookable → EMPTY map so the hint
    simply hides, the same degradation as before)."""
    return {key: h.adjusted_avg for key, h in (_holdings_or_none(conn) or {}).items()}


@router.get("/input/holdings")
def input_holdings(
    account: str, conn: sqlite3.Connection = Depends(get_conn)
) -> Any:
    """Per-account held / closed symbols: 股利 picker (FU-D35) + sell-entry hints (FU-D44).

    ``held``   — symbols whose CURRENT net shares in *account* are > 0 (a dividend
                 normally comes from a live position). FU-D44 (additive): each held
                 entry also carries ``shares`` + ``adjusted_avg`` as Decimal STRINGS
                 (可賣股數 / 持有均價 for the manual sell hints); ``adjusted_avg`` is
                 null when the book cannot value the position (un-bookable ledger or
                 unregistered symbol) — the hint hides, never guesses.
    ``closed`` — symbols with ANY ledger history in *account* (transactions / opening
                 inventory / dividends) whose current net shares there are 0: a closed
                 position can still pay a dividend after its ex-date (owner 假設 2).
                 Closed entries stay ``{symbol, name}`` — the extension is held-only.

    Share counts reuse the pure ``current_shares`` helper (opening + buys − sells +
    zero-cost reinvest shares — the same replay rule as ``build_book``); adjusted_avg
    comes from the verified cost-basis replay itself (``_adjusted_avg_by_position`` →
    ``build_book``). NO cost-basis math is duplicated here. Classification is strictly
    per (account, symbol): the SAME symbol may be ``held`` in one account and ``closed``
    in another. Names come from the instruments registry (fallback: the raw symbol).
    Unknown account -> 404.
    """
    accounts = {a.account_id for a in list_accounts(conn)}
    if account not in accounts:
        return JSONResponse(status_code=404, content=error_body(
            "not_found", f"帳戶 {account} 不存在", field="account"))
    # Every symbol this account has ever touched, across the three share-bearing ledgers.
    symbols: set[str] = set()
    for row in conn.execute(
        "SELECT DISTINCT symbol FROM transactions WHERE account_id=?", (account,)
    ):
        symbols.add(row["symbol"])
    for row in conn.execute(
        "SELECT DISTINCT symbol FROM opening_inventory WHERE account_id=?", (account,)
    ):
        symbols.add(row["symbol"])
    for row in conn.execute(
        "SELECT DISTINCT symbol FROM dividends WHERE account_id=?", (account,)
    ):
        symbols.add(row["symbol"])
    names = {i.symbol: i.name for i in list_instruments(conn)}
    avg_map: dict[tuple[str, str], Decimal] | None = None  # built once, on first held row
    held: list[dict[str, Any]] = []
    closed: list[dict[str, str]] = []
    for sym in sorted(symbols):
        shares = current_shares(conn, account, sym)
        if shares > _ZERO:
            if avg_map is None:
                avg_map = _adjusted_avg_by_position(conn)
            avg = avg_map.get((account, sym))
            held.append({
                "symbol": sym, "name": names.get(sym) or sym,
                "shares": decimal_str(shares),
                "adjusted_avg": decimal_str(avg) if avg is not None else None,
            })
        else:
            closed.append({"symbol": sym, "name": names.get(sym) or sym})
    return {"held": held, "closed": closed}


class ManualBody(BaseModel):
    account_id: str
    symbol: str
    side: str
    date: date
    # shares/price bounded (audit M4) so an overflow-sized value 400s here rather than
    # reaching the fee quantize as a 500. fee/tax overrides constrained >= 0 (audit H2).
    shares: Decimal = Field(le=Decimal("1e12"))
    price: Decimal = Field(le=Decimal("1e12"))
    fee_override: Decimal | None = Field(default=None, ge=0)
    tax_override: Decimal | None = Field(default=None, ge=0)
    daytrade: bool = False  # TW same-day round trip → 0.15% sell tax (persisted, MED-1)
    # DECLARED short sale (2026-07-31). Only a flagged sell may exceed holdings; the
    # replay opens a short lot whose basis is the proceeds received, and the next buy
    # covers it at that buy's per-share cost. Never inferred from an oversell.
    short_sale: bool = False
    note: str | None = None
    ack_oversell: bool = False  # used by commit (Task 3)
    # Batch B (F05): explicit target market for an UNREGISTERED symbol on a MERGED
    # (multi-market) account, where the settlement ccy no longer picks one market. Blank on a
    # single-market account (the one market is inferred exactly as before); a merged account
    # with a blank market + unknown symbol is a commit-time 422 (market_required).
    market: str = ""


def _txn_input(body: ManualBody) -> TxnInput:
    # is_etf is NOT taken from the body: the instrument registry is authoritative
    # (resolved at the fee-computation seam in manual.py / csv_import.py).
    return TxnInput(
        account_id=body.account_id, symbol=body.symbol, side=parse_side(body.side),
        quantity=body.shares, price=body.price, trade_date=body.date,
        fee=body.fee_override, tax=body.tax_override, daytrade=body.daytrade,
        short_sale=body.short_sale, note=body.note,
    )


def _rule_for(
    conn: sqlite3.Connection, account_id: str, market: Market | None
) -> FeeRuleSet | None:
    """Fee rule set for (*account_id*, *market*) — the manual-preview per-trade rule.

    Batch B: market-aware via the (account, market) binding table. When *market* is None
    (unregistered symbol — no resolved instrument) fall back to the account scalar exactly as
    before (mirrors the whatif None-fallback). An unknown account -> None (unchanged).
    """
    if market is None:
        row = conn.execute(
            "SELECT fee_rule_set FROM accounts WHERE account_id=?", (account_id,)
        ).fetchone()
        return get_fee_rule_set(row["fee_rule_set"], conn) if row is not None else None
    try:
        rule_name = fee_rule_for(conn, account_id, market)
    except KeyError:  # unknown account — preserve the pre-swap None return
        return None
    return get_fee_rule_set(rule_name, conn)


def _parse_market(value: str) -> Market | None:
    """Wire market string ('US'/'TW'/'MY', any case) -> Market; None when blank/invalid."""
    v = value.strip().upper()
    if not v:
        return None
    try:
        return Market(v)
    except ValueError:
        return None


def _account_markets(conn: sqlite3.Connection, account_id: str) -> frozenset[Market]:
    """Markets bound to *account_id* (empty on an unknown account, never raises here)."""
    try:
        return allowed_markets(conn, account_id)
    except (KeyError, ValueError):
        return frozenset()


def _issue_wire_manual(
    issue: Issue, symbol: str, *, multi_market: bool = False
) -> dict[str, Any]:
    """issue_wire + the manual-entry auto-register overlay (2026-07-02).

    ``symbol_unresolved`` is a HARD block at the data_ingestion layer (the ledger
    safety net stands), but the manual COMMIT path auto-registers unknown symbols,
    so the PREVIEW presents it as an info-severity note instead of an error — the
    confirm button must not be gated on a condition the commit resolves itself.

    On a MERGED (multi-market) account the settlement ccy no longer picks one market, so
    the auto-register cannot guess: the preview note tells the user to register/pick a
    market first (F05) rather than promising an auto-register the commit will 422.
    """
    if issue.kind == "symbol_unresolved":
        if multi_market:
            return {
                "sev": "info", "code": "symbol_needs_market",
                "text": f"未註冊標的 {symbol} — 此帳戶橫跨多個市場，請先於標的管理選擇市場註冊",
                "field": "symbol",
            }
        return {
            "sev": "info", "code": "symbol_auto_register",
            "text": f"未註冊標的 {symbol} — 寫入時將自動查詢並註冊（查無報價則無法寫入）",
            "field": "symbol",
        }
    wired: dict[str, Any] = issue_wire(issue)
    return wired


def _cash_overdraft_issue(
    conn: sqlite3.Connection, body: ManualBody, draft_fee: Decimal, draft_tax: Decimal
) -> Issue | None:
    """Soft overdraft warning (audit C1b): only when the account opted into cash tracking
    (>=1 cash movement) AND this BUY would push the instrument's cash pool below zero.

    Never a hard block — users may not track cash at all (only fires once they do).
    """
    if parse_side(body.side) is not Side.BUY:
        return None
    inst = next((i for i in list_instruments(conn) if i.symbol == body.symbol), None)
    if inst is None:  # unregistered symbol: pool ccy unknown until auto-register — skip
        return None
    tracked = conn.execute(
        "SELECT 1 FROM cash_movements WHERE account_id=? LIMIT 1", (body.account_id,)
    ).fetchone()
    if tracked is None:
        return None
    bal = cash_balances(
        list_cash_movements(conn), list_fx_conversions(conn), list_transactions(conn),
        list_dividends(conn), {i.symbol: i for i in list_instruments(conn)},
    )
    current = bal.get((body.account_id, inst.quote_ccy), _ZERO)
    cost = body.shares * body.price + draft_fee + draft_tax
    if current - cost < _ZERO:
        return Issue(
            kind="cash_overdraft",
            needs_confirm=True,
            message=f"此帳戶 {inst.quote_ccy.value} 現金將不足(可能漏登入金或換匯),確認要寫入?",
        )
    return None


def _old_position_fields(held: Holding | None) -> dict[str, str | None]:
    """The PRE-trade position triple for the R7 OLD-vs-NEW comparison (C5, additive).

    ``old_shares`` / ``old_original_avg`` / ``old_adjusted_avg`` come straight from the held
    Holding (averages = total / shares, computed on read — never a stored rounded average,
    domain-ledger.md). Null for a fresh position (held None or 0 shares), so a first-time buy
    renders 新 values against an OLD dash.
    """
    if held is None or held.shares <= _ZERO:
        return {"old_shares": None, "old_original_avg": None, "old_adjusted_avg": None}
    return {
        "old_shares": decimal_str(held.shares),
        "old_original_avg": decimal_str(held.original_cost_total / held.shares),
        "old_adjusted_avg": decimal_str(held.adjusted_cost_total / held.shares),
    }


def _position_preview(
    conn: sqlite3.Connection, body: ManualBody, fee: Decimal, tax: Decimal, gross: Decimal
) -> dict[str, Any] | None:
    """SERVER-computed what-if position math for the draft (草稿預覽, R6-E) — the same
    information the per-holding drawer's 試算 shows, but computed HERE as Decimal strings
    (the frontend renders only; no front-end arithmetic). Additive; null when the symbol is
    unregistered (EXACT-only resolution) or the inputs are incomplete (shares/price ≤ 0).
    Never-500: any degradation path → null.

    Every branch also carries the R7 (C5) OLD-vs-NEW pre-trade triple (``old_shares`` /
    ``old_original_avg`` / ``old_adjusted_avg``) via :func:`_old_position_fields`; the
    existing ``new_*`` fields are byte-identical.

    * SELL, position currently held → ``cost_removed`` = adjusted_total × (qty / held_shares),
      ``realized_pnl`` = (gross − fee − tax) − cost_removed, ``remain_shares`` =
      max(0, held − qty). cost_removed/realized_pnl replicate build_book's OWN sell arithmetic
      exactly (via the position TOTALS, not a re-divided average), so the preview equals the
      booked realized row bit-for-bit — a mirror of the ledger's sell math, NOT a second source
      (no double counting). Oversell (qty > held) still renders honestly; remain floors at 0.
      Not held → null.
    * BUY → ``new_shares`` / ``new_original_avg`` / ``new_adjusted_avg`` from the held totals +
      this trade's ALL-IN cost (gross+fee+tax); not held → fresh-position math (held = 0, both
      averages = all-in / qty). Averages are computed from totals on read, never a stored
      rounded average (domain-ledger.md).

    **Why this seam gates on the FLAG and not on ``Book.unapplied_actions``** (the opposite
    call from ``pnl.py`` / ``timeseries.py`` / the XIRR gate, so the reason is written down).
    Those three multiply ``shares`` by a GLOBAL, already-post-action price, so any refusal
    anywhere makes their number wrong and they must read the book-level record — which sees
    the two refusals that leave no flagged holding. Nothing here is multiplied by a market
    price: every figure is arithmetic on the position's OWN totals, so the only question is
    whether THIS position's totals and share count disagree. That is exactly what
    ``unbookable_action`` marks, and ``_reject`` sets it on every refusal where a source
    position exists. The two flagless refusals leave nothing for a book-level gate to
    withhold: a source that never existed (E1) transferred nothing, and a source already at
    zero shares (E2, e.g. a duplicated EXCHANGE row) would have transferred nothing either —
    in both cases every position the preview can read is exactly what the ledger replays to.
    Reading ``unapplied_actions`` here would therefore blank the sell hint and the draft
    preview for EVERY symbol in EVERY account over a row that changed none of them: the
    portfolio-wide bail-out ``pnl.py``'s containment note forbids, and which D38 reserves
    for XIRR alone (tier 3). These are tier-1 per-symbol figures and stay contained.

    Known residual, and it belongs to W4 rather than here: ``/input/holdings`` derives its
    ``shares`` (and its 持有/已結清 classification) from ``current_shares``, which is
    corporate-action AWARE — so it APPLIES an action this replay REFUSED. Measured: with an
    EXCHANGE refused at E18/E22, ``current_shares`` moves the position to the destination and
    the symbol drops off the 持有 list entirely, while ``build_book`` still holds the source's
    1,000 pre-action shares and flags them 待釐清. The two paths disagree about whether the
    position exists; this seam therefore asks the BOOK and never the share walker.
    """
    inst = next((i for i in list_instruments(conn) if i.symbol == body.symbol), None)
    if inst is None:
        return None
    qty = body.shares
    if qty <= _ZERO or body.price <= _ZERO:
        return None
    try:
        book = _book_or_none(conn)
        if book is None:  # un-bookable ledger → no trustworthy position math
            return None
        key = (body.account_id, body.symbol)
        held = next((h for h in book.holdings if (h.account_id, h.symbol) == key), None)
        if held is not None and _unclear(held):
            # HELD, but 待釐清 — and this branch is why the exclusion in
            # :func:`_holdings_or_none` is only HALF a fix. Dropping the key makes the SELL
            # side hide (it already returns None for "not held"), but the BUY side reads a
            # missing key as "never held" and falls straight through to fresh-position math:
            # measured on a 1,000-share position whose corporate action was refused, the
            # exclusion alone turned ``new_shares 1500`` into ``new_shares 500`` — one wrong
            # number for a different wrong number, and reproducing "mis-read an unvaluable
            # book as an empty portfolio" one position at a time. Both sides now withhold,
            # for the one stated reason.
            #
            # Asked of the SAME ``Book`` the math would have come from, never of a second
            # share path: ``current_shares`` is corporate-action AWARE (W4) while this replay
            # REFUSED the action, so the two disagree about whether the position exists at
            # all — the very disagreement this seam must not build a guard on.
            return None
        if parse_side(body.side) is Side.SELL:
            if held is None:
                return None
            # frac / removed mirror build_book's ev.quantity / pos.shares and
            # pos.adjusted_total × frac EXACTLY (same operands, same order) so the preview
            # realized == the booked realized row (see the contract-test cross-check).
            cost_removed = held.adjusted_cost_total * (qty / held.shares)
            realized_pnl = (gross - fee - tax) - cost_removed
            remain = held.shares - qty
            return {
                "kind": "sell",
                "cost_removed": decimal_str(cost_removed),
                "realized_pnl": decimal_str(realized_pnl),
                "remain_shares": decimal_str(remain if remain > _ZERO else _ZERO),
                **_old_position_fields(held),
            }
        all_in = gross + fee + tax
        held_shares = held.shares if held is not None else _ZERO
        held_original = held.original_cost_total if held is not None else _ZERO
        held_adjusted = held.adjusted_cost_total if held is not None else _ZERO
        new_shares = held_shares + qty  # qty > 0 guaranteed, so never a zero divisor
        return {
            "kind": "buy",
            "new_shares": decimal_str(new_shares),
            "new_original_avg": decimal_str((held_original + all_in) / new_shares),
            "new_adjusted_avg": decimal_str((held_adjusted + all_in) / new_shares),
            **_old_position_fields(held),
        }
    except (ValueError, KeyError, ArithmeticError):
        return None


def _account_cash(
    conn: sqlite3.Connection, body: ManualBody
) -> tuple[dict[str, Any] | None, Decimal | None]:
    """DISPLAY-ONLY cash-pool balance for the trade's settlement (quote) currency (R6-E) —
    the SAME ``cash_balances`` figure /api/cash serves, so the draft line and the 資金 page
    never disagree. null when the symbol is unregistered (no quote ccy); ``balance`` null when
    the pool has no tracked activity. Adds NO issue and NO gating (owner-signed). Never-500.

    Returns ``(wire_dict, balance)``: the wire dict is the byte-identical ``{ccy, balance}``
    the response embeds, and ``balance`` is the SAME raw Decimal so the caller can compute the
    additive R7 ``cash_after`` line (C5) WITHOUT a second ``cash_balances`` pass (no new engine
    call). ``balance`` is None whenever the pool amount is unknown.
    """
    inst = next((i for i in list_instruments(conn) if i.symbol == body.symbol), None)
    if inst is None:
        return None, None
    amount: Decimal | None
    try:
        bal = cash_balances(
            list_cash_movements(conn), list_fx_conversions(conn), list_transactions(conn),
            list_dividends(conn), {i.symbol: i for i in list_instruments(conn)},
        )
        amount = bal.get((body.account_id, inst.quote_ccy))
    except (ValueError, KeyError, ArithmeticError):
        amount = None
    wire = {
        "ccy": inst.quote_ccy.value,
        "balance": decimal_str(amount) if amount is not None else None,
    }
    return wire, amount


@router.post("/input/manual/preview")
def manual_preview(
    body: ManualBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    draft = enter_transaction(conn, _txn_input(body), confirm=False, today=now.date())
    gross = body.shares * body.price
    total = (
        -(gross + draft.fee + draft.tax)
        if draft.inp.side.value == "BUY"
        else (gross - draft.fee - draft.tax)
    )
    # Market-aware per-trade rule (Batch B): the resolved instrument's market picks the rule
    # set; an unregistered symbol (draft.instrument None) keeps the account scalar (whatif
    # None-fallback). The preview never fails on the lookup — degrades to None.
    rule = _rule_for(
        conn, body.account_id,
        draft.instrument.market if draft.instrument is not None else None,
    )
    issues = list(draft.issues)
    overdraft = _cash_overdraft_issue(conn, body, draft.fee, draft.tax)
    if overdraft is not None:
        issues.append(overdraft)
    # FE-D1 forecast HINT (informational, 不計入成本): the TW charge-first rebate on next
    # month's refund = floor(resolved fee × rebate_rate). Null when the account never rebates
    # (rebate_rate 0 — every non-TW rule) so the UI only shows the line where it applies.
    rebate_estimate = (
        decimal_str(forecast_tw_rebate(draft.fee, rule.rebate_rate))
        if rule is not None and rule.rebate_rate > _ZERO
        else None
    )
    # R6-E (additive): the drawer-parity position what-if + the display-only account-cash line,
    # both SERVER-computed as Decimal strings (the frontend renders only). null on any
    # degradation / unregistered symbol / incomplete inputs — the base preview never fails.
    position_preview = _position_preview(conn, body, draft.fee, draft.tax, gross)
    account_cash, cash_bal = _account_cash(conn, body)
    # R7 A3 (additive, C5): projected pool AFTER settlement = balance + the ALREADY-SIGNED total
    # (BUY total is negative, SELL positive), in the SAME quote ccy as account_cash. Emitted only
    # when the balance is known (else null) — a pure Decimal add over figures already computed,
    # NO new engine call, NO float. Frontend renders it verbatim under 該帳戶現金.
    cash_after = decimal_str(cash_bal + total) if cash_bal is not None else None
    # Merged-account preview overlay (F05): the unregistered-symbol note tells the user to
    # pick a market first. Dormant for single-market accounts (exactly one bound market).
    multi_market = len(_account_markets(conn, body.account_id)) > 1
    return {
        "fee": decimal_str(draft.fee), "tax": decimal_str(draft.tax),
        "gross": decimal_str(gross), "total": decimal_str(total),
        "fee_rule_label": fee_rules_wire(rule)["label"] if rule is not None else None,
        "fee_overridden": body.fee_override is not None,
        "tax_overridden": body.tax_override is not None,
        "rebate_estimate": rebate_estimate,
        "position_preview": position_preview,
        "account_cash": account_cash,
        "cash_after": cash_after,
        "issues": [_issue_wire_manual(i, body.symbol, multi_market=multi_market) for i in issues],
    }


@router.post("/input/manual/commit", status_code=201)
def manual_commit(
    body: ManualBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    today = now.date()
    inp = _txn_input(body)
    draft = enter_transaction(conn, inp, confirm=False, today=today)  # inspect, no write

    # Auto-register an unknown symbol (2026-07-02): infer the market and run the one-step
    # registration (which REQUIRES a real quote — the typo guard). Success -> the entry
    # proceeds as if the symbol had been registered first; failure -> a clear 400, nothing
    # written.
    #
    # Market determination (Batch B, F05): a SINGLE-market account infers the one market from
    # its settlement ccy exactly as before (byte-identical). A MERGED (multi-market) account
    # has no settlement-ccy answer, so it REQUIRES an explicit, allowed ``body.market``; a
    # blank/invalid market -> 422 market_required (never guess which market to book into).
    auto_registered: dict[str, Any] | None = None
    if any(i.kind == "symbol_unresolved" for i in draft.issues):
        markets = _account_markets(conn, body.account_id)
        market: Market | None
        if len(markets) > 1:
            chosen = _parse_market(body.market)
            if chosen is None or chosen not in markets:
                allowed = "／".join(sorted(m.value for m in markets))
                return JSONResponse(status_code=422, content=error_body(
                    "market_required",
                    f"此帳戶橫跨多個市場（{allowed}），請指定要註冊到哪個市場再寫入",
                    field="market"))
            market = chosen
        else:
            market = _account_market(conn, body.account_id)
        if market is None:
            return JSONResponse(status_code=400, content=error_body(
                "validation_error", f"帳戶 {body.account_id} 不存在，無法自動註冊標的",
                field="account_id"))
        try:
            outcome = quick_register(
                conn, symbol=body.symbol, market=market, now=now, force=False)
        except QuickRegisterError as exc:
            if exc.code != "duplicate_symbol":  # duplicate = registered meanwhile -> proceed
                return JSONResponse(status_code=400, content=error_body(
                    "symbol_auto_register_failed",
                    f"自動註冊失敗：{exc.message}", field="symbol"))
        else:
            auto_registered = {
                "symbol": outcome.instrument.symbol,
                "name": outcome.instrument.name,
                "last": decimal_str(outcome.last) if outcome.last is not None else None,
            }
            body = body.model_copy(update={"symbol": outcome.instrument.symbol})
        inp = _txn_input(body)
        draft = enter_transaction(conn, inp, confirm=False, today=today)  # re-validate now

    hard = [i for i in draft.issues if not i.needs_confirm]
    if hard:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", hard[0].message,
            issues=[issue_wire(i) for i in draft.issues]))
    oversell = any(i.kind == "sell_exceeds_holdings" for i in draft.issues)
    if oversell and not body.ack_oversell:
        return JSONResponse(status_code=422, content=error_body(
            "oversell_unacknowledged", "需確認賣超",
            issues=[issue_wire(i) for i in draft.issues]))
    written = enter_transaction(conn, inp, confirm=True, today=today)
    gross = body.shares * body.price
    total = (-(gross + written.fee + written.tax) if inp.side.value == "BUY"
             else (gross - written.fee - written.tax))
    return {"txn_id": written.transaction_id, "total": decimal_str(total),
            "auto_registered": auto_registered}


# --- CSV import: preview (12.3) + commit (12.4) shared infrastructure ---

def _cash_builder(conn: sqlite3.Connection, csv_text: str) -> ImportPreview:
    """Bind the pool arithmetic ONCE for the whole file, then build the cash preview.

    The 6th kind is registered through a named wrapper rather than as a bare parser because
    ``build_cash_movement_preview`` takes its pool probe as a REQUIRED argument. The withdraw
    guard's two figures live in ``portfolio/cash.py``, which ``data_ingestion`` may not import
    (``architecture.md``), so the arithmetic is INJECTED here — the layer that sits above both
    — exactly as ``scheduler/jobs.py::split_factor_fn`` injects the split factor into
    ``pricing/`` under D17.

    Requiring the argument is the whole design: a registration that forgot to bind it would
    ship the bulk door with a weaker guard than the single-row form, and this way that is a
    mypy error rather than a silent overdraft. Binding it HERE (not per row) is D17's other
    rule — the probe closes over one ledger snapshot, so an N-row file reads the ledgers once.
    """
    return build_cash_movement_preview(conn, csv_text, pool=cash_pool_fn(conn))


#: Every kind's preview builder has ONE shape, which is what lets the two import endpoints
#: stay kind-agnostic. :func:`_resolve_builder` substitutes a widened transactions builder
#: behind this same signature rather than special-casing the kind at the call sites.
Builder = Callable[[sqlite3.Connection, str], ImportPreview]

_BUILDERS: dict[str, Builder] = {
    "transactions": build_transaction_preview, "dividends": build_dividend_preview,
    "fx": build_fx_preview, "openings": build_opening_preview,
    "corporate_actions": build_corporate_action_preview,
    "cash": _cash_builder,
}
_WRITERS = {
    "transactions": write_transaction_row, "dividends": write_dividend_row,
    "fx": write_fx_row, "openings": write_opening_row,
    "corporate_actions": write_corporate_action_row,
    "cash": write_cash_movement_row,
}
# Kinds whose commit moves the stored price basis and must therefore run the §5.1(c)
# reconcile afterwards. A SPLIT restates every close of its symbol; leaving it out would
# leave share counts corrected and prices not (or the reverse on a delete), which is the
# state the drawer footer cannot see. Passing a non-SPLIT symbol is a provable no-op
# (``split_factor`` is SPLIT-scoped), so the whole batch's symbols go in.
_RECONCILING_KINDS = frozenset({"corporate_actions"})


def _row_status(row: PreviewRow) -> str:
    if row.has_hard_issue:
        return "error"
    return "warn" if row.issues else "ok"


def _row_data(row: PreviewRow) -> dict[str, Any]:
    data = {k: v for k, v in row.payload.items() if not k.startswith("snap.")}
    if row.fee is not None:
        data["fee"] = decimal_str(row.fee)
    if row.tax is not None:
        data["tax"] = decimal_str(row.tax)
    return data


def _row_code(row: PreviewRow) -> str | None:
    """A STABLE machine code for a row the frontend can act on (FU-D33), additive to the
    human ``reason`` text. Currently the only code: ``unregistered_symbol`` — an unregistered
    symbol (``symbol_unresolved`` issue) in the AI / CSV-import preview, which the AI pane
    surfaces as an inline 立即註冊 action (the row's ``data.symbol`` carries the symbol). Returns
    None for every other row, so the field is purely additive."""
    if any(i.kind == "symbol_unresolved" for i in row.issues):
        return "unregistered_symbol"
    return None


def _preview_wire(preview: ImportPreview) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    counts = {"ok": 0, "warn": 0, "error": 0}
    for r in preview.rows:
        st = _row_status(r)
        counts[st] += 1
        rows.append({"n": r.index, "status": st,
                     "reason": r.issues[0].message if r.issues else None,
                     "code": _row_code(r),
                     "data": _row_data(r)})
    return {"rows": rows, "summary": {"total": len(preview.rows), **counts}}


def _date_ambiguity_wire(amb: DateAmbiguity) -> dict[str, Any]:
    """Wire shape for a column-level date ambiguity (FU-D19) — the frontend chooser reads it."""
    return {
        "column": amb.column,
        "samples": list(amb.samples),
        "candidates": [
            {"id": c.id, "label": c.label,
             "example_in": c.example_in, "example_out": c.example_out}
            for c in amb.candidates
        ],
    }


def _bad_date_format(date_format: str | None) -> JSONResponse | None:
    if date_format is not None and date_format not in FORMAT_IDS:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", f"未知 date_format: {date_format}", field="date_format"))
    return None


def _resolve_builder(
    kind: str, conn: sqlite3.Connection, pending_actions_csv: str | None
) -> Builder:
    """The kind's builder, widened when trades arrive WITH the actions that legalise them.

    Only ``transactions`` is affected, and only when the caller supplies the accompanying
    action file — which today means the broker one-click import, where the converter emits
    trades and corporate actions from the same statement in one run.

    **Why this exists at all.** The commit order is trades-then-actions and cannot be
    reversed: a corporate action's own guards need the position to exist, so actions-first
    rejects them. But a sell that is only legal AFTER a split then meets a share count taken
    before it, and raises 賣超 — the ONE confirmation in this system whose acknowledgement
    permanently discards a cost basis (the STICKY rule). The ledger ends up correct either
    way, because the actions land moments later and every report rebuilds from the ledgers;
    what is NOT acceptable is teaching the owner to click that particular dialog through a
    wall of false positives.

    It widens what the guard can SEE, never what it permits: an action that is not really
    coming simply is not in the file, and a malformed one excludes itself
    (``load_action_index``).
    """
    builder = _BUILDERS[kind]
    if kind != "transactions" or not pending_actions_csv:
        return builder
    index = load_action_index(conn, pending=parse_action_batch(pending_actions_csv))
    return lambda c, text: build_transaction_preview(c, text, pending_actions=index)


class ImportPreviewBody(BaseModel):
    kind: str
    csv_text: str
    date_format: str | None = None  # FU-D19: pin the date parse (from the ambiguity chooser)
    #: The corporate-action CSV being imported in the SAME run (broker one-click). See
    #: :func:`_resolve_builder`; ignored for every kind but ``transactions``.
    pending_actions_csv: str | None = None


@router.post("/import/preview")
def import_preview(body: ImportPreviewBody, conn: sqlite3.Connection = Depends(get_conn)) -> Any:
    if body.kind not in _BUILDERS:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", f"未知 kind: {body.kind}", field="kind"))
    if (bad := _bad_date_format(body.date_format)) is not None:
        return bad
    builder = _resolve_builder(body.kind, conn, body.pending_actions_csv)
    # FU-D19: canonicalize headers + resolve the date column to ISO before the ISO-only builder.
    norm = normalize_import_csv(
        body.csv_text, DATE_COLUMN_BY_KIND[body.kind], date_format=body.date_format)
    wire = _preview_wire(builder(conn, norm.text))
    if norm.ambiguity is not None:
        wire["date_ambiguity"] = _date_ambiguity_wire(norm.ambiguity)
    return wire


class ImportCommitBody(BaseModel):
    kind: str
    csv_text: str
    ack_warnings: bool = False
    date_format: str | None = None  # FU-D19: the chosen format carried through from preview
    # Provenance (2026-08-13). Optional and purely descriptive: the batch's IDENTITY is the
    # digest of csv_text, so a caller that omits the name still gets a traceable, reversible
    # batch — it just cannot be shown which file it came from.
    source_name: str | None = None
    broker: str | None = None
    #: See :class:`ImportPreviewBody`. Carried through from the preview so the commit
    #: re-derives the SAME verdict — the preview's answer is advisory, this one writes.
    pending_actions_csv: str | None = None


@router.post("/import/commit")
def import_commit(
    body: ImportCommitBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    writer = _WRITERS.get(body.kind)
    if body.kind not in _BUILDERS or writer is None:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", f"未知 kind: {body.kind}", field="kind"))
    if (bad := _bad_date_format(body.date_format)) is not None:
        return bad
    builder = _resolve_builder(body.kind, conn, body.pending_actions_csv)
    norm = normalize_import_csv(
        body.csv_text, DATE_COLUMN_BY_KIND[body.kind], date_format=body.date_format)
    if norm.ambiguity is not None:
        # Never guess: an unresolved M/D-vs-D/M column cannot be committed. The frontend keeps
        # the confirm disabled until a format is pinned; this is the defensive server gate.
        content = error_body(
            "date_ambiguity_unresolved", "日期格式不明確，請先選擇日期格式再寫入")
        content["date_ambiguity"] = _date_ambiguity_wire(norm.ambiguity)
        return JSONResponse(status_code=422, content=content)
    preview = builder(conn, norm.text)  # re-derive (re-validate vs current ledger)
    has_warn = any((not r.has_hard_issue) and r.issues for r in preview.rows)
    if has_warn and not body.ack_warnings:
        return JSONResponse(status_code=422, content=error_body(
            "warnings_unacknowledged", "有警告列需確認後才寫入"))
    accept = {r.index for r in preview.rows if not r.has_hard_issue}
    # Provenance. The hashes are derived from the rows' own content, so re-uploading the
    # same export matches and skips rather than doubling the ledger, and the batch id makes
    # the whole import removable in one step. The batch INSERT sits inside commit_preview's
    # transaction, so a rollback takes it with it.
    hashes = dict(
        zip(
            (r.index for r in preview.rows),
            row_hashes(body.kind, [r.raw for r in preview.rows]),
            strict=True,
        )
    )
    already = existing_hashes(conn, body.kind, hashes.values())
    # The batch record is created ONLY if this commit will write something. A refused or
    # fully-duplicate import leaving an empty batch behind would fill the history with rows
    # that claim an import happened and own no data — and the list exists to be read.
    will_write = any(
        r.index in accept
        and not r.has_hard_issue
        and hashes.get(r.index) not in already
        for r in preview.rows
    )
    # ⚠ The context itself is ALWAYS built, even when nothing will be written: it carries
    # the duplicate detection, so dropping it on a full re-import would write every row a
    # second time — the exact defect provenance exists to prevent, reintroduced by the
    # optimisation that skips the batch record. Only the batch ROW is conditional.
    provenance = BatchContext(
        kind=body.kind,
        batch_id=(
            open_batch(
                conn, kind=body.kind, csv_text=norm.text,
                source_name=body.source_name, broker=body.broker,
            )
            if will_write
            else None
        ),
        hashes=hashes,
        already_present=already,
    )
    summary = commit_preview(
        conn, preview, accept=accept, writer=writer, provenance=provenance)
    out: dict[str, Any] = {
        "written": len(summary.written), "skipped": len(summary.skipped)}
    if summary.rejected:
        # ⚠ ADDITIVE and only when non-zero (the ``duplicates`` convention below), so the
        # four kinds that never reject keep a byte-identical payload.
        #
        # This exists because 「跳過」 was doing two jobs. A row the caller deselected and a
        # row the importer REFUSED both landed in ``skipped``, so an import that dropped 3
        # of 5 corporate actions announced 「成功 2 筆・跳過 3 筆」 — true, and read by
        # everyone as "I didn't tick three of them". The ledger was then wrong by a split
        # ratio with nothing on any screen saying so. The rows carry their reason, because
        # a count alone tells the owner something went wrong and not what to do about it.
        out["rejected"] = len(summary.rejected)
        out["rejected_rows"] = [
            {"row": r.index + 1, "kind": r.kind, "message": r.message}
            for r in summary.rejected
        ]
    # ⚠ CONTRACT CHANGE, deliberate and for EVERY kind (2026-08-13): a commit that wrote
    # anything now also returns its ``import_batch_id``. It is the handle to the undo, and
    # an undo whose id can only be recovered by listing batches afterwards is one nobody
    # reaches for at the moment they need it — right after an import that looks wrong.
    if provenance.batch_id is not None:
        out["import_batch_id"] = provenance.batch_id
    if summary.duplicates:
        # Only when non-empty. A re-import is the case that needs the word: "written 0,
        # skipped 0" alone reads like nothing happened rather than like the ledger already
        # held every row.
        out["duplicates"] = len(summary.duplicates)
    if body.kind in _RECONCILING_KINDS:
        # ADDITIVE, and only for the kind that needs it: the other four keep a
        # byte-identical payload, because a response shape that grows for every caller
        # when one kind gains a behaviour is how a contract drifts.
        written_rows = [r for r in preview.rows
                        if r.index in accept and not r.has_hard_issue]
        out["prices_restated"] = reconcile_split_prices(
            conn,
            {r.payload.get("from_symbol", "") for r in written_rows}
            | {r.payload.get("to_symbol", "") for r in written_rows},
        )
        # The owner's per-symbol config follows a re-keyed position, at the same post-commit
        # seam and for the same reason the prices do: an EXCHANGE changes what the symbol
        # means, and anything still filed under the old one is now filed under nothing. The
        # price-alert band moves inside the row writer (D47); the target weight moves here,
        # because `data_ingestion -> strategy` is not an authorised edge (architecture.md)
        # and re-deriving the weights format on that side would make it a second owner.
        #
        # Deduplicated by PAIR, not per row: one event is N rows, one per holding account
        # (D13), and the move is idempotent anyway — the second call finds the source gone.
        pairs = {
            (str(r.payload.get("from_symbol", "")), str(r.payload.get("to_symbol", "")))
            for r in written_rows
            if str(r.payload.get("kind", "")).strip().upper() == "EXCHANGE"
        }
        weights_moved = [
            {"from_symbol": frm, "to_symbol": to, "weight": decimal_str(w)}
            for frm, to in sorted(pairs)
            if (w := move_target_weight(
                conn, from_symbol=frm, to_symbol=to, now=now)) is not None
        ]
        if weights_moved:
            out["weights_moved"] = weights_moved
    return out


@router.get("/import/batches")
def import_batches(
    limit: int = 50, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    """The import history, newest first — what came in, from which file, and how much of it.

    Read-only and cheap. It exists so the DELETE below is reachable by a human: a batch id
    that can only be discovered by opening the SQLite file is not an undo anyone will use.
    """
    rows = conn.execute(
        "SELECT id, kind, broker, source_name, source_sha256, imported_at, row_count, status "
        "FROM import_batches ORDER BY id DESC LIMIT ?",
        (max(1, min(limit, 500)),),
    ).fetchall()
    return {"batches": [dict(r) for r in rows]}


@router.delete("/import/batches/{batch_id}")
def import_batch_delete(
    batch_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> Any:
    """Undo one import: delete exactly the ledger rows that batch wrote, and the batch.

    This is what makes trying an import on real data a reasonable thing to do. The
    alternative — restoring a pre-import backup — also discards everything entered since,
    so without this the safe move is never to attempt the import at all.

    Rows entered by hand, or by a DIFFERENT batch, are untouched: the delete is keyed on
    ``import_batch_id``, which only this batch's rows carry.
    """
    found = conn.execute(
        "SELECT 1 FROM import_batches WHERE id=?", (batch_id,)
    ).fetchone()
    if found is None:
        return JSONResponse(status_code=404, content=error_body(
            "not_found", f"找不到匯入批次 {batch_id}", field="batch_id"))
    removed = delete_batch(conn, batch_id)
    return {"deleted": removed, "import_batch_id": batch_id}


@router.get("/import/template")
def import_template(kind: str = "transactions") -> Response:
    """Download a CSV import template (canonical header + worked example rows) for *kind*.

    UTF-8 **with BOM** (so Excel opens the Chinese ``note`` column cleanly) + CRLF, mirroring
    the export download shape. The header is the parser's own column constant
    (:mod:`data_ingestion.import_templates`) — a single source guarded by the round-trip test.
    Unknown ``kind`` -> 400 (same envelope as the preview/commit routes).
    """
    if kind not in TEMPLATE_KINDS:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", f"未知 kind: {kind}", field="kind"))
    body = (_BOM + render_import_template(kind)).encode("utf-8")
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": content_disposition(template_filename(kind))},
    )


# --- AI agents input: NL text (+ screenshots) -> preview + meta + commit CSV (12.4) ---

_LLM_HTTP = {"budget_exceeded": 402, "ai_not_activated": 409,
             "llm_unavailable": 503, "llm_error": 503}

# FU-D20 screenshot intake bounds. The server is the authority for these limits — the
# frontend caps to 4 as a courtesy, but every rule below is re-checked here so a direct
# API caller cannot bypass them. Magic-byte sniffing rejects a non-image payload before it
# ever reaches the vision model (never trust the client-declared MIME).
_AI_MAX_IMAGES = 4
_AI_MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB decoded, per image
_DATA_URI_PREFIX = re.compile(r"^data:image/[A-Za-z0-9.+-]+;base64,", re.IGNORECASE)


def _sniff_image(data: bytes) -> bool:
    """True when *data* opens with a PNG, JPEG, or WebP magic-byte signature."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if data[:3] == b"\xff\xd8\xff":
        return True
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def _image_error(message: str) -> JSONResponse:
    return JSONResponse(status_code=400, content=error_body(
        "validation_error", message, field="images"))


def _decode_ai_images(raw: list[str]) -> tuple[list[bytes], JSONResponse | None]:
    """Decode + validate base64 (data-URI prefix tolerated) images.

    Returns ``(decoded_bytes, None)`` on success or ``([], JSONResponse-400)`` on the first
    violation: too many images, invalid base64, oversize, or a non-PNG/JPEG/WebP payload.
    """
    if len(raw) > _AI_MAX_IMAGES:
        return [], _image_error(f"最多只能附上 {_AI_MAX_IMAGES} 張圖片")
    out: list[bytes] = []
    for item in raw:
        b64 = re.sub(r"\s+", "", _DATA_URI_PREFIX.sub("", item.strip()))
        try:
            data = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            return [], _image_error("圖片編碼無效（需為 base64 圖片）")
        if len(data) > _AI_MAX_IMAGE_BYTES:
            return [], _image_error("單張圖片不可超過 5 MB")
        if not _sniff_image(data):
            return [], _image_error("僅支援 PNG／JPEG／WebP 圖片")
        out.append(data)
    return out, None


class AiBody(BaseModel):
    text: str = ""
    # base64 (data-URI prefix tolerated); ≤4 images, ≤5 MB decoded each, png/jpeg/webp only.
    images: list[str] | None = None
    # explicit per-run model alias; must name an ENABLED model (+ vision when images present).
    model_alias: str | None = None


@router.post("/input/ai/preview")
def ai_preview(
    body: AiBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    images, bad = _decode_ai_images(body.images or [])
    if bad is not None:
        return bad
    if not body.text.strip() and not images:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", "請提供對帳單文字或至少一張截圖", field="text"))
    if body.model_alias:
        model = get_model(conn, body.model_alias)
        if model is None or not model.enabled:
            return JSONResponse(status_code=400, content=error_body(
                "validation_error", f"指定的模型不存在或已停用：{body.model_alias}",
                field="model_alias"))
        # Consistent with the frontend rule: a non-vision model cannot read screenshots.
        if images and not model.vision:
            return JSONResponse(status_code=400, content=error_body(
                "validation_error",
                "所選模型不支援影像，請改用支援影像的模型或改用「自動」", field="model_alias"))
    result = ai_agents_input(
        conn, body.text, today=now.date(),
        images=images or None, model_alias=body.model_alias or None,
    )
    for r in result.preview.rows:
        for issue in r.issues:
            if issue.kind in _LLM_HTTP:
                return JSONResponse(status_code=_LLM_HTTP[issue.kind],
                                    content=error_body(issue.kind, issue.message))
    wire = _preview_wire(result.preview)
    wire["meta"] = {"model": result.meta.model, "via": result.meta.via,
                    "cost_usd": None if result.meta.cost_usd is None
                    else decimal_str(result.meta.cost_usd)}
    wire["csv_text"] = result.csv_text
    return wire
