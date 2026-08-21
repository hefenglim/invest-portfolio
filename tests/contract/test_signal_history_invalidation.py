"""The corporate-action seam invalidates BOTH derived signal tables (W6 — AI-D27).

A SPLIT restatement changes what every stored evaluation MEANS: ``signal_history`` rows
were computed under the old basis (wrong, not merely stale — the next scan's replay
rebuilds them), and the ``signal_states`` comparison row would diff against the
post-restatement evaluation and fire a PHANTOM transition event (a restatement is not a
market event). The seam is the single wrapper ``reconcile_split_prices`` — every
corporate-action mutation door (form create / edit / set-delete / row-delete / CSV commit)
calls it, so pinning the wrapper pins all five doors.

The disproof pair is the point: the control arm (SPLIT inserted WITHOUT the reconcile)
demonstrates the phantom exists; the fixed arm (insert + reconcile) proves the seam kills
it. The fixture's teeth are asserted too — the pre-invalidation history really does carry
cliff-poisoned rows, so the rebuild assertions are not vacuous.

The price-series fixture is AS-TRADED with the split cliff present (700 → 240 across a
7:1), seeded with ``fetched_at`` BEFORE the split date — so the write seam stores basis '1'
(the provider could not have folded a split it had not seen) and the reconcile is a
deliberate no-op on the closes themselves: the ONLY mutations in the fixed arm are the two
deletes, which is exactly what this file pins.
"""

import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from portfolio_dash.api.routers.ledgers import reconcile_split_prices
from portfolio_dash.api.signals_service import scan_signals
from portfolio_dash.data_ingestion.store import (
    delete_instrument,
    insert_corporate_action,
    upsert_instrument,
)
from portfolio_dash.portfolio.backtest import (
    HistoryPoint,
    SignalEvent,
    detect_events,
)
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.strategy import signal_history as sh
from portfolio_dash.strategy import signal_states as ss
from portfolio_dash.strategy.rules.composite import BAND_HIGH, BAND_LOW
from portfolio_dash.strategy.rules.params import PARAMS_VERSION
from tests.conftest import GOLDEN_NOW

_TZ = ZoneInfo("Asia/Taipei")
_END = GOLDEN_NOW.date()          # 2026-06-11 (a Thursday)
_N = 320                          # consecutive daily closes
_CLIFF = 280                      # the split sits at index 280 (inside the history range)
_SPLIT_DAY = _END - timedelta(days=_N - 1 - _CLIFF)   # day-280's date
_FETCHED = datetime(2026, 5, 2, 12, 0, tzinfo=_TZ)    # BEFORE the split date (as-traded)


def _register(conn: sqlite3.Connection, symbol: str) -> None:
    upsert_instrument(conn, Instrument(
        symbol=symbol, market=Market.US, quote_ccy=Currency.USD,
        sector="Tech", name=symbol,
    ))


def _seed_split_series(conn: sqlite3.Connection, symbol: str) -> None:
    """The TRULY monotonic company (+0.5/day in post-split terms) stored as traded:
    700 + 3.5i up to the 7:1 split, then 240 + 0.5(i−280) after it — the stored series
    shows a −85.7% cliff at the split date until the action ledger + the read path
    re-express it away."""
    rows: list[PriceRow] = []
    for i in range(_N):
        day = _END - timedelta(days=_N - 1 - i)
        close = Decimal(700) + Decimal("3.5") * i if i < _CLIFF else (
            Decimal(240) + Decimal("0.5") * (i - _CLIFF)
        )
        rows.append(PriceRow(instrument=symbol, market=Market.US, as_of=day,
                             close=close, source="test"))
    upsert_prices(conn, rows, fetched_at=_FETCHED)


def _insert_split(conn: sqlite3.Connection, symbol: str) -> None:
    insert_corporate_action(
        conn, account_id="tw_broker", action_date=_SPLIT_DAY,
        kind=CorporateActionKind.SPLIT, from_symbol=symbol, to_symbol=symbol,
        ratio_to=Decimal("7"), ratio_from=Decimal("1"),
    )


def _history_events(
    conn: sqlite3.Connection, symbol: str
) -> list[SignalEvent]:
    rows = sh.list_rows(conn, symbol, params_version=PARAMS_VERSION)
    points = [
        HistoryPoint(
            as_of=r.as_of,
            scores={
                "trend_filter": r.trend_score,
                "ma_cross": r.cross_score,
                "momentum_12_1": r.momentum_score,
                "rsi_regime": r.rsi_score,
            },
            tech_score=r.tech_score,
        )
        for r in rows
    ]
    return detect_events(points, band_high=BAND_HIGH, band_low=BAND_LOW)


def _signal_events(conn: sqlite3.Connection, symbol: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT rule_id FROM alert_events WHERE symbol = ? AND rule_id LIKE 'signal_%'",
        (symbol,),
    ).fetchall()


def test_history_rows_are_poisoned_until_the_action_is_recorded(
    golden_db: sqlite3.Connection,
) -> None:
    """The fixture's teeth: pre-invalidation, the cliff reads as a momentum CRASH."""
    _register(golden_db, "WATCH")
    _seed_split_series(golden_db, "WATCH")
    scan_signals(golden_db, now=GOLDEN_NOW)
    rows = sh.list_rows(golden_db, "WATCH")
    assert len(rows) == 61
    # Post-cliff rows compare ~250 against a ~1500 base → negative 12-1 momentum.
    assert any(r.momentum_score is not None and r.momentum_score < 0 for r in rows)


def test_invalidation_rebuilds_history_without_phantom_events(
    golden_db: sqlite3.Connection,
) -> None:
    _register(golden_db, "WATCH")
    _seed_split_series(golden_db, "WATCH")
    scan_signals(golden_db, now=GOLDEN_NOW)

    _insert_split(golden_db, "WATCH")
    restated = reconcile_split_prices(golden_db, ["WATCH"])
    assert restated == 0  # as-traded fixture: the closes themselves were already right
    assert sh.list_rows(golden_db, "WATCH") == []
    assert ss.get_state(golden_db, "WATCH") is None

    scan_signals(golden_db, now=GOLDEN_NOW)
    rows = sh.list_rows(golden_db, "WATCH")
    assert len(rows) == 61
    # The rebuilt history is the truly-monotonic company: momentum never negative…
    assert all(
        r.momentum_score is None or r.momentum_score > 0 for r in rows
    )
    # …so the study finds NO bearish events of any kind…
    events = _history_events(golden_db, "WATCH")
    assert [e for e in events if e.direction == "bearish"] == []
    # …and the rescan fired NO phantom transition into the alert feed.
    assert _signal_events(golden_db, "WATCH") == []


def test_control_arm_without_the_reconcile_fires_the_phantom(
    golden_db: sqlite3.Connection,
) -> None:
    """DISPROOF: record the split but SKIP the reconcile seam → the stale comparison row
    diffs against the re-expressed evaluation and a phantom signal_momentum fires."""
    _register(golden_db, "WATCH")
    _seed_split_series(golden_db, "WATCH")
    scan_signals(golden_db, now=GOLDEN_NOW)

    _insert_split(golden_db, "WATCH")
    # NO reconcile_split_prices call — the pre-W6 world.
    scan_signals(golden_db, now=GOLDEN_NOW)
    fired = _signal_events(golden_db, "WATCH")
    assert any(r["rule_id"] == "signal_momentum" for r in fired)


def test_api_create_split_invalidates_both_tables(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """The seam is really wired: one form POST (the 2330 held golden symbol) clears both
    derived tables via the wrapper — the other four doors share it by construction."""
    _seed_split_series(golden_db, "2330")
    scan_signals(golden_db, now=GOLDEN_NOW)
    assert len(sh.list_rows(golden_db, "2330")) == 61
    assert ss.get_state(golden_db, "2330") is not None

    r = api_client.post("/api/ledgers/corporate-actions", json={
        "account_id": "tw_broker", "date": _SPLIT_DAY.isoformat(), "kind": "SPLIT",
        "from_symbol": "2330", "to_symbol": "2330", "ratio_to": "7", "ratio_from": "1",
    })
    assert r.status_code == 201, r.text
    assert sh.list_rows(golden_db, "2330") == []
    assert ss.get_state(golden_db, "2330") is None


def test_purge_removes_the_history(golden_db: sqlite3.Connection) -> None:
    _register(golden_db, "WATCH")
    _seed_split_series(golden_db, "WATCH")
    scan_signals(golden_db, now=GOLDEN_NOW)
    assert sh.list_rows(golden_db, "WATCH") != []
    assert delete_instrument(golden_db, "WATCH") is True
    assert sh.list_rows(golden_db, "WATCH") == []
