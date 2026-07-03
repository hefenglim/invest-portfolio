"""FinMind 配息偵測 → 待確認匯入 (2026-07-03, R4 item 1, human decision A).

Turns fetched dividend EVENTS (pricing's ``dividend_events`` table — FinMind for
TW, since 2015) into a user-confirmable inbox:

- Detection window per symbol = its EARLIEST acquisition date (first BUY or
  opening build) → today, so a long-held position surfaces every distribution it
  was entitled to.
- Entitlement: shares held going INTO the ex-date (``holdings.shares_on``).
- An item disappears when the dividend LEDGER already has a row for the same
  (account, symbol) within ±45 days of the ex-date (already recorded, however it
  got there), or when the user explicitly skipped it (fingerprint persisted).
- CONFIRM recomputes server-side (never trusts client numbers) and writes a CASH
  dividend row (TW model: net = gross, withholding 0) — the existing
  adjusted-cost accounting then applies on rebuild. 絕不自動入帳: nothing is
  written without an explicit confirm.

v1 scope: TW cash dividends (the user's directive). US DRIP needs broker data
(reinvest shares/price) and MY events are a straightforward extension — both
recorded as roadmap in the round report.
"""

import sqlite3
from datetime import date, datetime
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
from portfolio_dash.pricing.store import get_dividend_events
from portfolio_dash.scheduler.jobs import earliest_acquisitions
from portfolio_dash.shared.enums import Market

_ZERO = Decimal("0")

# A ledger dividend row within this window of an event's ex-date means the event
# is already recorded (payout typically lands ~2-6 weeks after ex).
_MATCH_WINDOW_DAYS = 45

_SKIP_DDL = """
CREATE TABLE IF NOT EXISTS pending_dividend_skips (
    fingerprint TEXT PRIMARY KEY,
    skipped_at TEXT NOT NULL
);
"""


class PendingDividend(BaseModel):
    """One confirmable detection (wire-ready; Decimals serialize via to_wire)."""

    fingerprint: str
    source: str
    account_id: str
    account_name: str
    symbol: str
    name: str
    ex_date: date
    pay_date: date | None
    per_share: Decimal
    shares_held: Decimal
    est_gross: Decimal  # per_share × shares_held; TW cash: net == gross
    ccy: str


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


def _skipped(conn: sqlite3.Connection) -> set[str]:
    ensure_tables(conn)
    return {r["fingerprint"] for r in conn.execute(
        "SELECT fingerprint FROM pending_dividend_skips")}


def refresh_events_for_acquired(conn: sqlite3.Connection, *, now: datetime) -> str:
    """Targeted event fetch for every TW symbol with an acquisition history.

    FinMind's dividend dataset is fetched from 2015 onward, so one sweep covers
    any acquisition window. Returns a short human summary for the panel toast.
    """
    acq = earliest_acquisitions(conn)
    refs = [
        InstrumentRef(symbol=i.symbol, market=i.market, board=i.board or "TWSE")
        for i in list_instruments(conn)
        if i.market is Market.TW and i.symbol in acq
    ]
    if not refs:
        return "無台股持倉可偵測"
    summary = refresh_dividends(conn, default_registry(conn), refs, now=now)
    return f"{len(summary.ok)} 檔事件已更新, {len(summary.failed)} 檔失敗"


def detect(conn: sqlite3.Connection, *, now: datetime) -> list[PendingDividend]:
    """Compute the current inbox — pure read, self-healing (no pending rows stored)."""
    instruments = {i.symbol: i for i in list_instruments(conn)}
    accounts = {a.account_id: a for a in list_accounts(conn)}
    acq = earliest_acquisitions(conn)
    skips = _skipped(conn)
    today = now.date()

    # Ledger dividend dates per (account, symbol) — the already-recorded guard.
    ledger_dates: dict[tuple[str, str], list[date]] = {}
    for d in list_dividends(conn):
        ledger_dates.setdefault((d.account_id, d.symbol), []).append(d.date)

    out: list[PendingDividend] = []
    for symbol, first_date in sorted(acq.items()):
        inst = instruments.get(symbol)
        if inst is None or inst.market is not Market.TW:  # v1: TW cash only
            continue
        for ev in get_dividend_events(conn, symbol):
            if ev.cash_amount is None or ev.cash_amount <= _ZERO:
                continue  # stock-only events: 配股請手動入帳 (v1)
            if ev.ex_date < first_date or ev.ex_date > today:
                continue
            for account_id, account in accounts.items():
                held = shares_on(conn, account_id, symbol, before=ev.ex_date)
                if held <= _ZERO:
                    continue
                fingerprint = f"div:{account_id}:{symbol}:{ev.ex_date.isoformat()}"
                if fingerprint in skips:
                    continue
                near = any(
                    abs((d - ev.ex_date).days) <= _MATCH_WINDOW_DAYS
                    for d in ledger_dates.get((account_id, symbol), [])
                )
                if near:
                    continue
                out.append(PendingDividend(
                    fingerprint=fingerprint,
                    source=ev.source,
                    account_id=account_id,
                    account_name=account.name,
                    symbol=symbol,
                    name=inst.name or symbol,
                    ex_date=ev.ex_date,
                    pay_date=ev.pay_date,
                    per_share=ev.cash_amount,
                    shares_held=held,
                    est_gross=ev.cash_amount * held,
                    ccy=inst.quote_ccy.value,
                ))
    out.sort(key=lambda p: (p.ex_date, p.symbol), reverse=True)
    return out


def confirm(
    conn: sqlite3.Connection, fingerprints: list[str], *, now: datetime
) -> list[int]:
    """Write a CASH dividend ledger row per confirmed fingerprint; return row ids.

    Values are RECOMPUTED from the current detection (client numbers are display
    only). Unknown / already-resolved fingerprints are ignored (idempotent-ish:
    a double-click confirms once, because the first write makes the ledger-match
    guard suppress the item).
    """
    by_fp = {p.fingerprint: p for p in detect(conn, now=now)}
    written: list[int] = []
    for fp in fingerprints:
        p = by_fp.get(fp)
        if p is None:
            continue
        row_id = insert_dividend(
            conn,
            account_id=p.account_id,
            symbol=p.symbol,
            div_date=p.pay_date or p.ex_date,
            div_type="CASH",
            gross=p.est_gross,
            withholding=_ZERO,
            net=p.est_gross,
        )
        written.append(row_id)
    return written
