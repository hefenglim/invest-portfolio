"""Instruments registry API (spec 10): list (+ probe + register/update in later tasks).

Thin over data_ingestion.store + pricing.store reads. Computes nothing of record.
"""

import sqlite3
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from portfolio_dash.api.deps import get_conn, get_now
from portfolio_dash.api.errors import error_body
from portfolio_dash.api.instrument_service import (
    QuickRegisterError,
    gap_backfill,
    last_price_date,
    lookup_instrument,
    quick_register,
    restore_archived,
)
from portfolio_dash.data_ingestion.holdings import current_shares
from portfolio_dash.data_ingestion.store import (
    delete_instrument,
    get_instrument,
    has_ledger_history,
    list_accounts,
    list_instruments,
    set_instrument_archived,
    upsert_instrument,
)
from portfolio_dash.llm_insight.official_templates import (
    AI_INSTRUMENT_RESOLVE_PROMPT,
    AI_INSTRUMENT_RESOLVE_PROMPT_VERSION,
)
from portfolio_dash.pricing.benchmarks import BENCHMARKS
from portfolio_dash.pricing.board import probe_tw_board
from portfolio_dash.pricing.store import get_latest_price, get_price_history
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.llm import complete_structured
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.sectors import (
    CANONICAL_KEYS,
    CANONICAL_SECTORS,
    canonical_sector,
)
from portfolio_dash.shared.wire import decimal_str

router = APIRouter()


def _held(conn: sqlite3.Connection, account_ids: list[str], symbol: str) -> bool:
    return any(current_shares(conn, aid, symbol) > 0 for aid in account_ids)


def _board_wire(conn: sqlite3.Connection, inst: Instrument) -> str | None:
    """TW + board_status='unresolved' -> null; otherwise the stored board string."""
    row = conn.execute("SELECT board_status FROM instruments WHERE symbol=?",
                       (inst.symbol,)).fetchone()
    status = row["board_status"] if row is not None else "resolved"
    if inst.market.value == "TW" and status == "unresolved":
        return None
    return inst.board


def _element(conn: sqlite3.Connection, inst: Instrument, account_ids: list[str],
             now: datetime) -> dict[str, Any]:
    pr = get_latest_price(conn, inst.symbol, now=now)
    last = decimal_str(pr.value) if pr is not None else None
    chg_pct: str | None = None
    if pr is not None:
        hist = get_price_history(conn, inst.symbol, pr.as_of.replace(day=1), pr.as_of)
        if len(hist) >= 2 and hist[-2].value != 0:
            chg_pct = decimal_str((hist[-1].value - hist[-2].value) / hist[-2].value)
    return {
        "symbol": inst.symbol, "name": inst.name, "market": inst.market.value,
        "board": _board_wire(conn, inst), "sector": inst.sector,
        "industry": inst.industry,  # R6: GICS industry passthrough (null until next-wave fill)
        "ccy": inst.quote_ccy.value, "held": _held(conn, account_ids, inst.symbol),
        "last": last, "chg_pct": chg_pct,
        "target_low": decimal_str(inst.target_low) if inst.target_low is not None else None,
        "target_high": decimal_str(inst.target_high) if inst.target_high is not None else None,
        "is_etf": inst.is_etf,
        "archived": inst.archived,
        # FU-D32: any permanent ledger history (incl. closed positions) — the watchlist
        # dialog pre-disables 永久移除 (purge) for these (~3 LIMIT-1 queries; fine at scale).
        "has_history": has_ledger_history(conn, inst.symbol),
    }


@router.get("/instruments")
def list_all(
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    account_ids = [a.account_id for a in list_accounts(conn)]
    items = [_element(conn, inst, account_ids, now) for inst in list_instruments(conn)]
    return {"as_of": now.isoformat(), "list": items}


_BOARD_LABEL = {"TWSE": "TWSE 上市", "TPEx": "TPEx 上櫃"}


class ProbeBody(BaseModel):
    symbol: str


@router.post("/instruments/probe")
def probe(body: ProbeBody) -> dict[str, Any]:
    """Registration step 1: guess the TW board for a symbol (user confirms next)."""
    sym = body.symbol.strip()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol 不可為空")
    board = probe_tw_board(sym)
    return {"symbol": sym, "name": None, "board": board,
            "board_label": _BOARD_LABEL.get(board or "", "未解析")}


@router.get("/instruments/lookup")
def lookup(
    symbol: str = Query(...),
    market: Market = Query(...),
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """FU-D23 fast identification for the quick-add dialog — name + suggested sector +
    board/is_etf, no history fetch. ``found=false`` is the typo guard (the dialog blocks the
    confirm). A KNOWN symbol resolves from stored metadata with no provider call
    (``registered`` when active, ``archived`` when soft-deleted → confirming restores it)."""
    return lookup_instrument(conn, symbol=symbol, market=market, now=now).model_dump()


# --- FU-D31: canonical sector vocabulary + AI sector detection ------------------------


@router.get("/instruments/sectors")
def sectors() -> dict[str, Any]:
    """The canonical sector vocabulary that seeds the sector ``<select>`` in the quick-add
    dialog + the edit form. English ``key`` is the stored/grouped value; ``zh`` is the
    dropdown label only. Synonyms are SERVER-SIDE (``shared/sectors.canonical_sector``), so
    the frontend never hardcodes the list (FU-D31)."""
    return {"sectors": [dict(s) for s in CANONICAL_SECTORS]}


# --- R6-B: unified AI instrument-resolve (one impl × four entry points) ----------------


def _parse_market(raw: str) -> Market | None:
    """Best-effort parse of the wire market string into a :class:`Market`, else None.

    ``None`` disables the provider-verification step (no market to probe) — the reply then
    stays advisory (candidates), never falsely 'verified'."""
    try:
        return Market(raw.strip().upper())
    except ValueError:
        return None


class AiResolveBody(BaseModel):
    """Raw user input (symbol and/or name field content) + the dialog's target market.

    ``sector_only`` (the watchlist edit-form 「AI 偵測產業類別」 re-detect case) SKIPS the
    already-registered short-circuit so the LLM re-classifies a known instrument; the frontend
    then applies ONLY the returned sector/industry (never symbol/name)."""

    query: str = ""
    market: str = ""
    sector_only: bool = False


class AiResolveCandidate(BaseModel):
    """One uncertain-case candidate the model offers (2-5 when confidence is not high)."""

    symbol: str = ""
    name: str = ""
    gics_sector: str = ""


class AiInstrumentResolveReply(BaseModel):
    """The single structured contract the unified prompt returns.

    ``symbol`` may be empty (with ``not_found=true``) when the model honestly cannot identify
    the instrument; ``gics_sector`` must be one of the 11 GICS keys (re-validated server-side);
    ``gics_industry`` is the optional finer GICS level. ``confidence`` is a plain str so a stray
    value degrades gracefully (normalized to low) rather than failing schema validation."""

    symbol: str = ""
    name: str = ""
    gics_sector: str = ""
    gics_industry: str | None = None
    confidence: str = "low"
    candidates: list[AiResolveCandidate] = Field(default_factory=list)
    not_found: bool = False


@router.post("/instruments/ai-resolve")
def ai_resolve(
    body: AiResolveBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """Unified AI instrument-resolve (R6-B, 2026-07-19): ONE LLM call → local exchange code +
    name + GICS sector (+ optional industry), serving every registration entry point
    (manual-trade / AI-input / CSV quick-add dialogs) plus the watchlist surfaces.

    Pipeline: (a) server-side re-gate — a raw input that is ALREADY a registered (active) symbol
    answers from the registry with NO LLM call (skipped in ``sector_only`` re-detect mode, whose
    purpose is to re-classify a known symbol); (b) one deterministic (temperature=0) structured
    LLM call with the code-owned unified prompt; (c) ``gics_sector`` re-mapped through
    ``canonical_sector`` — an off-vocabulary value downgrades confidence to low; (d) PROVIDER
    VERIFICATION — the returned symbol is resolved via the REAL quote/name lookup (the sole
    registration authority): a verified + high-confidence result is ``status:"resolved"``
    (auto-fill; provider name preferred, AI name as fallback), anything else is
    ``status:"candidates"`` (manual pick — the unverified primary FIRST, marked
    ``verified:false``). An honest model not-found → ``status:"not_found"`` with a zh message
    (never fabricates a code). LLM degradation (budget / not-activated / provider) propagates to
    the global 402 / 409 / 503 handlers — the same envelope the prior AI endpoints returned;
    nothing here blocks the form, and the AI never sets ``is_etf``."""
    query = body.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="query 不可為空")
    market = _parse_market(body.market)

    # (a) re-gate: an already-registered ACTIVE symbol resolves from the registry, no LLM call.
    #     Skipped for sector-only re-detect (which must re-classify a known symbol via the LLM).
    if not body.sector_only:
        existing = get_instrument(conn, query.upper())
        if existing is not None and not existing.archived:
            return {
                "status": "resolved",
                "symbol": existing.symbol,
                "name": existing.name,
                "sector": existing.sector,
                "industry": existing.industry,
                "confidence": "high",
                "verified": True,
                "prompt_version": AI_INSTRUMENT_RESOLVE_PROMPT_VERSION,
            }

    # (b) single deterministic LLM call (temperature=0).
    prompt = AI_INSTRUMENT_RESOLVE_PROMPT.format(
        query=query, market=body.market.strip() or "（未提供）"
    )
    reply = complete_structured(
        prompt, AiInstrumentResolveReply, agent="ai_instrument_resolve",
        conn=conn, temperature=0,
    )

    # honest not-found: the model could not identify the instrument — never invent a code.
    if reply.not_found or not reply.symbol.strip():
        return {
            "status": "not_found",
            "message": "查無此標的 — 請確認名稱與市場是否正確",
            "prompt_version": AI_INSTRUMENT_RESOLVE_PROMPT_VERSION,
        }

    # (c) sector validation: canonicalize; an off-vocabulary sector downgrades to low.
    primary_symbol = reply.symbol.strip().upper()
    sector = canonical_sector(reply.gics_sector)
    sector_ok = sector in CANONICAL_KEYS
    confidence = reply.confidence.strip().lower()
    if confidence not in ("high", "medium", "low"):
        confidence = "low"
    if not sector_ok:
        confidence = "low"
    industry = (reply.gics_industry or "").strip() or None

    # (d) provider verification: the returned symbol must resolve via the REAL quote/name
    #     lookup (the sole registration authority) before any auto-fill.
    verified = False
    provider_name = ""
    if market is not None:
        lk = lookup_instrument(conn, symbol=primary_symbol, market=market, now=now)
        verified = lk.found
        provider_name = lk.name

    if verified and confidence == "high":
        return {
            "status": "resolved",
            "symbol": primary_symbol,
            "name": provider_name or reply.name.strip(),  # prefer the provider's name
            "sector": sector,
            "industry": industry,
            "confidence": "high",
            "verified": True,
            "prompt_version": AI_INSTRUMENT_RESOLVE_PROMPT_VERSION,
        }

    # verification fail OR medium/low → candidates; the UNVERIFIED primary goes FIRST.
    candidates: list[dict[str, Any]] = [{
        "symbol": primary_symbol,
        "name": reply.name.strip(),
        "sector": sector if sector_ok else "",
        "verified": False,
    }]
    for cand in reply.candidates:
        csym = cand.symbol.strip().upper()
        if not csym or csym == primary_symbol:
            continue
        csector = canonical_sector(cand.gics_sector) if cand.gics_sector.strip() else ""
        candidates.append({
            "symbol": csym,
            "name": cand.name.strip(),
            "sector": csector if csector in CANONICAL_KEYS else "",
            "verified": False,
        })
        if len(candidates) >= 5:
            break
    return {
        "status": "candidates",
        "candidates": candidates,
        "confidence": confidence,
        "prompt_version": AI_INSTRUMENT_RESOLVE_PROMPT_VERSION,
    }


_TW_BOARDS = {"TWSE", "TPEx"}


class RegisterBody(BaseModel):
    symbol: str
    market: Market
    name: str = ""
    sector: str = ""
    industry: str | None = None  # R6: optional GICS 產業細分 (blank ⇒ preserve existing)
    board: str | None = None
    quote_ccy: Currency | None = None
    target_low: Decimal | None = None
    target_high: Decimal | None = None
    is_etf: bool = False


class UpdateBody(BaseModel):
    name: str | None = None
    sector: str | None = None
    industry: str | None = None  # R6: GICS 產業細分 (exclude_unset ⇒ omitted = unchanged)
    board: str | None = None
    target_low: Decimal | None = None
    target_high: Decimal | None = None
    is_etf: bool | None = None


def _apply_extras(
    conn: sqlite3.Connection,
    inst: Instrument,
    *,
    target_high: Decimal | None,
    industry: str | None,
) -> Instrument:
    """Persist REGISTRATION-time extras the shared onboarding service does not carry.

    ``quick_register`` / ``restore_archived`` predate both ``target_high`` (FU-D28) and
    ``industry`` (R6): they are applied here via a SINGLE read-modify-write upsert once the row
    exists, so a value the service omitted is written without clobbering any field it DID set
    (``model_copy`` carries the full row). Each ``None`` extra is skipped — a blank industry
    from the quick-add dialog therefore never clears a restored row's stored industry — so an
    all-``None`` call (the usual quick-add case) is a no-op, byte-identical to before.
    """
    updates: dict[str, object] = {}
    if target_high is not None:
        updates["target_high"] = target_high
    if industry is not None:
        updates["industry"] = industry
    if not updates:
        return inst
    upsert_instrument(conn, inst.model_copy(update=updates))
    saved = get_instrument(conn, inst.symbol)
    assert saved is not None
    return saved


@router.post("/instruments", status_code=201)
def register(
    body: RegisterBody,
    background_tasks: BackgroundTasks,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    """Classic (detail-form) registration: registers even when no quote is found.

    Delegates to the shared quick_register service (probe + instant quote + name
    fill-in) with ``force=True`` so its behavior stays backward compatible: a provider
    outage never blocks an explicit registration. The heavy history backfill is offloaded
    to a background gap_backfill (FU-D23) so the quick-add dialog's confirm returns fast.

    FU-D18 door (b): when *symbol* already exists but is ARCHIVED, this RESTORES it
    (un-archive + apply provided metadata) and schedules a background gap backfill, so the
    response stays fast; it returns ``restored: true`` + ``last_price_date`` (the last data
    on file, pre-backfill).
    """
    if body.market in (Market.US, Market.MY) and (body.board or "") in _TW_BOARDS:
        return JSONResponse(status_code=400,
                            content=error_body("validation_error", "US/MY 不可帶台股板別",
                                               field="board"))
    sym = body.symbol.strip().upper()
    # R6: a blank industry means "not provided" — never clobbers a stored value (the edit form,
    # via UpdateBody + exclude_unset, is the path that can explicitly clear it).
    industry = (body.industry or "").strip() or None
    existing = get_instrument(conn, sym)
    if existing is not None and existing.archived:
        saved = restore_archived(
            conn, existing, name=body.name, sector=body.sector,
            board=body.board or None, quote_ccy=body.quote_ccy,
            target_low=body.target_low, is_etf=body.is_etf,
        )
        # FU-D28 (target_high) + R6 (industry): the onboarding service carries neither.
        saved = _apply_extras(conn, saved, target_high=body.target_high, industry=industry)
        last_date = last_price_date(conn, sym)  # read BEFORE the backfill runs
        background_tasks.add_task(gap_backfill, sym, now=now)
        account_ids = [a.account_id for a in list_accounts(conn)]
        elem = _element(conn, saved, account_ids, now)
        elem["restored"] = True
        elem["last_price_date"] = last_date
        return elem
    try:
        outcome = quick_register(
            conn, symbol=body.symbol, market=body.market, now=now, name=body.name,
            sector=body.sector, board=body.board or None, quote_ccy=body.quote_ccy,
            target_low=body.target_low, is_etf=body.is_etf, force=True,
            backfill_history=False,
        )
    except QuickRegisterError as exc:
        return JSONResponse(status_code=exc.status,
                            content=error_body(exc.code, exc.message))
    # FU-D23: the instant quote is fetched synchronously (so ``last`` is immediate), but the
    # heavy history/dividend backfill runs in the BACKGROUND — the same gap_backfill primitive
    # the FU-D18 restore path schedules, now consistently wired for a brand-new registration
    # too, so the quick-add dialog's confirm returns fast (「背景抓取報價中」).
    background_tasks.add_task(gap_backfill, sym, now=now)
    saved = _apply_extras(  # FU-D28 (target_high) + R6 (industry)
        conn, outcome.instrument, target_high=body.target_high, industry=industry
    )
    account_ids = [a.account_id for a in list_accounts(conn)]
    return _element(conn, saved, account_ids, now)


class QuickBody(BaseModel):
    symbol: str
    market: Market
    sector: str = ""
    force: bool = False  # true: register even when no source supplies a quote


_QUICK_BOARD_LABEL = {"TWSE": "TWSE 上市", "TPEx": "TPEx 上櫃", ".KL": "馬股 .KL", "": "美股"}


@router.post("/instruments/quick", status_code=201)
def quick(
    body: QuickBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    """One-step add (2026-07-02): symbol -> probe + real quote + name + history.

    422 ``quote_not_found`` when no source supplies a price (typo guard); the
    frontend re-sends with ``force=true`` only after an explicit user confirm.
    """
    try:
        outcome = quick_register(
            conn, symbol=body.symbol, market=body.market, now=now,
            sector=body.sector, force=body.force,
        )
    except QuickRegisterError as exc:
        return JSONResponse(status_code=exc.status,
                            content=error_body(exc.code, exc.message))
    account_ids = [a.account_id for a in list_accounts(conn)]
    elem = _element(conn, outcome.instrument, account_ids, now)
    elem["board_label"] = (
        _QUICK_BOARD_LABEL.get(outcome.board or "", "板別未解析（暫以 TWSE 抓報價）")
        if body.market is Market.TW
        else _QUICK_BOARD_LABEL[".KL" if body.market is Market.MY else ""]
    )
    elem["name_source"] = outcome.name_source
    elem["history_backfilled"] = outcome.history_points
    # FU-D18 door (c): a re-add of an archived symbol RESTORES it inline (never 409).
    elem["restored"] = outcome.restored
    return elem


@router.put("/instruments/{symbol}")
def update(
    symbol: str,
    body: UpdateBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    existing = get_instrument(conn, symbol)
    if existing is None:
        return JSONResponse(status_code=404,
                            content=error_body("not_found", f"{symbol} 不存在"))
    if existing.market in (Market.US, Market.MY) and (body.board or "") in _TW_BOARDS:
        return JSONResponse(status_code=400,
                            content=error_body("validation_error", "US/MY 不可帶台股板別",
                                               field="board"))
    # exclude_unset (2026-07-03): an EXPLICIT null must clear the field (target_low
    # null = remove the alert) — the old exclude_none silently dropped it, so
    # clearing a target price never worked.
    fields = body.model_dump(exclude_unset=True)
    updated = existing.model_copy(update=fields)
    upsert_instrument(conn, updated)
    # An explicit board set on a TW instrument resolves its board_status (the
    # 重新探測-and-save flow, 2026-07-02); upsert_instrument itself never touches
    # board_status (owned by registration).
    if "board" in fields and updated.market is Market.TW and updated.board:
        conn.execute("UPDATE instruments SET board_status='resolved' WHERE symbol=?",
                     (symbol,))
        conn.commit()
    saved = get_instrument(conn, symbol)
    assert saved is not None
    account_ids = [a.account_id for a in list_accounts(conn)]
    return _element(conn, saved, account_ids, now)


class ArchiveBody(BaseModel):
    archived: bool


@router.put("/instruments/{symbol}/archive")
def archive(
    symbol: str,
    body: ArchiveBody,
    background_tasks: BackgroundTasks,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    """Set / clear a symbol's 封存 (stop-tracking) flag (FU-D13; restore backfill FU-D18).

    Archiving a HELD symbol is refused (422 ``held``) — the invariant is held ⇒ not
    archived. Un-archiving (``archived: false``, the 還原 path) is always allowed and
    schedules a background gap backfill (FU-D18); the response returns ``last_price_date``
    (the last data on file, pre-backfill). The flag only scopes quote/signal/news fetches;
    the symbol stays registered, so no money figure moves (the archived-symbol
    dashboard-invariant test proves this).
    """
    if get_instrument(conn, symbol) is None:
        return JSONResponse(status_code=404,
                            content=error_body("not_found", f"{symbol} 不存在"))
    account_ids = [a.account_id for a in list_accounts(conn)]
    if body.archived and _held(conn, account_ids, symbol):
        return JSONResponse(status_code=422, content=error_body(
            "held", "持倉中的標的不可移除或封存", field="symbol"))
    set_instrument_archived(conn, symbol, body.archived)
    if not body.archived:  # FU-D18: restoring triggers a background gap backfill
        last_date = last_price_date(conn, symbol)  # read BEFORE the backfill runs
        background_tasks.add_task(gap_backfill, symbol, now=now)
        return {"ok": True, "archived": False, "last_price_date": last_date}
    return {"ok": True, "archived": True}


@router.delete("/instruments/{symbol}")
def remove(
    symbol: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    """SOFT-delete (archive) any non-held symbol — accumulative watchlist (FU-D18).

    404 unknown → 422 ``held`` (a held symbol is never removable) → else SOFT delete via
    ``set_instrument_archived(True)``: the symbol is hidden from the watchlist front-end but
    NO data is removed (prices, dividend_events, signals, alerts, news all stay), so re-adding
    it restores everything and gap-backfills the missing window. The former ``has_history``
    422 tier is gone (a closed-with-history symbol soft-deletes like any other); the hard
    delete (``store.delete_instrument``) is retained internally but no longer routed.
    """
    if get_instrument(conn, symbol) is None:
        return JSONResponse(status_code=404,
                            content=error_body("not_found", f"{symbol} 不存在"))
    account_ids = [a.account_id for a in list_accounts(conn)]
    if _held(conn, account_ids, symbol):
        return JSONResponse(status_code=422, content=error_body(
            "held", "持倉中的標的不可移除或封存", field="symbol"))
    set_instrument_archived(conn, symbol, True)
    return {"ok": True, "removed": True}


# --- FU-D32: 永久移除 (hard purge) — the second, destructive deletion tier -------------

# Benchmark storage keys (``pricing/benchmarks.py``): a purge of one of these keeps its
# market-data rows so the dashboard TWR benchmark series survives (only ``"0050"`` can ever
# collide with a real user instrument; ``"^GSPC"`` is not a valid symbol — see benchmarks.py).
_BENCHMARK_KEYS: frozenset[str] = frozenset(b.storage_key for b in BENCHMARKS)

# The owner's explanation for why a symbol with ANY ledger history (INCLUDING a closed
# position) can never be hard-purged — surfaced verbatim so the dialog can disable 永久移除.
_HAS_HISTORY_MSG = (
    "有歷史帳務紀錄，無法永久移除：已清倉標的的歷史交易仍被現金流回溯、XIRR／歷年報酬、"
    "股利紀錄、已實現損益報表引用；硬刪會導致帳目無法對帳與孤兒資料。可改用「移除（隱藏）」。"
)


@router.post("/instruments/{symbol}/purge")
def purge(
    symbol: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    """HARD-delete (永久移除) a never-traded watch symbol + all its personal artifacts (FU-D32).

    Deletion-tier gate, most-specific first: 404 unknown → 422 ``held`` (a held symbol is never
    purgeable — close the position first) → 422 ``has_history`` for ANY permanent ledger history
    INCLUDING closed positions (their trades still feed cashflow/XIRR/歷年報酬/股利/已實現報表;
    hard-deleting orphans the books — the owner's copy) → else ``delete_instrument`` removes the
    registry row and every derived/cache row in one transaction.

    Benchmark guard: when *symbol* is also a benchmark storage key, the purge PRESERVES the
    market-data rows (``prices`` / ``dividend_events``) so the dashboard TWR benchmark series
    is not orphaned — the registry row and personal artifacts are still cleaned; the response
    flags ``preserved_market_data``. The plain DELETE (soft delete / 隱藏) is untouched."""
    if get_instrument(conn, symbol) is None:
        return JSONResponse(status_code=404,
                            content=error_body("not_found", f"{symbol} 不存在"))
    account_ids = [a.account_id for a in list_accounts(conn)]
    if _held(conn, account_ids, symbol):
        return JSONResponse(status_code=422, content=error_body(
            "held", "持倉中的標的不可永久移除，請先清空持倉", field="symbol"))
    if has_ledger_history(conn, symbol):
        return JSONResponse(status_code=422, content=error_body(
            "has_history", f"{symbol} {_HAS_HISTORY_MSG}", field="symbol"))
    preserve = symbol in _BENCHMARK_KEYS
    delete_instrument(conn, symbol, preserve_market_data=preserve)
    return {"ok": True, "purged": True, "preserved_market_data": preserve}
