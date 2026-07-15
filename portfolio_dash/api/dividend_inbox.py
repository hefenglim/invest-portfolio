"""配息偵測 → 待確認匯入 (R4 item 1; R5 expansion 2026-07-03).

Turns fetched dividend EVENTS (pricing's ``dividend_events`` — FinMind for TW,
yfinance for US/MY) into a user-confirmable inbox across ALL markets, booking
per the ACCOUNT's dividend model on confirm:

- ``cash_cost_reduction`` (TW broker) — CASH row, net = gross (成本沖減 applies
  on rebuild). TW events may ALSO carry a stock distribution
  (StockEarningsDistribution, 元 of par value): booked as a separate STOCK item
  whose share count = held × amount / 10 (面額 10 元 convention).
- ``drip_us`` (Schwab / Moomoo US) — DRIP row: 30% withholding, net reinvested
  at an ESTIMATED price (last stored close on-or-before the pay/ex date;
  clearly marked, editable in the ledger afterwards). No stored price →
  the item is NOT confirmable (缺再投資價) until history is backfilled.
- ``cash`` (Moomoo MY) — NET row (single-tier net received).

Detection window per symbol = its earliest acquisition date → today;
entitlement = shares held going INTO the ex-date. Suppression: a ledger row of
the SAME family (cash-like vs stock) within ±45 days of the ex-date, or an
explicit skip (fingerprint persisted). Pending items are computed on read —
self-healing, nothing auto-written, 絕不自動入帳; confirm recomputes
server-side.
"""

import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel

from portfolio_dash.data_ingestion.holdings import shares_on
from portfolio_dash.data_ingestion.store import (
    insert_dividend,
    list_accounts,
    list_dividends,
    list_instruments,
)
from portfolio_dash.pricing.defaults import default_registry
from portfolio_dash.pricing.refresh import refresh_dividends
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.pricing.results import DividendEvent
from portfolio_dash.pricing.store import get_dividend_events, get_price_history
from portfolio_dash.scheduler.jobs import DEFAULT_BOARD, earliest_acquisitions
from portfolio_dash.shared.enums import Market
from portfolio_dash.shared.models.assets import Instrument

_ZERO = Decimal("0")
_US_WITHHOLDING = Decimal("0.30")
_TW_STOCK_PAR = Decimal("10")  # 股票股利以面額 10 元計: X 元 → X/10 股 per share

# A ledger dividend row (same family) within this window of an event's ex-date
# means the event is already recorded (payout lands ~2-6 weeks after ex).
_MATCH_WINDOW_DAYS = 45
# How far back to look for the DRIP reinvest-price estimate.
_PRICE_LOOKBACK_DAYS = 14

_SKIP_DDL = """
CREATE TABLE IF NOT EXISTS pending_dividend_skips (
    fingerprint TEXT PRIMARY KEY,
    skipped_at TEXT NOT NULL
);
"""

# ledger type -> suppression family ("cash-like" money rows vs share-only rows)
_FAMILY = {"CASH": "cash", "DRIP": "cash", "NET": "cash", "STOCK": "stock"}


class PendingDividend(BaseModel):
    """One confirmable detection (wire-ready; Decimals serialize via to_wire)."""

    fingerprint: str
    kind: str  # cash | drip | net | stock — the booking model on confirm
    source: str
    account_id: str
    account_name: str
    symbol: str
    name: str
    ex_date: date
    pay_date: date | None
    per_share: Decimal  # cash per share (stock items: 股票股利 in 元 of par)
    shares_held: Decimal
    est_gross: Decimal
    est_withhold: Decimal
    est_net: Decimal
    est_reinvest_price: Decimal | None = None  # DRIP estimate (stored close)
    est_reinvest_shares: Decimal | None = None  # DRIP net/price · STOCK added shares
    ccy: str
    confirmable: bool = True
    note: str | None = None


class SkippedDividend(BaseModel):
    """A previously-skipped detection (3E), for the 「已忽略」 un-skip list.

    ``detail`` carries the full re-detected item when it is STILL detectable (the event +
    holding still exist and it is not otherwise recorded); when it is no longer detectable
    only ``fingerprint`` + the ``symbol``/``ex_date`` parsed from the fingerprint are known.
    """

    fingerprint: str
    skipped_at: str
    symbol: str | None = None
    ex_date: date | None = None
    detail: PendingDividend | None = None


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(_SKIP_DDL)
    conn.commit()


def mark_skipped(conn: sqlite3.Connection, fingerprint: str, *, now: datetime) -> None:
    ensure_tables(conn)
    conn.execute(
        "INSERT INTO pending_dividend_skips (fingerprint, skipped_at) VALUES (?, ?) "
        "ON CONFLICT(fingerprint) DO NOTHING",
        (fingerprint, now.isoformat()),
    )
    conn.commit()


def unskip(conn: sqlite3.Connection, fingerprints: list[str]) -> int:
    """Remove skip marks so the items re-surface in the inbox (3E). Returns rows deleted."""
    ensure_tables(conn)
    removed = 0
    for fp in fingerprints:
        cur = conn.execute(
            "DELETE FROM pending_dividend_skips WHERE fingerprint = ?", (fp,)
        )
        removed += cur.rowcount
    conn.commit()
    return removed


def _skipped(conn: sqlite3.Connection) -> set[str]:
    ensure_tables(conn)
    return {r["fingerprint"] for r in conn.execute(
        "SELECT fingerprint FROM pending_dividend_skips")}


def _skipped_at(conn: sqlite3.Connection) -> dict[str, str]:
    """fingerprint -> skipped_at, newest first (drives the 「已忽略」 list ordering)."""
    ensure_tables(conn)
    return {
        r["fingerprint"]: r["skipped_at"]
        for r in conn.execute(
            "SELECT fingerprint, skipped_at FROM pending_dividend_skips "
            "ORDER BY skipped_at DESC, fingerprint"
        )
    }


def _fp_parts(fingerprint: str) -> tuple[str | None, date | None]:
    """Best-effort (symbol, ex_date) parse of a ``div:acct:symbol:YYYY-MM-DD[:stock]`` fp."""
    parts = fingerprint.split(":")
    if len(parts) < 4 or parts[0] != "div":
        return None, None
    symbol = parts[2] or None
    try:
        ex = date.fromisoformat(parts[3])
    except ValueError:
        ex = None
    return symbol, ex


def list_skipped(conn: sqlite3.Connection, *, now: datetime) -> list[SkippedDividend]:
    """The skipped-fingerprint list with as much reconstructable detail as detection allows.

    Runs detection with the skip filter OFF to recover the full item for each skipped
    fingerprint that is still detectable; fingerprints no longer detectable degrade to
    symbol/ex_date parsed from the fingerprint (or bare fingerprint if unparseable).
    """
    skipped = _skipped_at(conn)
    if not skipped:
        return []
    by_fp = {p.fingerprint: p for p in detect(conn, now=now, include_skipped=True)}
    out: list[SkippedDividend] = []
    for fp, at in skipped.items():
        p = by_fp.get(fp)
        if p is not None:
            out.append(SkippedDividend(
                fingerprint=fp, skipped_at=at, symbol=p.symbol, ex_date=p.ex_date, detail=p))
        else:
            sym, ex = _fp_parts(fp)
            out.append(SkippedDividend(fingerprint=fp, skipped_at=at, symbol=sym, ex_date=ex))
    return out


def refresh_events_for_acquired(conn: sqlite3.Connection, *, now: datetime) -> str:
    """Targeted event fetch for every symbol (any market) with an acquisition history.

    TW routes to FinMind (fetches since 2015); US/MY route to yfinance (full
    dividend series). Returns a short human summary for the panel toast.
    """
    acq = earliest_acquisitions(conn)
    refs = [
        InstrumentRef(symbol=i.symbol, market=i.market,
                      board=i.board or DEFAULT_BOARD[i.market])
        for i in list_instruments(conn)
        if i.symbol in acq
    ]
    if not refs:
        return "無持倉可偵測"
    summary = refresh_dividends(conn, default_registry(conn), refs, now=now)
    return f"{len(summary.ok)} 檔事件已更新, {len(summary.failed)} 檔失敗"


def _price_on_or_before(
    conn: sqlite3.Connection, symbol: str, target: date
) -> Decimal | None:
    """Last stored close on-or-before *target* (bounded lookback), or None."""
    hist = get_price_history(
        conn, symbol, target - timedelta(days=_PRICE_LOOKBACK_DAYS), target)
    return hist[-1].value if hist else None


def detect(
    conn: sqlite3.Connection, *, now: datetime, include_skipped: bool = False
) -> list[PendingDividend]:
    """Compute the current inbox — pure read, self-healing (no pending rows stored).

    ``include_skipped=True`` ignores the skip filter so :func:`list_skipped` (3E) can
    reconstruct the detail of previously-skipped fingerprints; the default excludes them.
    """
    instruments = {i.symbol: i for i in list_instruments(conn)}
    accounts = {a.account_id: a for a in list_accounts(conn)}
    acq = earliest_acquisitions(conn)
    skips: set[str] = set() if include_skipped else _skipped(conn)
    today = now.date()

    # Ledger dividend dates per (account, symbol, family) — the recorded guard.
    ledger_dates: dict[tuple[str, str, str], list[date]] = {}
    for d in list_dividends(conn):
        family = _FAMILY.get(d.type, "cash")
        ledger_dates.setdefault((d.account_id, d.symbol, family), []).append(d.date)

    def recorded(account_id: str, symbol: str, family: str, ex: date) -> bool:
        return any(
            abs((d - ex).days) <= _MATCH_WINDOW_DAYS
            for d in ledger_dates.get((account_id, symbol, family), [])
        )

    out: list[PendingDividend] = []
    for symbol, first_date in sorted(acq.items()):
        inst = instruments.get(symbol)
        if inst is None:
            continue
        for ev in get_dividend_events(conn, symbol):
            if ev.ex_date < first_date or ev.ex_date > today:
                continue
            for account_id, account in accounts.items():
                held = shares_on(conn, account_id, symbol, before=ev.ex_date)
                if held <= _ZERO:
                    continue
                model = account.dividend_model

                def _mk(  # noqa: PLR0913 — a typed builder beats an untyped **dict
                    fingerprint: str, kind: str, per_share: Decimal,
                    est_gross: Decimal, est_withhold: Decimal, est_net: Decimal,
                    *, est_reinvest_price: Decimal | None = None,
                    est_reinvest_shares: Decimal | None = None,
                    confirmable: bool = True, note: str | None = None,
                    _acct: str = account_id, _acct_name: str = account.name,
                    _sym: str = symbol, _held: Decimal = held,
                    _ev: DividendEvent = ev, _inst: Instrument = inst,
                ) -> PendingDividend:
                    return PendingDividend(
                        fingerprint=fingerprint, kind=kind, source=_ev.source,
                        account_id=_acct, account_name=_acct_name, symbol=_sym,
                        name=_inst.name or _sym, ex_date=_ev.ex_date,
                        pay_date=_ev.pay_date, per_share=per_share,
                        shares_held=_held, est_gross=est_gross,
                        est_withhold=est_withhold, est_net=est_net,
                        est_reinvest_price=est_reinvest_price,
                        est_reinvest_shares=est_reinvest_shares,
                        ccy=_inst.quote_ccy.value, confirmable=confirmable, note=note,
                    )

                # --- cash-family item (CASH / DRIP / NET by account model) ---
                if ev.cash_amount is not None and ev.cash_amount > _ZERO:
                    fp = f"div:{account_id}:{symbol}:{ev.ex_date.isoformat()}"
                    if fp not in skips and not recorded(
                        account_id, symbol, "cash", ev.ex_date
                    ):
                        gross = ev.cash_amount * held
                        if model == "drip_us":
                            wh = gross * _US_WITHHOLDING
                            net = gross - wh
                            px = _price_on_or_before(
                                conn, symbol, ev.pay_date or ev.ex_date)
                            out.append(_mk(
                                fp, "drip", ev.cash_amount, gross, wh, net,
                                est_reinvest_price=px,
                                est_reinvest_shares=(net / px) if px else None,
                                confirmable=px is not None,
                                note=(None if px is not None else
                                      "缺再投資價（無庫存收盤價）— 請先回補歷史報價再確認"),
                            ))
                        elif model == "cash":
                            out.append(_mk(fp, "net", ev.cash_amount, gross,
                                           _ZERO, gross, note="馬股單層淨額入帳"))
                        else:  # cash_cost_reduction (TW) and any future default
                            out.append(_mk(fp, "cash", ev.cash_amount, gross,
                                           _ZERO, gross))

                # --- TW stock distribution (配股): separate share-only item ---
                if (
                    ev.stock_amount is not None and ev.stock_amount > _ZERO
                    and inst.market is Market.TW and model == "cash_cost_reduction"
                ):
                    fp_s = f"div:{account_id}:{symbol}:{ev.ex_date.isoformat()}:stock"
                    if fp_s not in skips and not recorded(
                        account_id, symbol, "stock", ev.ex_date
                    ):
                        added = held * ev.stock_amount / _TW_STOCK_PAR
                        out.append(_mk(
                            fp_s, "stock", ev.stock_amount, _ZERO, _ZERO, _ZERO,
                            est_reinvest_shares=added,
                            note=f"配股 {ev.stock_amount} 元（面額制）→ 每股配 "
                                 f"{ev.stock_amount}/10 股，$0 成本入帳",
                        ))
    out.sort(key=lambda p: (p.ex_date, p.symbol), reverse=True)
    return out


def confirm(
    conn: sqlite3.Connection, fingerprints: list[str], *, now: datetime
) -> list[int]:
    """Book one ledger row per confirmed fingerprint (per account model); ids back.

    Values are RECOMPUTED from the current detection (client numbers are display
    only). Unknown / already-resolved / non-confirmable fingerprints are ignored,
    so a double-click confirms once and a 缺再投資價 DRIP cannot be forced through.
    """
    by_fp = {p.fingerprint: p for p in detect(conn, now=now)}
    written: list[int] = []
    for fp in fingerprints:
        p = by_fp.get(fp)
        if p is None or not p.confirmable:
            continue
        div_date = p.pay_date or p.ex_date
        if p.kind == "drip":
            row_id = insert_dividend(
                conn, account_id=p.account_id, symbol=p.symbol, div_date=div_date,
                div_type="DRIP", gross=p.est_gross, withholding=p.est_withhold,
                net=p.est_net, reinvest_shares=p.est_reinvest_shares,
                reinvest_price=p.est_reinvest_price,
            )
        elif p.kind == "net":
            row_id = insert_dividend(
                conn, account_id=p.account_id, symbol=p.symbol, div_date=div_date,
                div_type="NET", gross=p.est_gross, withholding=_ZERO, net=p.est_net,
            )
        elif p.kind == "stock":
            row_id = insert_dividend(
                conn, account_id=p.account_id, symbol=p.symbol, div_date=div_date,
                div_type="STOCK", gross=_ZERO, withholding=_ZERO, net=_ZERO,
                reinvest_shares=p.est_reinvest_shares,
            )
        else:  # cash (TW 沖減)
            row_id = insert_dividend(
                conn, account_id=p.account_id, symbol=p.symbol, div_date=div_date,
                div_type="CASH", gross=p.est_gross, withholding=_ZERO, net=p.est_net,
            )
        written.append(row_id)
    return written


def scan_job(conn: sqlite3.Connection, *, now: datetime) -> str:
    """Scheduler-dispatched daily scan: refresh events + report the pending count.

    Registered into ``scheduler.jobs`` at app startup (runner seam — scheduler
    never imports api), so the inbox grows by itself and the run history shows
    how many items await the user.
    """
    refreshed = refresh_events_for_acquired(conn, now=now)
    pending = len(detect(conn, now=now))
    return f"{refreshed} · 待確認 {pending} 筆"
