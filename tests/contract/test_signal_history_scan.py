"""The scan maintains signal_history (W6 — AI-D27/AI-D28): replay, idempotency, floors.

Pins: first scan backfills exactly the computable dates (and the detail says so); a
same-day re-scan is a FULL-row no-op (updated_at included — compare-then-skip); the row
key is the PRICE date, not the scan's wall clock (a Sunday scan keys Friday's row); the
missing-set rule refills a deleted middle row AND a left-edge hole a later price backfill
opens; the progress callback reports the backfill; a thin series writes NO history rows
(the full-coverage floor) while still seeding signal_states.
"""

import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from portfolio_dash.api.signals_service import required_sessions, scan_signals
from portfolio_dash.data_ingestion.store import upsert_instrument
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.strategy import signal_history as sh
from portfolio_dash.strategy import signal_states as ss
from portfolio_dash.strategy.rules.params import default_params
from tests.conftest import GOLDEN_NOW

_TZ = ZoneInfo("Asia/Taipei")
_END = GOLDEN_NOW.date()  # 2026-06-11

# The golden DB's two held symbols (2330, AAPL) carry one price each — permanently below
# the history floor, so every assertion here keys on a freshly registered watch symbol.
_FLOOR = required_sessions(default_params())  # 260 sessions


def _register(conn: sqlite3.Connection, symbol: str) -> None:
    upsert_instrument(conn, Instrument(
        symbol=symbol, market=Market.US, quote_ccy=Currency.USD,
        sector="Tech", name=symbol,
    ))


def _seed_days(
    conn: sqlite3.Connection, symbol: str, n: int, *, end: date, start_price: str = "100",
) -> list[date]:
    """Seed ``n`` consecutive daily closes ascending 1/day ending at ``end``; returns the
    seeded dates. Idempotent upsert, so a later seed can extend the SAME series left."""
    rows = [
        PriceRow(
            instrument=symbol, market=Market.US, as_of=end - timedelta(days=n - 1 - i),
            close=Decimal(start_price) + Decimal(i), source="test",
        )
        for i in range(n)
    ]
    upsert_prices(conn, rows, fetched_at=GOLDEN_NOW)
    return [end - timedelta(days=n - 1 - i) for i in range(n)]


def test_first_scan_backfills_exactly_the_computable_dates(
    golden_db: sqlite3.Connection,
) -> None:
    _register(golden_db, "WATCH")
    seeded = _seed_days(golden_db, "WATCH", 320, end=_END)
    detail = scan_signals(golden_db, now=GOLDEN_NOW)
    rows = sh.list_rows(golden_db, "WATCH")
    assert len(rows) == 320 - (_FLOOR - 1) == 61
    assert rows[0].as_of == seeded[_FLOOR - 1] == _END - timedelta(days=60)
    assert rows[-1].as_of == _END
    assert all(r.params_version == "rules-v1" for r in rows)
    assert "61 history row(s) replayed" in detail
    # The floor held for the golden one-price symbols: no history, but seeded states.
    assert sh.list_rows(golden_db, "2330") == []
    assert ss.get_state(golden_db, "2330") is not None


def test_rescan_is_a_full_row_noop(golden_db: sqlite3.Connection) -> None:
    _register(golden_db, "WATCH")
    _seed_days(golden_db, "WATCH", 320, end=_END)
    scan_signals(golden_db, now=GOLDEN_NOW)
    before = sh.list_rows(golden_db, "WATCH")
    detail = scan_signals(golden_db, now=GOLDEN_NOW)
    after = sh.list_rows(golden_db, "WATCH")
    assert "0 history row(s) replayed, 0 head refresh(es)" in detail
    assert after == before  # dataclass equality covers updated_at — compare-then-skip


def test_weekend_scan_keys_the_row_by_price_date(golden_db: sqlite3.Connection) -> None:
    friday = date(2026, 6, 12)
    sunday = datetime(2026, 6, 14, 10, 0, tzinfo=_TZ)
    _register(golden_db, "WATCH")
    _seed_days(golden_db, "WATCH", 320, end=friday)
    scan_signals(golden_db, now=sunday)
    rows = sh.list_rows(golden_db, "WATCH")
    assert rows[-1].as_of == friday  # the data the evaluation describes
    # ⑬ (2026-08-26): this line used to pin "2026-06-14" — the SCAN date — with the comment
    # 「signal_states keeps its own scan-date semantics」. That divergence was known and
    # accepted at W6 time, and the investment-logic review is what re-opened it: two sibling
    # tables describing the same evaluation disagreed about which day it describes, and the
    # wire + prompt 守則 published the wall-clock one. Both tables now key on the DATA date.
    state = ss.get_state(golden_db, "WATCH")
    assert state is not None and state.as_of == "2026-06-12"
    assert state.updated_at.startswith("2026-06-14")  # when scanned — a separate field


def test_a_deleted_middle_row_is_refilled(golden_db: sqlite3.Connection) -> None:
    _register(golden_db, "WATCH")
    _seed_days(golden_db, "WATCH", 320, end=_END)
    scan_signals(golden_db, now=GOLDEN_NOW)
    rows = sh.list_rows(golden_db, "WATCH")
    victim = rows[10]
    golden_db.execute(
        "DELETE FROM signal_history WHERE symbol=? AND as_of=?",
        ("WATCH", victim.as_of.isoformat()),
    )
    detail = scan_signals(golden_db, now=GOLDEN_NOW)
    refilled = sh.list_rows(golden_db, "WATCH")
    assert "1 history row(s) replayed" in detail
    assert [r.as_of for r in refilled] == [r.as_of for r in rows]
    restored = refilled[10]
    assert restored.params_version == "rules-v1"
    # The refilled row equals the original CONTENT (the stamp moves — it was rewritten).
    assert restored.trend_score == victim.trend_score
    assert restored.momentum_score == victim.momentum_score


def test_a_later_left_edge_price_backfill_extends_the_history(
    golden_db: sqlite3.Connection,
) -> None:
    _register(golden_db, "WATCH")
    seeded = _seed_days(golden_db, "WATCH", 280, end=_END)
    scan_signals(golden_db, now=GOLDEN_NOW)
    first = sh.list_rows(golden_db, "WATCH")
    assert len(first) == 280 - (_FLOOR - 1) == 21
    oldest_stamp = first[0].updated_at

    # A deeper price backfill lands LATER (the yfinance 5y pattern): 60 older dates.
    older_start = seeded[0] - timedelta(days=60)
    _seed_days(golden_db, "WATCH", 60, end=seeded[0] - timedelta(days=1),
               start_price=str(Decimal("100") - 60))
    detail = scan_signals(golden_db, now=GOLDEN_NOW)
    rows = sh.list_rows(golden_db, "WATCH")
    assert "60 history row(s) replayed" in detail
    assert len(rows) == 81
    assert rows[0].as_of == older_start + timedelta(days=_FLOOR - 1)
    # Rows already stored are untouched (a max-as_of watermark would have written ZERO).
    assert rows[60].updated_at == oldest_stamp


def test_progress_callback_reports_the_backfill(golden_db: sqlite3.Connection) -> None:
    _register(golden_db, "WATCH")
    _seed_days(golden_db, "WATCH", 320, end=_END)
    messages: list[str] = []
    scan_signals(golden_db, now=GOLDEN_NOW, progress=messages.append)
    assert any("回填訊號歷史 WATCH" in m for m in messages)
    # A no-op rescan reports nothing (no per-symbol noise on the jobs page).
    messages.clear()
    scan_signals(golden_db, now=GOLDEN_NOW)
    assert messages == []


def test_thin_series_writes_no_history_but_still_seeds_state(
    golden_db: sqlite3.Connection,
) -> None:
    _register(golden_db, "WATCH")
    _seed_days(golden_db, "WATCH", 100, end=_END)
    detail = scan_signals(golden_db, now=GOLDEN_NOW)
    assert sh.list_rows(golden_db, "WATCH") == []  # below the full-coverage floor
    assert ss.get_state(golden_db, "WATCH") is not None  # current-state cache unaffected
    assert "0 history row(s) replayed" in detail
