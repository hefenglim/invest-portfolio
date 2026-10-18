"""The read helpers of spec 2026-10-18 §10 steps 2–3 (plan W1-B "Read helpers"): when the store
is usable, and which days are live or stale, as a pure function of ``S, H, Dd, W``."""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from portfolio_dash.portfolio import settlement_store as st
from portfolio_dash.shared.enums import Currency
from tests.reference.corpora import open_db
from tests.settlement.conftest import CorpusFactory

TODAY = date(2026, 10, 18)
W = date(2026, 9, 1)


def _state(*, settled: date | None = date(2026, 10, 17), held: date | None = None,
           dirty: date | None = None, run_from: date | None = None,
           usable: bool = True) -> st.StoreState:
    return st.StoreState(generation=1, settled_through=settled,
                         held_through=held or settled, first_day=date(2021, 1, 4),
                         dirty_from=dirty, run_from=run_from, calc_ok=usable,
                         currency_ok=True, triggers_ok=True)


def test_window_start_is_the_first_of_the_previous_month() -> None:
    assert st.window_start(TODAY) == W
    assert st.window_start(date(2026, 1, 31)) == date(2025, 12, 1)


@pytest.mark.parametrize(("state", "live", "stale"), [
    # clean: today (or a few days after a gap in runs)
    (_state(), date(2026, 10, 18), None),
    (_state(settled=date(2026, 10, 12)), date(2026, 10, 13), None),
    # dirty inside the window: every point correct, live from the dirty day
    (_state(dirty=date(2026, 9, 20)), date(2026, 9, 20), None),
    # dirty before the window: live from W, the stretch before it stale
    (_state(dirty=date(2025, 6, 16)), W, (date(2025, 6, 16), date(2026, 8, 31))),
    # a range a run claimed is still dirty
    (_state(run_from=date(2025, 6, 16)), W, (date(2025, 6, 16), date(2026, 8, 31))),
    (_state(dirty=date(2026, 9, 3), run_from=date(2025, 6, 16)), W,
     (date(2025, 6, 16), date(2026, 8, 31))),
    # after a partial publish: Dd = S + 1, the held days (S, H] labelled stale before W
    (_state(settled=date(2025, 6, 15), held=date(2026, 10, 17), dirty=date(2025, 6, 16)),
     W, (date(2025, 6, 16), date(2026, 8, 31))),
    (_state(settled=date(2026, 9, 9), held=date(2026, 10, 17), dirty=date(2026, 9, 10)),
     date(2026, 9, 10), None),
    # an unusable store: everything live, nothing stale
    (_state(usable=False, dirty=date(2025, 6, 16)), date.min, None),
])
def test_live_from_and_the_stale_range(state: st.StoreState, live: date,
                                       stale: tuple[date, date] | None) -> None:
    assert st.live_from(state, TODAY) == live
    assert st.stale_range(state, TODAY) == stale


def test_the_dirty_day_is_the_earlier_of_dirty_from_and_run_from() -> None:
    assert _state().dirty_day is None
    assert _state(dirty=date(2026, 3, 1), run_from=date(2026, 2, 1)).dirty_day == date(2026, 2, 1)
    assert _state(dirty=date(2026, 3, 1)).dirty_day == date(2026, 3, 1)


def _set(conn: sqlite3.Connection, **cols: object) -> None:
    conn.execute(f"UPDATE settle_meta SET {', '.join(f'{k} = ?' for k in cols)} WHERE id = 1",
                 list(cols.values()))
    conn.commit()


def test_the_store_is_usable_only_when_every_check_holds(corpus_db: CorpusFactory) -> None:
    conn = open_db(corpus_db("golden").path)
    try:
        st.install_or_verify(conn, calc_version_changed=False)
        twd = Currency.TWD
        assert not st.store_state(conn, reporting=twd).usable  # generation 0
        _set(conn, generation=3, settled_through="2026-06-10", held_through="2026-06-10",
             first_day="2026-01-05", calc_version=st.calc_version(twd), reporting_ccy="TWD")
        state = st.store_state(conn, reporting=twd)
        assert (state.calc_ok, state.currency_ok, state.triggers_ok) == (True, True, False)
        st.record_schema_version(conn)
        state = st.store_state(conn, reporting=twd)
        assert state.usable and state.generation == 3
        assert state.settled_through == date(2026, 6, 10)
        assert state.schema_version == state.trigger_schema_version
        usd = st.store_state(conn, reporting=Currency.USD)
        assert not usd.usable and not usd.calc_ok and not usd.currency_ok
        _set(conn, calc_version="old")
        assert not st.store_state(conn, reporting=twd).usable
        _set(conn, calc_version=st.calc_version(twd))
        conn.execute("CREATE TABLE unrelated_migration (x)")  # any schema change
        assert not st.store_state(conn, reporting=twd).usable
        assert not st.triggers_verified(conn)
    finally:
        conn.close()


def test_a_database_without_the_store_reads_as_empty_and_unusable() -> None:
    conn = sqlite3.connect(":memory:")
    state = st.store_state(conn, reporting=Currency.TWD)
    assert state == st.StoreState() and not state.usable
    assert not st.triggers_verified(conn)
    assert st.live_from(state, TODAY) == date.min


def test_reads_take_no_write_lock(corpus_db: CorpusFactory) -> None:
    db = corpus_db("golden")
    conn = open_db(db.path)
    holder = sqlite3.connect(db.path, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        st.store_state(conn, reporting=Currency.TWD)
        st.triggers_verified(conn)
        st.read_days(conn, 1)
        st.read_periods(conn, 1)
        st.pending_changes(conn)
        st.list_runs(conn)
        conn.set_trace_callback(None)
        assert not any(s.split()[0].upper() in {"INSERT", "UPDATE", "DELETE", "BEGIN"}
                       for s in statements), statements
    finally:
        holder.execute("ROLLBACK")
        holder.close()
        conn.close()
