"""ONE seam restates the price basis AND invalidates the derived signal rows (W6 / AI-D27).

A restated basis makes every stored evaluation of the symbol wrong (``signal_history``), and a
stale ``signal_states`` row would diff against the next scan's evaluation and fire PHANTOM
transition events. That invalidation lived in the corporate-action doors' wrapper
(``api/routers/ledgers.py::reconcile_split_prices``), so ``scripts/check_split_window.py
--apply``, which called the reconcile directly, restated prices and left the signal rows behind
(review of the split-window fix, measured: 1 ``signal_states`` row kept vs 0 through the doors).
The invalidation now lives in ``api.instrument_service.restate_price_basis``, which every
restatement path calls — the doors through ``reconcile_split_prices`` / ``reconcile_price_basis``
and the script directly.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.api import instrument_service
from portfolio_dash.api.instrument_service import reconcile_price_basis, restate_price_basis
from portfolio_dash.api.routers.ledgers import reconcile_split_prices
from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.store import insert_corporate_action
from portfolio_dash.pricing.reconcile import Restatement
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.scheduler.jobs import split_factor_fn
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Market
from portfolio_dash.strategy import signal_history, signal_states

TAIPEI = ZoneInfo("Asia/Taipei")
NEW_YORK = ZoneInfo("America/New_York")
_EX = date(2026, 6, 16)
_LAST = date(2026, 6, 15)
_FIRED = datetime.combine(_LAST, time(16, 30), tzinfo=NEW_YORK).astimezone(TAIPEI)


def _seed_signals(conn: sqlite3.Connection, symbols: Iterable[str]) -> None:
    signal_history.ensure_table(conn)
    signal_states.ensure_table(conn)
    for s in symbols:
        conn.execute("INSERT INTO signal_history (symbol, as_of, params_version, updated_at) "
                     "VALUES (?, '2026-06-15', 'v1', '2026-06-16T04:31:00+08:00')", (s,))
        conn.execute("INSERT INTO signal_states (symbol, params_version, as_of, updated_at) "
                     "VALUES (?, 'v1', '2026-06-15', '2026-06-16T04:31:00+08:00')", (s,))
    conn.commit()


def _signal_symbols(conn: sqlite3.Connection) -> tuple[set[str], set[str]]:
    return ({str(r[0]) for r in conn.execute("SELECT symbol FROM signal_history")},
            {str(r[0]) for r in conn.execute("SELECT symbol FROM signal_states")})


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    """AAA's last pre-split session stored under the OLD window (8000 / 20); BBB has no SPLIT."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    create_pricing_tables(c)
    insert_corporate_action(
        c, account_id="schwab", action_date=_EX, kind=CorporateActionKind.SPLIT,
        from_symbol="AAA", to_symbol="AAA", ratio_to=Decimal("20"), ratio_from=Decimal("1"),
    )
    real = split_factor_fn(c)

    def old_window(symbol: str, *, after: date, through: date) -> Decimal:
        return real(symbol, after=after, through=_EX)

    upsert_prices(c, [PriceRow(instrument=s, market=Market.US, as_of=_LAST,
                               close=Decimal("400"), source="test") for s in ("AAA", "BBB")],
                  fetched_at=_FIRED, factor_of=old_window)
    _seed_signals(c, ["AAA", "BBB"])
    yield c
    c.close()


def test_the_seam_restates_the_rows_and_clears_only_those_symbols_signal_rows(
    conn: sqlite3.Connection,
) -> None:
    applied = restate_price_basis(conn, ["AAA"])
    assert [(r.symbol, r.as_of_date, r.current[0], r.current_basis, r.wanted[0],
             r.wanted_basis) for r in applied] == [("AAA", "2026-06-15", "8000", "20", "400", "1")]
    assert _signal_symbols(conn) == ({"BBB"}, {"BBB"})


def test_the_seam_clears_signal_rows_even_in_a_prices_less_database() -> None:
    bare = sqlite3.connect(":memory:")
    bare.row_factory = sqlite3.Row
    bootstrap_db(bare)
    _seed_signals(bare, ["AAA"])
    assert restate_price_basis(bare, ["AAA"]) == []
    assert _signal_symbols(bare) == (set(), set())
    bare.close()


def test_no_symbols_is_a_no_op(conn: sqlite3.Connection) -> None:
    assert restate_price_basis(conn, ["", ""]) == []
    assert _signal_symbols(conn) == ({"AAA", "BBB"}, {"AAA", "BBB"})


def test_the_corporate_action_doors_and_the_count_entry_go_through_the_seam(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    original = instrument_service.restate_price_basis

    def spy(c: sqlite3.Connection, symbols: Iterable[str]) -> list[Restatement]:
        wanted = list(symbols)
        calls.append(wanted)
        return original(c, wanted)

    monkeypatch.setattr(instrument_service, "restate_price_basis", spy)
    assert reconcile_split_prices(conn, ["AAA", "BBB"]) == 1
    assert reconcile_price_basis(conn, ["AAA"]) == 0
    assert calls == [["AAA", "BBB"], ["AAA"]]
    assert _signal_symbols(conn) == (set(), set())
