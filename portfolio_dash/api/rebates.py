"""待確認退款（折讓款）forecaster — compute-on-read, mirrors dividend_inbox (Wave B, FE-D1).

The TW 券商 charge-first (先收後退) model charges the FULL commission at settlement and
refunds a fixed fraction the FOLLOWING month, confirmed off-ledger. That rebate is NEVER
money of record and NEVER enters cost basis / P&L / XIRR (FE-D1): ``compute_fees`` charges
the full price and never reads ``rebate_rate``. This module only FORECASTS the expected
monthly refund — per trade ``floor(fee × rebate_rate)`` (delegated to
:func:`data_ingestion.fees.forecast_tw_rebate`) — and surfaces it as a pending-confirmation
inbox item. On ACTUAL receipt the owner confirms → a cash-pool credit (movement kind
``rebate``) with an EDITABLE amount (the estimate is only a prefill; actual wins).

State: NONE except a skip table (``rebate_skips``). A month becomes PENDING (confirmable)
on the 1st of the FOLLOWING month; before that it is ACCRUING — surfaced by
:func:`detect_accruing` as a NON-confirmable forecast (owner #1) so a trade entered THIS
month is visible immediately, not only next month. A pending month is suppressed when
(a) a confirmed rebate cash movement (kind ``rebate``) for that account maps back to it —
DUAL-KEYED by BOTH the movement's date (structural: the trade month is the month before the
refund date, robust to a note edit) AND the 「YYYY-MM 折讓款」 note tag (documented contract) —
or (b) the month is skipped. The dual key is the double-credit guard (F2d/F12): a booked
month cannot re-surface after its (user-editable) note is changed. Self-healing — nothing is
auto-written, and confirm recomputes/validates server-side. Mirrors the dividend-inbox
posture (compute-on-read, ungated in guest mode).
"""

import sqlite3
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel

from portfolio_dash.data_ingestion.config_seed import get_fee_rule_set
from portfolio_dash.data_ingestion.fees import forecast_tw_rebate
from portfolio_dash.data_ingestion.rules_binding import rule_sets_for
from portfolio_dash.data_ingestion.store import (
    list_accounts,
    list_cash_movements,
    list_instruments,
    list_transactions,
)
from portfolio_dash.shared.models.assets import Account
from portfolio_dash.shared.models.enums import Side

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


_TAG_SUFFIX = " 折讓款"


def month_tag(month: str) -> str:
    """The deterministic note fingerprint a confirmed rebate credit carries.

    Re-derived server-side at confirm AND matched by :func:`detect`'s suppression, so a
    booked month never re-surfaces. ``month`` is a ``YYYY-MM`` string.
    """
    return f"{month}{_TAG_SUFFIX}"


def _month_from_tag(note: str) -> str | None:
    """Recover the ``YYYY-MM`` a confirmed rebate note tags — the inverse of :func:`month_tag`.

    Returns None when *note* is not a well-formed tag (e.g. the owner edited it). Used
    ALONGSIDE the structural date key so a booked month stays suppressed whether it is still
    recognised by its (editable) note or by the movement's date.
    """
    if not note.endswith(_TAG_SUFFIX):
        return None
    head = note[: -len(_TAG_SUFFIX)]
    if len(head) == 7 and head[4] == "-" and head[:4].isdigit() and head[5:].isdigit():
        return head
    return None


def _month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _prev_month(year: int, month: int) -> tuple[int, int]:
    """The calendar month immediately before ``(year, month)``."""
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _is_pending(month: str, now: datetime) -> bool:
    """A trade month is PENDING once the clock has advanced past it (1st of the next month).

    Before that boundary the month is ACCRUING (see :func:`detect_accruing`): the refund is
    not yet due, so the month is informational and NOT confirmable.
    """
    my, mm = int(month[:4]), int(month[5:7])
    return (my, mm) < (now.year, now.month)


class RebateTrade(BaseModel):
    """One fee-bearing trade contributing to a month's forecast rebate (§3.6 breakdown).

    ``expected`` is this single trade's ``floor(fee × rebate_rate)`` — the same per-trade
    forecast that is summed into the parent :class:`PendingRebate`'s ``expected`` (so
    ``Σ trade.expected == month.expected`` and ``Σ trade.fee == month.fee_total`` by
    construction). FORECAST-ONLY; never money of record (FE-D1).
    """

    trade_date: date
    symbol: str
    name: str  # instrument display name; falls back to the symbol when unknown
    side: Side
    fee: Decimal
    expected: Decimal


class PendingRebate(BaseModel):
    """One month's forecast rebate awaiting the owner's receipt confirmation.

    ``expected`` is Σ per-trade ``floor(fee × rebate_rate)`` — a FORECAST, never money of
    record. The confirm amount is editable (this is only the prefill). ``trades`` is the
    per-trade breakdown (§3.6), ordered by ``trade_date``, that sums to the month totals.
    """

    account_id: str
    account_name: str
    month: str  # "YYYY-MM"
    trade_count: int
    fee_total: Decimal
    expected: Decimal
    ccy: str
    trades: list[RebateTrade] = []


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
    """account_id -> (account, rebate_rate) for every account ANY of whose bound rule sets
    rebates (>0).

    Batch B: an account may bind several rule sets (one per market); the first bound set
    (alphabetical, per ``rule_sets_for``) with ``rebate_rate > 0`` supplies the rate.
    Behaviour-identical today — only the TW rule set rebates, and every account binds a
    single market, so exactly one rule set is ever consulted.
    """
    out: dict[str, tuple[Account, Decimal]] = {}
    for a in list_accounts(conn):
        for rule_name in rule_sets_for(conn, a.account_id):
            rate = get_fee_rule_set(rule_name, conn).rebate_rate
            if rate > _ZERO:
                out[a.account_id] = (a, rate)
                break
    return out


def _skips(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    ensure_tables(conn)
    return {
        (r["account_id"], r["month"])
        for r in conn.execute("SELECT account_id, month FROM rebate_skips")
    }


def _confirmed_months(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """(account_id, ``YYYY-MM``) trade months already booked by a confirmed rebate credit.

    A confirmed rebate is a ``REBATE``-kind cash movement dated the 1st of the refund month
    (the month AFTER the trade month). Each booking is mapped back to its trade month by TWO
    independent keys, so a booked month can NEVER re-surface and be double-credited (F2d/F12)
    after an edit:

    * STRUCTURAL (robust to a note edit): the trade month is the month BEFORE the movement's
      date. Survives the owner editing the movement's free-text note tag.
    * NOTE TAG (documented contract): the ``{YYYY-MM} 折讓款`` fingerprint month.

    Either match suppresses the month; both agree for an unedited booking. (Breaking the
    structural key too would require also changing the movement's date/kind — the cash-page
    edit modal locks those on a rebate row, and a backend guard on the movement PUT is the
    belt-and-braces stop; see the router.)
    """
    out: set[tuple[str, str]] = set()
    for m in list_cash_movements(conn):
        if m.kind.upper() != REBATE_KIND:
            continue
        py, pm = _prev_month(m.date.year, m.date.month)
        out.add((m.account_id, f"{py:04d}-{pm:02d}"))
        if m.note:
            tagged = _month_from_tag(m.note)
            if tagged is not None:
                out.add((m.account_id, tagged))
    return out


def _aggregate(conn: sqlite3.Connection) -> list[PendingRebate]:
    """Every rebate account's fee-bearing trades grouped by calendar month — UNFILTERED.

    Pure aggregation shared by :func:`detect` (pending, confirmable) and
    :func:`detect_accruing` (current / not-yet-due, informational). The pending gate, skip,
    and confirmed-suppression filters are the callers' concern, not applied here.
    """
    accts = _rebate_accounts(conn)
    if not accts:
        return []
    # Instrument display names, looked up ONCE; unknown symbol -> the symbol itself.
    names = {i.symbol: i.name for i in list_instruments(conn)}

    # (account_id, month) -> [fee_total, expected]; parallel per-trade breakdown.
    agg: dict[tuple[str, str], list[Decimal]] = {}
    counts: dict[tuple[str, str], int] = {}
    trades: dict[tuple[str, str], list[RebateTrade]] = {}
    # list_transactions is ordered by trade_date ASC, so per-key trade lists inherit that order.
    for t in list_transactions(conn):
        if t.account_id not in accts:
            continue
        if t.fees is None or t.fees <= _ZERO:  # skip fee-free rows (nothing to rebate)
            continue
        rate = accts[t.account_id][1]
        trade_expected = forecast_tw_rebate(t.fees, rate)
        key = (t.account_id, _month_key(t.trade_date))
        cell = agg.setdefault(key, [_ZERO, _ZERO])
        cell[0] += t.fees
        cell[1] += trade_expected
        counts[key] = counts.get(key, 0) + 1
        trades.setdefault(key, []).append(RebateTrade(
            trade_date=t.trade_date, symbol=t.symbol,
            name=names.get(t.symbol, t.symbol), side=t.side,
            fee=t.fees, expected=trade_expected,
        ))

    out: list[PendingRebate] = []
    for (account_id, month), (fee_total, expected) in agg.items():
        account = accts[account_id][0]
        out.append(PendingRebate(
            account_id=account_id, account_name=account.name, month=month,
            trade_count=counts[(account_id, month)], fee_total=fee_total,
            expected=expected, ccy=account.settlement_ccy.value,
            trades=trades[(account_id, month)],
        ))
    return out


def detect(
    conn: sqlite3.Connection, *, now: datetime, include_skipped: bool = False
) -> list[PendingRebate]:
    """The pending (confirmable) rebate list — pure read, self-healing (no rows stored).

    Keeps only aggregated months that are (a) PENDING (past the following month's 1st),
    (b) not suppressed by a confirmed rebate movement (dual-keyed — see
    :func:`_confirmed_months`), and (c) not skipped (unless ``include_skipped``, used by
    :func:`list_skipped` to reconstruct detail). Current / not-yet-due months are NOT here
    — they are the accruing forecast (see :func:`detect_accruing`).
    """
    skips: set[tuple[str, str]] = set() if include_skipped else _skips(conn)
    confirmed = _confirmed_months(conn)
    out = [
        p for p in _aggregate(conn)
        if _is_pending(p.month, now)
        and (p.account_id, p.month) not in skips
        and (p.account_id, p.month) not in confirmed
    ]
    out.sort(key=lambda p: (p.month, p.account_id), reverse=True)
    return out


def detect_accruing(conn: sqlite3.Connection, *, now: datetime) -> list[PendingRebate]:
    """Current / not-yet-due months surfaced as a NON-confirmable forecast (owner #1).

    Same per-month forecast shape as :func:`detect`, but for months whose refund is not yet
    due (``_is_pending`` is False — the trade month is the current month, or later). These
    are informational: the confirm endpoint rejects a not-yet-pending month, and the frontend
    renders them WITHOUT a 確認 button. Confirmed-suppression is applied for symmetry (a
    future-dated booking, if any, hides its month); skips are NOT — an accruing forecast is
    not actionable, so not skippable. Fixes the by-design gap where a trade entered THIS month
    was invisible in the inbox until the following month.
    """
    confirmed = _confirmed_months(conn)
    out = [
        p for p in _aggregate(conn)
        if not _is_pending(p.month, now)
        and (p.account_id, p.month) not in confirmed
    ]
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
