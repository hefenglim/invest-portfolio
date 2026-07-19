"""One-step instrument onboarding shared by the instruments + input routers.

Collapses probe -> name lookup -> register -> instant quote -> 3-month history
into ONE call. Layering: this is api-layer orchestration — it may call
``data_ingestion`` (register/store), ``pricing`` (probe/name/refresh), and the
``scheduler.jobs`` worklist constants; none of those layers import it back.

The correctness gate: by default a symbol is registrable ONLY when a real quote
can be fetched for it (``require_quote=True``) — a symbol whose price no source
can supply is almost always a typo, and admitting it would recreate the
permanently price-less rows this gate exists to prevent. ``force=True`` (an
explicit user confirmation) registers a quote-less symbol anyway; the classic
register endpoint passes it for backward compatibility.
"""

import logging
import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel

from portfolio_dash.data_ingestion.register import register_instrument
from portfolio_dash.data_ingestion.store import (
    get_instrument,
    set_instrument_archived,
    upsert_instrument,
)
from portfolio_dash.pricing.board import probe_tw_board
from portfolio_dash.pricing.defaults import default_registry
from portfolio_dash.pricing.names import lookup_name
from portfolio_dash.pricing.refresh import (
    refresh_dividends,
    refresh_history,
    refresh_quotes,
)
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.pricing.store import get_latest_price
from portfolio_dash.scheduler.jobs import (
    DEFAULT_BOARD,
    REPORTING_FX_PAIRS,
    refresh_instrument_quote,
)
from portfolio_dash.shared.config import get_settings
from portfolio_dash.shared.db import session
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument

logger = logging.getLogger(__name__)

_DEFAULT_CCY = {Market.TW: Currency.TWD, Market.US: Currency.USD, Market.MY: Currency.MYR}


class QuickRegisterError(Exception):
    """A quick-registration failure the router maps onto an HTTP error envelope."""

    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class QuickRegisterOutcome(BaseModel):
    instrument: Instrument
    board: str | None
    last: Decimal | None  # instant quote (None only when force-registered quote-less)
    name_source: str  # "provider" | "user" | "none"
    history_points: bool  # whether the initial history backfill stored anything
    restored: bool = False  # FU-D18: True when this call re-activated an archived symbol
    last_price_date: str | None = None  # FU-D18: ISO date of the last stored price (restore)


class InstrumentLookup(BaseModel):
    """FU-D23 fast identification for the quick-add dialog (no history fetch).

    ``found`` is the typo guard: a brand-new symbol is ``found`` only when a provider can
    supply a quote for it (same gate as :func:`quick_register`). An ALREADY-known symbol
    (active or archived) is resolved from stored metadata with NO provider call — keeping the
    duplicate/restore paths deterministic and network-free. ``registered`` = the symbol is
    already active (the dialog must refuse to re-add it); ``archived`` = known but soft-deleted
    (confirming will RESTORE it). ``sector`` is empty for a brand-new symbol — no provider
    sector source exists here; the dialog offers the datalist of existing sectors + free text.
    """

    found: bool
    registered: bool = False
    archived: bool = False
    name: str = ""
    sector: str = ""
    board: str | None = None
    is_etf: bool = False


def lookup_instrument(
    conn: sqlite3.Connection, *, symbol: str, market: Market, now: datetime
) -> InstrumentLookup:
    """Fast identify *symbol* for the quick-add dialog (FU-D23). Never fetches history.

    A KNOWN symbol (registered or archived) is resolved from stored metadata WITHOUT any
    provider call, so the instruments-page duplicate path and the archived-restore path stay
    deterministic. A brand-new symbol is provider-verified (the typo guard): ``found`` is True
    only when a quote can be fetched; the name is looked up best-effort. A provider crash
    degrades to ``found=False`` (the dialog then blocks the confirm), never raising."""
    sym = symbol.strip().upper()
    if not sym:
        return InstrumentLookup(found=False)
    existing = get_instrument(conn, sym)
    if existing is not None:
        return InstrumentLookup(
            found=True,
            registered=not existing.archived,
            archived=existing.archived,
            name=existing.name,
            sector=existing.sector,
            board=existing.board or None,
            is_etf=existing.is_etf,
        )
    # Brand-new symbol: probe the board (TW) then verify existence via a real quote fetch.
    board = probe_tw_board(sym) if market is Market.TW else None
    resolved_board = board if board is not None else (
        None if market is Market.TW else DEFAULT_BOARD[market]
    )
    ref = InstrumentRef(
        symbol=sym, market=market, board=resolved_board or DEFAULT_BOARD[market]
    )
    registry = default_registry(conn)
    quote_ok = False
    try:
        summary = refresh_quotes(conn, registry, [ref], REPORTING_FX_PAIRS, now=now)
        quote_ok = sym in summary.ok
    except Exception:  # noqa: BLE001 — a provider crash degrades like "no quote" (typo guard)
        logger.warning("lookup quote fetch crashed for %s", sym, exc_info=True)
    if not quote_ok:
        return InstrumentLookup(found=False)
    return InstrumentLookup(
        found=True,
        registered=False,
        name=lookup_name(sym, market, board=resolved_board) or "",
        sector="",
        board=resolved_board,
        is_etf=False,
    )


# ---------------------------------------------------------------------------
# FU-D18 — accumulative watchlist: restore + background gap backfill
# ---------------------------------------------------------------------------


def last_price_date(conn: sqlite3.Connection, symbol: str) -> str | None:
    """The most recent stored price date for *symbol* (ISO string) or None.

    This is the "last data on file" surfaced in the restore toast; it is read BEFORE the
    background gap backfill runs, so it reports the pre-restore boundary the backfill fills
    forward from."""
    row = conn.execute(
        "SELECT MAX(as_of_date) AS d FROM prices WHERE instrument=?", (symbol,)
    ).fetchone()
    return row["d"] if row is not None and row["d"] else None


def restore_archived(
    conn: sqlite3.Connection,
    existing: Instrument,
    *,
    name: str = "",
    sector: str = "",
    board: str | None = None,
    quote_ccy: Currency | None = None,
    target_low: Decimal | None = None,
    is_etf: bool = False,
) -> Instrument:
    """Un-archive an already-registered archived instrument and apply any provided metadata
    overrides (FU-D18 restore doors b/c). Caller-supplied non-empty values win; omitted
    fields keep the archived row's stored metadata. This does NOT fetch market data — the
    caller schedules :func:`gap_backfill` (background, doors a/b) or runs it inline (door c).
    """
    merged = existing.model_copy(
        update={
            "name": (name or "").strip() or existing.name,
            "sector": sector or existing.sector,
            "board": board if board is not None else existing.board,
            "quote_ccy": quote_ccy or existing.quote_ccy,
            "target_low": target_low if target_low is not None else existing.target_low,
            "is_etf": is_etf or existing.is_etf,
        }
    )
    upsert_instrument(conn, merged)  # never touches ``archived`` (owned by the flag setter)
    set_instrument_archived(conn, merged.symbol, False)
    saved = get_instrument(conn, merged.symbol)
    assert saved is not None
    return saved


def _first_acquisition(conn: sqlite3.Connection, symbol: str) -> date | None:
    """Earliest acquisition date for *symbol*: min(first BUY trade, opening build).

    Mirrors ``scheduler.jobs.earliest_acquisitions`` restricted to one symbol (that module
    is owned by another wave; the smart-window read is duplicated here, not refactored)."""
    dates: list[date] = []
    for sql in (
        "SELECT MIN(trade_date) AS d FROM transactions WHERE side='BUY' AND symbol=?",
        "SELECT MIN(build_date) AS d FROM opening_inventory WHERE symbol=?",
    ):
        row = conn.execute(sql, (symbol,)).fetchone()
        if row is not None and row["d"]:
            dates.append(date.fromisoformat(row["d"]))
    return min(dates) if dates else None


def _gap_start(conn: sqlite3.Connection, symbol: str, now: datetime) -> date:
    """Backfill start for a restored symbol: last stored price date − 7d overlap, or (when
    nothing is stored yet) the smart window — the 5y config floor extended to the position's
    first acquisition when older. The 7-day overlap is safe: upserts are idempotent, so a
    re-fetch of the boundary rows costs nothing and covers a back-dated or weekend gap."""
    row = conn.execute(
        "SELECT MAX(as_of_date) AS d FROM prices WHERE instrument=?", (symbol,)
    ).fetchone()
    if row is not None and row["d"]:
        return date.fromisoformat(row["d"]) - timedelta(days=7)
    default_start = (now - timedelta(days=get_settings().history_backfill_days)).date()
    first = _first_acquisition(conn, symbol)
    return min(default_start, first) if first is not None else default_start


def _gap_backfill_inner(conn: sqlite3.Connection, symbol: str, now: datetime) -> None:
    inst = get_instrument(conn, symbol)
    if inst is None:  # restored row deleted again before the task ran — nothing to do
        return
    board = inst.board or DEFAULT_BOARD[inst.market]
    ref = InstrumentRef(symbol=symbol, market=inst.market, board=board)
    registry = default_registry(conn)
    start = _gap_start(conn, symbol, now)
    # history + dividends degrade internally (failed keys are summarized, never raised);
    # refresh_instrument_quote CAN raise on a provider crash — run it last so a quote
    # failure never skips the history/dividend backfill.
    refresh_history(conn, registry, [ref], start, now=now)
    refresh_dividends(conn, registry, [ref], now=now)
    refresh_instrument_quote(
        conn, symbol=symbol, market=inst.market, board=inst.board or None, now=now
    )


def gap_backfill(
    symbol: str, *, now: datetime, conn: sqlite3.Connection | None = None
) -> None:
    """Best-effort gap backfill for a RESTORED (un-archived) symbol (FU-D18).

    Refreshes daily price history (from the missing-data window), the latest quote, and
    dividend events for that ONE symbol. Wrapped WHOLLY in try/except: it runs either on
    FastAPI ``BackgroundTasks`` (doors a/b) or inline (door c) and must NEVER fail the
    request or raise into the response.

    ``conn=None`` (the background-task path) opens its OWN session: a FastAPI yield-dependency
    connection is already closed by the time a BackgroundTask runs (FastAPI ≥0.106), so the
    request connection must not be captured. Door (c) passes the live request conn to run the
    identical refresh inline (the /quick spinner already covers the wait)."""
    try:
        if conn is None:
            with session() as own:
                _gap_backfill_inner(own, symbol, now)
        else:
            _gap_backfill_inner(conn, symbol, now)
    except Exception:  # noqa: BLE001 — best-effort; a provider/DB failure never surfaces
        logger.warning("gap backfill failed for %s", symbol, exc_info=True)


def quick_register(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    market: Market,
    now: datetime,
    name: str = "",
    sector: str = "",
    board: str | None = None,
    quote_ccy: Currency | None = None,
    target_low: Decimal | None = None,
    is_etf: bool = False,
    force: bool = False,
    backfill_history: bool = True,
) -> QuickRegisterOutcome:
    """Register *symbol* in one step: probe, fetch a real quote, name it, backfill.

    Raises :class:`QuickRegisterError` — ``duplicate_symbol`` (409) when already
    registered, ``quote_not_found`` (422) when no source supplies a price and
    *force* is False. Quote/history/name fetches are idempotent upserts; history
    and name failures never block the registration itself.

    ``backfill_history=False`` skips the SYNCHRONOUS initial history window: the caller
    (the classic register endpoint, FU-D23) instead schedules the heavy history/dividend
    backfill on FastAPI ``BackgroundTasks`` so the response returns fast (the instant quote
    is still fetched synchronously so ``last`` is populated immediately).
    """
    sym = symbol.strip().upper()
    if not sym:
        raise QuickRegisterError("validation_error", "symbol 不可為空", 400)
    existing = get_instrument(conn, sym)
    if existing is not None:
        if not existing.archived:
            raise QuickRegisterError("duplicate_symbol", f"{sym} 已註冊", 409)
        # FU-D18 door (c): re-adding an ARCHIVED symbol RESTORES it (never a 409) and
        # refreshes its data inline — the same gap-backfill primitive doors a/b run in the
        # background. This is the /quick and inline-quick-add re-add path.
        restored_inst = restore_archived(
            conn, existing, name=name, sector=sector, board=board,
            quote_ccy=quote_ccy, target_low=target_low, is_etf=is_etf,
        )
        gap_backfill(sym, now=now, conn=conn)
        price = get_latest_price(conn, sym, now=now)
        last_date = last_price_date(conn, sym)
        return QuickRegisterOutcome(
            instrument=restored_inst,
            board=restored_inst.board or None,
            last=price.value if price is not None else None,
            name_source="user" if restored_inst.name else "none",
            history_points=last_date is not None,
            restored=True,
            last_price_date=last_date,
        )

    # 1. Board: explicit value respected; TW probed once here (register_instrument
    #    receives the result and must NOT re-probe — no double network call).
    resolved_board = board
    if resolved_board is None and market is Market.TW:
        resolved_board = probe_tw_board(sym)
    if resolved_board is None and market is not Market.TW:
        resolved_board = DEFAULT_BOARD[market]

    # 2. Real-quote gate: fetch the latest quote (+ reporting FX) BEFORE registering.
    #    Price rows are keyed by symbol and idempotent, so writing one for a symbol we
    #    may not register is harmless.
    ref = InstrumentRef(
        symbol=sym, market=market, board=resolved_board or DEFAULT_BOARD[market]
    )
    registry = default_registry(conn)
    quote_ok = False
    try:
        summary = refresh_quotes(conn, registry, [ref], REPORTING_FX_PAIRS, now=now)
        quote_ok = sym in summary.ok
    except Exception:  # noqa: BLE001 — a provider crash degrades like "no quote"
        logger.warning("quick-register quote fetch crashed for %s", sym, exc_info=True)
    if not quote_ok and not force:
        raise QuickRegisterError(
            "quote_not_found",
            f"查無 {sym} 的報價 — 請確認代號與市場是否正確（確定無誤可強制加入）",
            422,
        )

    # 3. Name: caller-supplied wins; otherwise best-effort provider lookup.
    resolved_name = name.strip()
    name_source = "user" if resolved_name else "none"
    if not resolved_name:
        found = lookup_name(sym, market, board=resolved_board)
        if found:
            resolved_name, name_source = found, "provider"

    # 4. Persist (register_instrument handles the unresolved-TW-board soft state).
    inst = Instrument(
        symbol=sym, market=market, quote_ccy=quote_ccy or _DEFAULT_CCY[market],
        sector=sector, name=resolved_name, board=resolved_board or "",
        target_low=target_low, is_etf=is_etf,
    )
    register_instrument(conn, inst, prober=None, confirm=True)

    # 5. Initial history window (config-driven, 5y default; owner 2026-07-08) —
    #    best-effort, never blocks registration. Skipped when ``backfill_history=False``
    #    (the caller offloads the heavy history fetch to a background gap_backfill).
    history_points = False
    if backfill_history:
        try:
            start = (now - timedelta(days=get_settings().history_backfill_days)).date()
            hist_summary = refresh_history(conn, registry, [ref], start, now=now)
            history_points = sym in hist_summary.ok
        except Exception:  # noqa: BLE001 — presentation backfill must not fail the write
            logger.warning("quick-register history backfill failed for %s", sym, exc_info=True)

    saved = get_instrument(conn, sym)
    assert saved is not None
    price = get_latest_price(conn, sym, now=now)
    return QuickRegisterOutcome(
        instrument=saved,
        board=resolved_board,
        last=price.value if price is not None else None,
        name_source=name_source,
        history_points=history_points,
    )
