"""待確認退款（折讓款）forecaster — compute-on-read, mirrors dividend_inbox (Wave B, FE-D1).

The TW 券商 charge-first (先收後退) model charges the FULL commission at settlement and
refunds a fixed fraction the FOLLOWING month, confirmed off-ledger. That rebate is NEVER
money of record and NEVER enters cost basis / P&L / XIRR (FE-D1): ``compute_fees`` charges
the full price and never reads ``rebate_rate``. This module only FORECASTS the expected
monthly refund — per trade ``floor(fee × rebate_rate)`` (delegated to
:func:`data_ingestion.fees.forecast_tw_rebate`) — and surfaces it as a pending-confirmation
inbox item. On ACTUAL receipt the owner confirms → a cash-pool credit (movement kind
``rebate``) with an EDITABLE amount (the estimate is only a prefill; actual wins).

State: NONE except a skip table (``rebate_skips``). A month becomes PENDING on the 1st of
the FOLLOWING month; it is suppressed when (a) a cash movement (kind ``rebate``) carrying
the month's note tag 「YYYY-MM 折讓款」 already exists for that account, or (b) the month is
skipped. Self-healing — nothing is auto-written, and confirm recomputes/validates
server-side. Mirrors the dividend-inbox posture (compute-on-read, ungated in guest mode).
"""

import sqlite3
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel

from portfolio_dash.data_ingestion.config_seed import get_fee_rule_set
from portfolio_dash.data_ingestion.fees import forecast_tw_rebate
from portfolio_dash.data_ingestion.store import (
    list_accounts,
    list_cash_movements,
    list_transactions,
)
from portfolio_dash.shared.models.assets import Account

_ZERO = Decimal("0")
# The cash movement kind that BOOKS a confirmed rebate (deposit-like credit). Stored
# uppercase like the other movement kinds; ``portfolio.cash`` credits any non-WITHDRAW kind.
REBATE_KIND = "REBATE"

_SKIP_DDL = """
CREATE TABLE IF NOT EXISTS rebate_skips (
    account_id TEXT NOT NULL,
    month TEXT NOT NULL,
    skipped_at TEXT NOT NULL,
    PRIMARY KEY (account_id, month)
);
"""


def month_tag(month: str) -> str:
    """The deterministic note fingerprint a confirmed rebate credit carries.

    Re-derived server-side at confirm AND matched by :func:`detect`'s suppression, so a
    booked month never re-surfaces. ``month`` is a ``YYYY-MM`` string.
    """
    return f"{month} 折讓款"


def _month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _is_pending(month: str, now: datetime) -> bool:
    """A trade month is PENDING once the clock has advanced past it (1st of the next month)."""
    my, mm = int(month[:4]), int(month[5:7])
    return (my, mm) < (now.year, now.month)


class PendingRebate(BaseModel):
    """One month's forecast rebate awaiting the owner's receipt confirmation.

    ``expected`` is Σ per-trade ``floor(fee × rebate_rate)`` — a FORECAST, never money of
    record. The confirm amount is editable (this is only the prefill).
    """

    account_id: str
    account_name: str
    month: str  # "YYYY-MM"
    trade_count: int
    fee_total: Decimal
    expected: Decimal
    ccy: str


class SkippedRebate(BaseModel):
    """A previously-skipped month for the 「已略過」 un-skip list.

    ``detail`` carries the re-detected forecast when the month is still detectable; else None.
    """

    account_id: str
    account_name: str
    month: str
    skipped_at: str
    detail: PendingRebate | None = None


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(_SKIP_DDL)
    conn.commit()


def _rebate_accounts(conn: sqlite3.Connection) -> dict[str, tuple[Account, Decimal]]:
    """account_id -> (account, rebate_rate) for every account whose rule set rebates (>0)."""
    rule_by_acct = {
        r["account_id"]: r["fee_rule_set"]
        for r in conn.execute("SELECT account_id, fee_rule_set FROM accounts")
    }
    out: dict[str, tuple[Account, Decimal]] = {}
    for a in list_accounts(conn):
        rule_name = rule_by_acct.get(a.account_id)
        if rule_name is None:
            continue
        rate = get_fee_rule_set(rule_name).rebate_rate
        if rate > _ZERO:
            out[a.account_id] = (a, rate)
    return out


def _skips(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    ensure_tables(conn)
    return {
        (r["account_id"], r["month"])
        for r in conn.execute("SELECT account_id, month FROM rebate_skips")
    }


def detect(
    conn: sqlite3.Connection, *, now: datetime, include_skipped: bool = False
) -> list[PendingRebate]:
    """Compute the current pending-rebate list — pure read, self-healing (no rows stored).

    Groups each rebate account's fee-bearing trades by calendar month, sums the per-trade
    floor forecast, and keeps only months that are (a) pending (past the following month's
    1st), (b) not suppressed by a matching ``rebate`` cash movement, and (c) not skipped
    (unless ``include_skipped``, used by :func:`list_skipped` to reconstruct detail).
    """
    accts = _rebate_accounts(conn)
    if not accts:
        return []
    skips: set[tuple[str, str]] = set() if include_skipped else _skips(conn)
    # A confirmed rebate carries the month's note tag -> that month is already booked.
    confirmed = {
        (m.account_id, m.note)
        for m in list_cash_movements(conn)
        if m.kind.upper() == REBATE_KIND and m.note
    }

    # (account_id, month) -> [trade_count, fee_total, expected]
    agg: dict[tuple[str, str], list[Decimal]] = {}
    counts: dict[tuple[str, str], int] = {}
    for t in list_transactions(conn):
        if t.account_id not in accts:
            continue
        if t.fees is None or t.fees <= _ZERO:  # skip fee-free rows (nothing to rebate)
            continue
        rate = accts[t.account_id][1]
        key = (t.account_id, _month_key(t.trade_date))
        cell = agg.setdefault(key, [_ZERO, _ZERO])
        cell[0] += t.fees
        cell[1] += forecast_tw_rebate(t.fees, rate)
        counts[key] = counts.get(key, 0) + 1

    out: list[PendingRebate] = []
    for (account_id, month), (fee_total, expected) in agg.items():
        if not _is_pending(month, now):
            continue
        if (account_id, month) in skips:
            continue
        if (account_id, month_tag(month)) in confirmed:
            continue
        account = accts[account_id][0]
        out.append(PendingRebate(
            account_id=account_id, account_name=account.name, month=month,
            trade_count=counts[(account_id, month)], fee_total=fee_total,
            expected=expected, ccy=account.settlement_ccy.value,
        ))
    out.sort(key=lambda p: (p.month, p.account_id), reverse=True)
    return out


def pending_count(conn: sqlite3.Connection, *, now: datetime) -> int:
    """Pending-rebate count for the sidebar badge (summed with the dividend inbox)."""
    return len(detect(conn, now=now))


def mark_skipped(
    conn: sqlite3.Connection, account_id: str, month: str, *, now: datetime
) -> None:
    ensure_tables(conn)
    conn.execute(
        "INSERT INTO rebate_skips (account_id, month, skipped_at) VALUES (?, ?, ?) "
        "ON CONFLICT(account_id, month) DO NOTHING",
        (account_id, month, now.isoformat()),
    )
    conn.commit()


def unskip(conn: sqlite3.Connection, items: list[tuple[str, str]]) -> int:
    """Remove skip marks so the months re-surface. Returns rows deleted."""
    ensure_tables(conn)
    removed = 0
    for account_id, month in items:
        cur = conn.execute(
            "DELETE FROM rebate_skips WHERE account_id=? AND month=?", (account_id, month)
        )
        removed += cur.rowcount
    conn.commit()
    return removed


def list_skipped(conn: sqlite3.Connection, *, now: datetime) -> list[SkippedRebate]:
    """The skipped-month list with reconstructable detail (newest skip first).

    Re-runs detection with the skip filter OFF to recover each skipped month's forecast when
    it is still detectable; a month no longer detectable (booked, or its trades gone) keeps
    only account/month.
    """
    ensure_tables(conn)
    rows = conn.execute(
        "SELECT account_id, month, skipped_at FROM rebate_skips "
        "ORDER BY skipped_at DESC, account_id, month"
    ).fetchall()
    if not rows:
        return []
    by_key = {(p.account_id, p.month): p for p in detect(conn, now=now, include_skipped=True)}
    accts = _rebate_accounts(conn)
    out: list[SkippedRebate] = []
    for r in rows:
        key = (r["account_id"], r["month"])
        detail = by_key.get(key)
        name = accts[r["account_id"]][0].name if r["account_id"] in accts else r["account_id"]
        out.append(SkippedRebate(
            account_id=r["account_id"], account_name=name, month=r["month"],
            skipped_at=r["skipped_at"], detail=detail,
        ))
    return out
