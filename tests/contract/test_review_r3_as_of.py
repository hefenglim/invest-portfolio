"""R3/⑬ counter-evidence: the signals ``as_of`` was the wall clock, not the data date.

``signal_history`` has always stamped its rows with the PRICE date. ``signal_states`` and
the ``/api/signals`` wire did not — they stamped ``now.date()``. So on a Monday after a
long weekend, or any day a provider had not delivered yet, the drawer and
``rule_signals_json`` both announced 「資料基準＝今天」 over a series whose last close was
days old, and the prompt守則 (「資料基準 {{as_of}} — 在卡首標注基準日」) faithfully
propagated the wrong date into the card.

``evaluated_at`` (the wall clock) is unchanged and still carried separately — the two
fields were always meant to be two different facts.

The second half: ``freshness_json`` was built from ``FreshnessReport``, which covers only
prices and FX, while four official templates instruct the model to check 新鮮度 against it.
Every external variable already carries its own ``last_as_of``; the fix consolidates them
into a ``sources`` block so the model does not have to walk 16 payloads to find a stale one.
No new query, no invented staleness verdict (there is no defensible 「how many days is a
fundamentals snapshot good for」 — that would be exactly the guess this project forbids).
"""

import sqlite3
from datetime import timedelta
from decimal import Decimal

from portfolio_dash.api import signals_service
from portfolio_dash.api.routers.prompts import _external_vars
from portfolio_dash.data_ingestion.store import upsert_instrument
from portfolio_dash.llm_insight import variables as V
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.portfolio.dashboard_models import DashboardData
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from tests.conftest import GOLDEN_NOW

_END = GOLDEN_NOW.date()
_STALE_GAP = 5  # trading-day-agnostic: the provider simply stopped delivering 5 days ago


def _register(conn: sqlite3.Connection, symbol: str, *, last_gap: int) -> None:
    """Register ``symbol`` with 320 daily closes ending ``last_gap`` days before now."""
    upsert_instrument(conn, Instrument(
        symbol=symbol, market=Market.US, quote_ccy=Currency.USD,
        sector="Tech", name=symbol,
    ))
    last = _END - timedelta(days=last_gap)
    upsert_prices(
        conn,
        [PriceRow(instrument=symbol, market=Market.US,
                  as_of=last - timedelta(days=319 - i),
                  close=Decimal(100 + i) / 10, source="test")
         for i in range(320)],
        fetched_at=GOLDEN_NOW,
    )


# --- ⑬a: the wire + the stored state both report the DATA date ------------------------


def test_a_stale_series_reports_its_own_last_close_not_today(
    golden_db: sqlite3.Connection,
) -> None:
    _register(golden_db, "STALE", last_gap=_STALE_GAP)
    signals, price_as_of = signals_service.evaluate_symbol(
        golden_db, "STALE", now=GOLDEN_NOW
    )
    wire = signals_service.to_wire(
        "STALE", signals, now=GOLDEN_NOW, held=False, price_as_of=price_as_of
    )
    expected = (_END - timedelta(days=_STALE_GAP)).isoformat()
    assert wire["as_of"] == expected, "as_of must be the last close, not the wall clock"
    assert wire["as_of"] != _END.isoformat()
    # The wall clock is still carried — separately, where it belongs.
    assert wire["evaluated_at"] == GOLDEN_NOW.isoformat()


def test_a_symbol_with_no_prices_reports_null_not_today(
    golden_db: sqlite3.Connection,
) -> None:
    """Honest null beats a confident wrong date (the same rule as a labelled stale price)."""
    upsert_instrument(golden_db, Instrument(
        symbol="EMPTY", market=Market.US, quote_ccy=Currency.USD,
        sector="Tech", name="EMPTY",
    ))
    signals, price_as_of = signals_service.evaluate_symbol(
        golden_db, "EMPTY", now=GOLDEN_NOW
    )
    assert price_as_of is None
    wire = signals_service.to_wire(
        "EMPTY", signals, now=GOLDEN_NOW, held=False, price_as_of=price_as_of
    )
    assert wire["as_of"] is None


def test_scan_stamps_signal_states_with_the_price_date(
    golden_db: sqlite3.Connection,
) -> None:
    """``signal_history`` already did this; ``signal_states`` disagreed with its sibling."""
    from portfolio_dash.strategy import signal_history, signal_states

    _register(golden_db, "STALE", last_gap=_STALE_GAP)
    signals_service.scan_signals(golden_db, now=GOLDEN_NOW)
    expected = (_END - timedelta(days=_STALE_GAP)).isoformat()

    state = signal_states.get_state(golden_db, "STALE")
    assert state is not None
    assert state.as_of == expected, "the scan date is not the data date"

    # The two sibling tables must now agree — that was the whole inconsistency.
    hist = signal_history.as_of_set(golden_db, "STALE")
    assert max(hist).isoformat() == state.as_of


def test_the_list_endpoint_reports_the_newest_data_date_across_symbols(
    api_client: object, golden_db: sqlite3.Connection,
) -> None:
    """The universe-level basis date is the newest DATA date, not the wall clock.

    The golden fixture alone would not prove this — its symbols happen to carry prices up
    to GOLDEN_NOW, so the old wall-clock answer and the new one coincide. A deliberately
    stale universe is what separates them, so this registers one whose newest close is
    ``_STALE_GAP`` days back and asserts the endpoint reports THAT.
    """
    from fastapi.testclient import TestClient
    assert isinstance(api_client, TestClient)
    for row in golden_db.execute("SELECT symbol FROM instruments"):
        golden_db.execute(
            "UPDATE instruments SET archived = 1 WHERE symbol = ?", (row["symbol"],)
        )
    golden_db.commit()
    _register(golden_db, "STALE", last_gap=_STALE_GAP)

    body = api_client.get("/api/signals").json()
    assert set(body) == {"as_of", "evaluated_at", "signals"}
    expected = (_END - timedelta(days=_STALE_GAP)).isoformat()
    assert body["as_of"] == expected
    assert body["as_of"] != _END.isoformat(), "the wall-clock answer must be gone"
    assert [s["as_of"] for s in body["signals"]] == [expected]


# --- ⑬b: freshness_json covers every fed source, not just prices/FX -------------------


def test_freshness_json_lists_every_external_source_with_its_basis_date(
    golden_db: sqlite3.Connection,
) -> None:
    ext = _external_vars(golden_db, None, now=GOLDEN_NOW, actions=None)
    ctx = V.VarContext(
        data=_dashboard(golden_db),
        external_vars=ext,
        external_as_of=V.external_as_of_map(ext),
    )
    out = V.value_for("freshness_json", ctx)
    assert isinstance(out, dict)
    sources = out["sources"]
    assert isinstance(sources, list) and sources
    by_token = {s["token"]: s for s in sources}
    # Every token the router actually fed is described — nothing is silently omitted.
    assert set(by_token) == set(ext)
    for row in sources:
        assert set(row) == {"token", "last_as_of", "age_days", "unavailable"}
    # An unavailable source says so and carries no basis date to misread.
    unavailable = [s for s in sources if s["unavailable"]]
    assert unavailable, "the golden DB has no external snapshots — all must read unavailable"
    assert all(s["last_as_of"] is None and s["age_days"] is None for s in unavailable)
    # The price/FX legs are untouched.
    assert "missing_prices" in out and "fx" in out


def _dashboard(conn: sqlite3.Connection) -> DashboardData:
    return build_dashboard(conn, now=GOLDEN_NOW, reporting=Currency.TWD)
