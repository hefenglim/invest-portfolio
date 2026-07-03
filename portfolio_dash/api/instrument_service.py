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
from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel

from portfolio_dash.data_ingestion.register import register_instrument
from portfolio_dash.data_ingestion.store import get_instrument
from portfolio_dash.pricing.board import probe_tw_board
from portfolio_dash.pricing.defaults import default_registry
from portfolio_dash.pricing.names import lookup_name
from portfolio_dash.pricing.refresh import refresh_history, refresh_quotes
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.pricing.store import get_latest_price
from portfolio_dash.scheduler.jobs import DEFAULT_BOARD, REPORTING_FX_PAIRS
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument

logger = logging.getLogger(__name__)

_DEFAULT_CCY = {Market.TW: Currency.TWD, Market.US: Currency.USD, Market.MY: Currency.MYR}

# Initial history window (2026-07-03, user decision — supersedes the 92-day round-2
# value): 12 months of daily closes. A freshly added symbol has no position yet, so
# the default window applies; positions predating 12 months get their fuller window
# through the smart backfill action (scheduler.jobs.backfill_history_all).
HISTORY_BACKFILL_DAYS = 365


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
) -> QuickRegisterOutcome:
    """Register *symbol* in one step: probe, fetch a real quote, name it, backfill.

    Raises :class:`QuickRegisterError` — ``duplicate_symbol`` (409) when already
    registered, ``quote_not_found`` (422) when no source supplies a price and
    *force* is False. Quote/history/name fetches are idempotent upserts; history
    and name failures never block the registration itself.
    """
    sym = symbol.strip().upper()
    if not sym:
        raise QuickRegisterError("validation_error", "symbol 不可為空", 400)
    if get_instrument(conn, sym) is not None:
        raise QuickRegisterError("duplicate_symbol", f"{sym} 已註冊", 409)

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

    # 5. Initial 3-month history window — best-effort, never blocks registration.
    history_points = False
    try:
        start = (now - timedelta(days=HISTORY_BACKFILL_DAYS)).date()
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
