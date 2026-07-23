"""Unit: 折讓款 forecaster service (api/rebates.py), Wave B / FE-D1.

Drives the compute-on-read service directly against the golden DB connection: month
grouping + per-trade floor delegation (forecast_tw_rebate), the following-month pending
gate (injected clock), and suppression by a matching rebate movement OR an explicit skip.
The forecast is NEVER money of record — these tests assert the FORECAST shape only.
"""

import sqlite3
from datetime import date, datetime
from decimal import Decimal

from portfolio_dash.api import rebates as svc
from portfolio_dash.data_ingestion.fees import forecast_tw_rebate
from portfolio_dash.data_ingestion.store import insert_cash_movement, insert_transaction
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.enums import Side

_TW = "tw_broker"


def _buy(conn: sqlite3.Connection, *, d: date, fee: str, account: str = _TW) -> None:
    insert_transaction(
        conn, account_id=account, symbol="2330", side=Side.BUY,
        quantity=Decimal("1000"), price=Decimal("500"), fees=Decimal(fee),
        tax=Decimal("0"), trade_date=d)


def _at(y: int, m: int, day: int = 11) -> datetime:
    return datetime(y, m, day)


def test_groups_by_month_and_floors_per_trade(golden_db: sqlite3.Connection) -> None:
    _buy(golden_db, d=date(2026, 5, 5), fee="142")
    _buy(golden_db, d=date(2026, 5, 20), fee="156")
    rows = svc.detect(golden_db, now=_at(2026, 6, 11))
    hit = next(p for p in rows if p.account_id == _TW and p.month == "2026-05")
    assert hit.trade_count == 2
    assert hit.fee_total == Decimal("298")            # 142 + 156
    # Σ per-trade floor(fee × 0.77): floor(109.34)=109 + floor(120.12)=120 = 229
    assert hit.expected == Decimal("229")
    assert hit.expected == (
        forecast_tw_rebate(Decimal("142"), Decimal("0.77"))
        + forecast_tw_rebate(Decimal("156"), Decimal("0.77"))
    )
    assert hit.ccy == "TWD"


def test_trade_breakdown_sums_to_month_totals(golden_db: sqlite3.Connection) -> None:
    """FU-D6: per-trade breakdown is ordered by trade_date and sums to the month totals."""
    _buy(golden_db, d=date(2026, 5, 5), fee="142")
    _buy(golden_db, d=date(2026, 5, 20), fee="156")
    hit = next(p for p in svc.detect(golden_db, now=_at(2026, 6, 11))
               if p.account_id == _TW and p.month == "2026-05")
    assert [t.trade_date for t in hit.trades] == [date(2026, 5, 5), date(2026, 5, 20)]
    assert [t.expected for t in hit.trades] == [Decimal("109"), Decimal("120")]
    # INVARIANT: Σ per-trade fee/expected == the month aggregate
    assert sum((t.fee for t in hit.trades), Decimal("0")) == hit.fee_total
    assert sum((t.expected for t in hit.trades), Decimal("0")) == hit.expected
    # instrument display name resolved once (golden 2330 -> TSMC); side preserved
    assert hit.trades[0].name == "TSMC" and hit.trades[0].side == Side.BUY


def test_trade_name_falls_back_to_symbol(golden_db: sqlite3.Connection) -> None:
    """A symbol with no instrument row keeps the symbol as its breakdown name."""
    insert_transaction(
        golden_db, account_id=_TW, symbol="9999", side=Side.BUY, quantity=Decimal("100"),
        price=Decimal("10"), fees=Decimal("100"), tax=Decimal("0"),
        trade_date=date(2026, 5, 5))
    hit = next(p for p in svc.detect(golden_db, now=_at(2026, 6, 11)) if p.account_id == _TW)
    t = next(x for x in hit.trades if x.symbol == "9999")
    assert t.name == "9999"


def test_distinct_months_are_distinct_items(golden_db: sqlite3.Connection) -> None:
    _buy(golden_db, d=date(2026, 4, 10), fee="100")
    _buy(golden_db, d=date(2026, 5, 10), fee="200")
    months = {p.month for p in svc.detect(golden_db, now=_at(2026, 6, 11))
              if p.account_id == _TW}
    assert {"2026-04", "2026-05"} <= months


def test_fee_free_trades_skipped(golden_db: sqlite3.Connection) -> None:
    # The golden 2330 buy (2026-01, fee 0) contributes nothing; a fee-0 May row too.
    _buy(golden_db, d=date(2026, 5, 5), fee="0")
    assert not svc.detect(golden_db, now=_at(2026, 6, 11))


def test_following_month_pending_gate(golden_db: sqlite3.Connection) -> None:
    _buy(golden_db, d=date(2026, 5, 5), fee="142")
    # Same calendar month as the trade -> NOT yet pending (refund lands next month's 1st).
    assert not [p for p in svc.detect(golden_db, now=_at(2026, 5, 31)) if p.month == "2026-05"]
    # First day of the FOLLOWING month -> pending.
    assert [p for p in svc.detect(golden_db, now=_at(2026, 6, 1)) if p.month == "2026-05"]


def test_non_rebate_account_never_appears(golden_db: sqlite3.Connection) -> None:
    # schwab (US rule, rebate_rate 0) with a real fee produces NO rebate item.
    insert_transaction(
        golden_db, account_id="schwab", symbol="AAPL", side=Side.SELL,
        quantity=Decimal("1"), price=Decimal("100"), fees=Decimal("5"),
        tax=Decimal("0"), trade_date=date(2026, 5, 5))
    assert all(p.account_id != "schwab" for p in svc.detect(golden_db, now=_at(2026, 6, 11)))


def test_suppressed_by_matching_rebate_movement(golden_db: sqlite3.Connection) -> None:
    _buy(golden_db, d=date(2026, 5, 5), fee="142")
    assert [p for p in svc.detect(golden_db, now=_at(2026, 6, 11)) if p.month == "2026-05"]
    # A booked rebate carrying the month's note tag suppresses that month.
    insert_cash_movement(
        golden_db, account_id=_TW, move_date=date(2026, 6, 1), kind=svc.REBATE_KIND,
        ccy=Currency.TWD, amount=Decimal("109"), note=svc.month_tag("2026-05"))
    assert not [p for p in svc.detect(golden_db, now=_at(2026, 6, 11)) if p.month == "2026-05"]


def test_suppressed_by_movement_date_even_if_note_edited(
    golden_db: sqlite3.Connection,
) -> None:
    """F2d/F12: the STRUCTURAL date key suppresses a booked month independent of the
    (user-editable) note tag — a mangled note can no longer re-surface the month for a
    second credit. The rebate movement is dated in the refund month (2026-06) → the covered
    trade month is 2026-05."""
    _buy(golden_db, d=date(2026, 5, 5), fee="142")
    now = _at(2026, 6, 11)
    assert [p for p in svc.detect(golden_db, now=now) if p.month == "2026-05"]
    insert_cash_movement(
        golden_db, account_id=_TW, move_date=date(2026, 6, 1), kind=svc.REBATE_KIND,
        ccy=Currency.TWD, amount=Decimal("109"), note="totally unrelated note")
    assert not [p for p in svc.detect(golden_db, now=now) if p.month == "2026-05"]


def test_current_month_accrues_not_pending(golden_db: sqlite3.Connection) -> None:
    """owner #1: a current-month trade is ACCRUING (not-yet-due), a prior month is PENDING.

    detect() lists only pending months; detect_accruing() lists only current/future months.
    The two are disjoint and carry the same forecast shape."""
    now = _at(2026, 6, 11)
    _buy(golden_db, d=date(2026, 6, 3), fee="142")   # current month (2026-06) -> accruing
    _buy(golden_db, d=date(2026, 5, 5), fee="142")   # prior month (2026-05) -> pending
    pending_months = {p.month for p in svc.detect(golden_db, now=now) if p.account_id == _TW}
    assert "2026-05" in pending_months and "2026-06" not in pending_months
    acc = svc.detect_accruing(golden_db, now=now)
    acc_months = {p.month for p in acc if p.account_id == _TW}
    assert "2026-06" in acc_months and "2026-05" not in acc_months
    hit = next(p for p in acc if p.account_id == _TW and p.month == "2026-06")
    assert hit.expected == Decimal("109") and hit.trade_count == 1 and len(hit.trades) == 1
    # accruing is never counted in the pending badge
    assert svc.pending_count(golden_db, now=now) == 1


def test_skip_then_unskip_resurfaces(golden_db: sqlite3.Connection) -> None:
    _buy(golden_db, d=date(2026, 5, 5), fee="142")
    now = _at(2026, 6, 11)
    svc.mark_skipped(golden_db, _TW, "2026-05", now=now)
    assert not [p for p in svc.detect(golden_db, now=now) if p.month == "2026-05"]
    # The skipped month reconstructs its detail in the 已略過 list.
    skipped = svc.list_skipped(golden_db, now=now)
    row = next(s for s in skipped if s.account_id == _TW and s.month == "2026-05")
    assert row.detail is not None and row.detail.expected == Decimal("109")
    assert len(row.detail.trades) == 1 and row.detail.trades[0].fee == Decimal("142")
    assert svc.unskip(golden_db, [(_TW, "2026-05")]) == 1
    assert [p for p in svc.detect(golden_db, now=now) if p.month == "2026-05"]


def test_unskip_unknown_is_noop(golden_db: sqlite3.Connection) -> None:
    assert svc.unskip(golden_db, [(_TW, "2099-01")]) == 0
