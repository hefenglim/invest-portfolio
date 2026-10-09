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
(a) a confirmed rebate cash movement for that account maps back to it — by the explicit
``rebate_period`` link the confirm writes (DEF-009: survives ANY edit of the row), or, for an
unlinked REBATE row, DUAL-KEYED by BOTH the movement's date (the trade month is the month
before the refund date) AND the 「YYYY-MM 折讓款」 note tag — or (b) the month is skipped.
Either way it is the double-credit guard (F2d/F12): a booked month cannot re-surface after
the row is edited. Self-healing — nothing is
auto-written, and confirm recomputes/validates server-side. Mirrors the dividend-inbox
posture (compute-on-read, ungated in guest mode).
"""

import sqlite3
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel

from portfolio_dash.data_ingestion.config_seed import get_fee_rule_set
from portfolio_dash.data_ingestion.fees import (
    booked_discount,
    forecast_tw_rebate,
    rebate_applies,
)
from portfolio_dash.data_ingestion.rules_binding import rule_sets_for
from portfolio_dash.data_ingestion.store import (
    list_accounts,
    list_cash_movements,
    list_instruments,
    list_transactions,
)
from portfolio_dash.shared.account_ref import account_ref
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


# How far back the pending list reaches by default (audit L4, 2026-07-26). The forecaster is
# stateless and walks the WHOLE transaction ledger, so importing several years of TW history
# used to drop dozens of un-actioned months into the inbox at once, each needing an individual
# 略過 to clear. Twelve months is one rebate cycle's worth of memory; anything older is still
# detectable and confirmable, just behind an explicit 「顯示更早」 (and its count is always
# reported, so nothing is silently dropped).
_WINDOW_MONTHS = 12
WINDOW_MONTHS = _WINDOW_MONTHS  # public alias for the API payload / UI copy


def _months_before(month: str, now: datetime) -> int:
    """How many whole calendar months *month* sits before *now*'s month (0 = this month)."""
    my, mm = int(month[:4]), int(month[5:7])
    return (now.year - my) * 12 + (now.month - mm)


def _in_window(month: str, now: datetime) -> bool:
    return _months_before(month, now) <= _WINDOW_MONTHS


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
    #: DEF-008: an ACCOUNT TOKEN (``{account:<id>}``, ``shared/account_ref.py``), not the
    #: English ``accounts.name`` — the fetch layer resolves it to the ``pdNames`` spelling.
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


def _rebate_accounts(
    conn: sqlite3.Connection,
) -> dict[str, tuple[Account, Decimal, Decimal]]:
    """account_id -> (account, rebate_rate, discount) for every account ANY of whose bound
    rule sets rebates (>0).

    Batch B: an account may bind several rule sets (one per market); the first bound set
    (alphabetical, per ``rule_sets_for``) with ``rebate_rate > 0`` supplies the rate.
    Behaviour-identical today — only the TW rule set rebates, and every account binds a
    single market, so exactly one rule set is ever consulted.

    ``discount`` (DEF-010) is that SAME rule set's current settlement discount — the fallback
    for a trade whose own fee snapshot does not record one (:func:`booked_discount`).
    """
    out: dict[str, tuple[Account, Decimal, Decimal]] = {}
    for a in list_accounts(conn):
        for rule_name in rule_sets_for(conn, a.account_id):
            rs = get_fee_rule_set(rule_name, conn)
            if rs.rebate_rate > _ZERO:
                out[a.account_id] = (a, rs.rebate_rate, rs.discount)
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

    **The explicit link first (DEF-009, 2026-09-24).** A credit booked by the confirm
    endpoint carries ``rebate_period`` — the month it books — and that link alone decides,
    whatever the row's kind, date or note now say. The owner ruled the row fully editable
    (it was locked precisely because those three fields WERE the key), so the key had to
    stop being the fields. Changing its kind to something else does NOT re-open the month
    either (the conservative reading — the money was received once); deleting the row does.

    **The legacy keys for an unlinked REBATE row** — one entered by hand on the cash page or
    imported by CSV, neither of which names a month: a ``REBATE``-kind movement dated the 1st
    of the refund month maps back by TWO independent keys, so a booked month never
    re-surfaces after a note edit (F2d/F12):

    * STRUCTURAL: the trade month is the month BEFORE the movement's date.
    * NOTE TAG (documented contract): the ``{YYYY-MM} 折讓款`` fingerprint month.
    """
    out: set[tuple[str, str]] = set()
    for m in list_cash_movements(conn):
        if m.rebate_period is not None:
            out.add((m.account_id, m.rebate_period))
            continue
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
        _account, rate, current_discount = accts[t.account_id]
        # DEF-010: a fee already discounted at settlement earns no refund. The trade's OWN
        # snapshot says which regime charged it; the rule set answers only when it is silent.
        discount = booked_discount(t.fee_rule_snapshot, fallback=current_discount)
        if not rebate_applies(discount):
            continue  # not forecast, not counted: 快照已打折的交易不列入退款預估 (B-16)
        trade_expected = forecast_tw_rebate(t.fees, rate, discount=discount)
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
            account_id=account_id, account_name=account_ref(account_id), month=month,
            trade_count=counts[(account_id, month)], fee_total=fee_total,
            expected=expected, ccy=account.settlement_ccy.value,
            trades=trades[(account_id, month)],
        ))
    return out


def detect(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    include_skipped: bool = False,
    include_older: bool = False,
) -> list[PendingRebate]:
    """The pending (confirmable) rebate list — pure read, self-healing (no rows stored).

    Keeps only aggregated months that are (a) PENDING (past the following month's 1st),
    (b) not suppressed by a confirmed rebate movement (dual-keyed — see
    :func:`_confirmed_months`), (c) not skipped (unless ``include_skipped``, used by
    :func:`list_skipped` to reconstruct detail), and (d) within the last
    ``_WINDOW_MONTHS`` months unless ``include_older`` (audit L4). Current / not-yet-due
    months are NOT here — they are the accruing forecast (see :func:`detect_accruing`).

    ``include_older=True`` is what the 「顯示更早」 view and the CONFIRM endpoint pass: a
    month the user chose to reveal must remain confirmable, or the window would turn into a
    silent cap on what can be booked.
    """
    skips: set[tuple[str, str]] = set() if include_skipped else _skips(conn)
    confirmed = _confirmed_months(conn)
    out = [
        p for p in _aggregate(conn)
        if _is_pending(p.month, now)
        and (include_older or _in_window(p.month, now))
        and (p.account_id, p.month) not in skips
        and (p.account_id, p.month) not in confirmed
    ]
    out.sort(key=lambda p: (p.month, p.account_id), reverse=True)
    return out


def older_pending_count(conn: sqlite3.Connection, *, now: datetime) -> int:
    """How many pending months fall OUTSIDE the default window — never silently dropped.

    The inbox reports this so a bounded list can never read as "nothing left to confirm".
    """
    inside = {(p.account_id, p.month) for p in detect(conn, now=now)}
    return len([
        p for p in detect(conn, now=now, include_older=True)
        if (p.account_id, p.month) not in inside
    ])


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
    out: list[SkippedRebate] = []
    for r in rows:
        key = (r["account_id"], r["month"])
        detail = by_key.get(key)
        out.append(SkippedRebate(
            account_id=r["account_id"], account_name=account_ref(r["account_id"]),
            month=r["month"],
            skipped_at=r["skipped_at"], detail=detail,
        ))
    return out
