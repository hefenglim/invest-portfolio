"""The settlement worker's runs: failures, concurrent changes, interruptions, the self-check and
the lock (PERF plan §6.1 "Tests" — failures and concurrent changes; spec §9.4, §18 "Store",
"Run report").

Every "≡ fresh" assertion compares the published generation, row for row and byte for byte
(``settlement_store.generation_rows``), with an independent full run on a COPY of the same
database whose store was reset (``drop_store``) — never with the run under test.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from portfolio_dash.portfolio import settle_worker
from portfolio_dash.portfolio import settlement_store as st
from portfolio_dash.portfolio.settlement_models import PHASE_LABELS, job_status_for
from portfolio_dash.shared import proc_lock
from tests.reference.corpora import CorpusDB, copy_db, open_db
from tests.settlement.conftest import CorpusFactory
from tests.settlement.test_worker import generation, prepare, start_run, store_rows, yesterday

# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------


def _x(db: CorpusDB, sql: str, params: tuple[Any, ...] = ()) -> int:
    """One write through a plain connection (the triggers fire for any writer)."""
    conn = open_db(db.path)
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def _backdated_buy(db: CorpusDB, day: date, qty: str = "1") -> int:
    conn = open_db(db.path)
    try:
        row = conn.execute("SELECT account_id, symbol, price FROM transactions "
                           "WHERE side = 'BUY' ORDER BY id LIMIT 1").fetchone()
    finally:
        conn.close()
    return _x(db, "INSERT INTO transactions (account_id, symbol, side, quantity, price, fees, "
                  "tax, trade_date) VALUES (?, ?, 'BUY', ?, ?, '0', '0', ?)",
              (row[0], row[1], qty, row[2], day.isoformat()))


def _fresh(db: CorpusDB, tmp_path: Path) -> dict[str, dict[str, Any]]:
    """An independent full run on a copy of *db* with its store reset."""
    copy = CorpusDB(copy_db(db.path, tmp_path / f"fresh-{db.path.stem}.db"), db.now,
                    db.reporting)
    conn = open_db(copy.path)
    try:
        st.drop_store(conn)
        st.create_tables(conn)
    finally:
        conn.close()
    prepare(copy)
    assert start_run(copy, 900) == "published"
    conn = open_db(copy.path)
    try:
        first = st.store_state(conn, reporting=db.reporting).first_day
        assert first is not None
        return store_rows(conn, first, yesterday(db))
    finally:
        conn.close()


def _published(db: CorpusDB) -> dict[str, dict[str, Any]]:
    conn = open_db(db.path)
    try:
        state = st.store_state(conn, reporting=db.reporting)
        assert state.usable, state
        assert state.first_day is not None
        return store_rows(conn, state.first_day, yesterday(db))
    finally:
        conn.close()


def _state(db: CorpusDB) -> st.StoreState:
    conn = open_db(db.path)
    try:
        return st.store_state(conn, reporting=db.reporting)
    finally:
        conn.close()


def _report(db: CorpusDB, run_id: int) -> Any:
    conn = open_db(db.path)
    try:
        report = st.get_run(conn, run_id)
        assert report is not None
        return report
    finally:
        conn.close()


@pytest.fixture
def demo(corpus_db: CorpusFactory) -> CorpusDB:
    db = corpus_db("demo")
    prepare(db)
    assert start_run(db, 1) == "published"
    return db


# ---------------------------------------------------------------------------------------------
# failures
# ---------------------------------------------------------------------------------------------

_PHASE_METHODS = {"load": "_load", "check": "_check", "replay": "_plan",
                  "valuate": "_valuate", "close": "_close", "bench": "_bench"}


@pytest.mark.parametrize("phase", [*_PHASE_METHODS, "swap"])
def test_a_failure_in_any_phase_leaves_the_published_generation_whole(
        demo: CorpusDB, phase: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    before = _published(demo)
    _backdated_buy(demo, date(2026, 3, 4))
    dirty = _state(demo).dirty_from
    assert dirty == date(2026, 3, 4)

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("injected")

    if phase == "swap":
        monkeypatch.setattr(st, "swap", boom)
    else:
        monkeypatch.setattr(settle_worker._Run, _PHASE_METHODS[phase], boom)
    assert start_run(demo, 2, scope="dirty", reason="prompt") == "failed"
    monkeypatch.undo()
    state = _state(demo)
    assert state.generation == 1 and state.run_id is None and state.run_from is None
    assert state.dirty_from == dirty  # put back: the earlier of dirty_from and run_from
    assert _published(demo) == before  # G intact, its rows byte for byte
    conn = open_db(demo.path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM settled_day WHERE generation > 1"
                            ).fetchone()[0] == 0
        trail = conn.execute("SELECT claimed_run_id, settled_run_id, attempted_run_ids FROM "
                             "settle_change_log").fetchall()
        assert [tuple(r) for r in trail] == [(None, None, "2")]
    finally:
        conn.close()
    report = _report(demo, 2)
    assert report.summary.status == "failed" and report.summary.status_text == "失敗"
    assert report.failure is not None
    assert report.failure.phase == PHASE_LABELS[phase]
    assert "RuntimeError" in report.failure.reason_text
    assert report.failure.consequence == (
        "保留第 1 版（結算至 2026-08-10）；這 1 筆交易變更回到待結算")
    assert "本次未生效，已退回待結算" in report.summary_lines
    assert report.changes.handed_back
    # the retry publishes, and the result is a fresh run's
    assert start_run(demo, 3, scope="dirty", reason="button", previous=2) == "published"
    retry = _report(demo, 3)
    assert retry.changes.attempted_by == [2]
    assert any("#2" in line for line in retry.summary_lines)
    assert _published(demo) == _fresh(demo, tmp_path)


def test_a_refused_swap_fails_the_run_and_never_reads_done(demo: CorpusDB,
                                                          monkeypatch: pytest.MonkeyPatch
                                                          ) -> None:
    real = st.swap

    def short_a_day(conn: sqlite3.Connection, run_id: int, **kw: Any) -> Any:
        conn.execute("DELETE FROM settled_day WHERE generation = 2 AND day = '2026-05-05'")
        conn.commit()
        return real(conn, run_id, **kw)

    monkeypatch.setattr(st, "swap", short_a_day)
    assert start_run(demo, 2) == "failed"
    report = _report(demo, 2)
    assert report.failure is not None and "完整性檢查" in report.failure.reason_text
    assert report.summary.status_text != "完成"
    assert _state(demo).generation == 1
    # a generation nobody was shown leaves no history and no restatement behind
    assert report.history is None and report.restatement_ids == []
    conn = open_db(demo.path)
    try:
        assert st.diff_page(conn, 2).total_count == 0
        assert st.restatements_of_run(conn, 2) == []
    finally:
        conn.close()


def test_the_lock_held_elsewhere_fails_only_a_preparing_run(corpus_db: CorpusFactory,
                                                            monkeypatch: pytest.MonkeyPatch
                                                            ) -> None:
    db = corpus_db("golden")
    prepare(db)
    monkeypatch.setattr(settle_worker, "LOCK_TIMEOUT_S", 0.05)
    with proc_lock.lock(proc_lock.settlement_lock_path(db.path.resolve().parent), timeout=1):
        assert start_run(db, 1) == "failed"
    report = _report(db, 1)
    assert report.failure is not None and "結算鎖" in report.failure.reason_text
    assert _state(db).generation == 0


# ---------------------------------------------------------------------------------------------
# changes during a run (§9.4): before the claim, between the claim and the snapshot, during the
# computation, just before the swap — every one ends ≡ fresh after the chained run
# ---------------------------------------------------------------------------------------------


def _wrap(monkeypatch: pytest.MonkeyPatch, owner: Any, name: str,
          before: Callable[[], object]) -> None:
    real = getattr(owner, name)

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        before()
        return real(*args, **kwargs)

    monkeypatch.setattr(owner, name, wrapped)


@pytest.mark.parametrize("moment", ["before_claim", "after_claim", "computing", "before_swap"])
def test_a_change_during_a_run_is_settled_by_the_chained_run(
        demo: CorpusDB, moment: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _backdated_buy(demo, date(2026, 2, 3))
    late = date(2026, 5, 6)

    def change() -> None:
        _backdated_buy(demo, late, qty="2")

    if moment == "before_claim":
        change()
    elif moment == "after_claim":
        _wrap(monkeypatch, settle_worker._Run, "_load", change)
    elif moment == "computing":
        _wrap(monkeypatch, settle_worker._Run, "_close", change)
    else:
        _wrap(monkeypatch, st, "swap", change)
    status = start_run(demo, 2, scope="dirty", reason="prompt")
    monkeypatch.undo()
    if moment == "before_claim":
        assert status == "published"
        assert _state(demo).dirty_from is None
    else:
        # the change arrived after the claim: published below it, handed to the chained run
        assert status == "partial"
        state = _state(demo)
        assert state.settled_through == late - timedelta(days=1)
        assert state.held_through == yesterday(demo)
        assert state.dirty_from == late and state.bench_state is None
        report = _report(demo, 2)
        assert report.range.handed_from == late
        assert report.summary.status_text == "部分完成，接續處理"
        assert start_run(demo, 3, scope="dirty", reason="chained", previous=2) == "published"
        chained = _report(demo, 3)
        assert f"延續 #2 的變更於 {late.isoformat()} 起的影響" in chained.summary_lines
    assert _published(demo) == _fresh(demo, tmp_path)


def test_a_change_before_the_start_supersedes_the_run(demo: CorpusDB,
                                                      monkeypatch: pytest.MonkeyPatch,
                                                      tmp_path: Path) -> None:
    _backdated_buy(demo, date(2026, 5, 4))
    _wrap(monkeypatch, settle_worker._Run, "_close",
          lambda: _backdated_buy(demo, date(2026, 2, 2)))
    assert start_run(demo, 2, scope="dirty", reason="prompt") == "superseded"
    monkeypatch.undo()
    state = _state(demo)
    assert state.generation == 1 and state.dirty_from == date(2026, 2, 2)
    report = _report(demo, 2)
    assert report.summary.status_text == "已併入下一輪"
    assert "本次未生效，已退回待結算" in report.summary_lines
    assert report.history is None and report.changes.handed_back
    assert start_run(demo, 3, scope="dirty", reason="chained", previous=2) == "published"
    assert _published(demo) == _fresh(demo, tmp_path)


def test_a_partial_first_generation_and_its_chained_run(corpus_db: CorpusFactory,
                                                        monkeypatch: pytest.MonkeyPatch,
                                                        tmp_path: Path) -> None:
    db = corpus_db("demo")
    prepare(db)
    _wrap(monkeypatch, settle_worker._Run, "_close",
          lambda: _backdated_buy(db, date(2026, 6, 2)))
    assert start_run(db, 1) == "partial"
    monkeypatch.undo()
    state = _state(db)
    assert state.generation == 1 and state.settled_through == date(2026, 6, 1)
    assert state.held_through == date(2026, 6, 1)
    assert start_run(db, 2, scope="dirty", reason="chained", previous=1) == "published"
    assert _published(db) == _fresh(db, tmp_path)


# ---------------------------------------------------------------------------------------------
# interruption: the trail is never truncated, and every change is settled by exactly one run
# ---------------------------------------------------------------------------------------------


def test_a_thousand_backdated_rows_survive_an_interruption(demo: CorpusDB,
                                                          monkeypatch: pytest.MonkeyPatch,
                                                          tmp_path: Path) -> None:
    conn = open_db(demo.path)
    try:
        row = conn.execute("SELECT account_id, symbol, price FROM transactions "
                           "WHERE side = 'BUY' ORDER BY id LIMIT 1").fetchone()
        for i in range(1000):
            day = date(2026, 2, 1) + timedelta(days=i % 150)
            conn.execute("INSERT INTO transactions (account_id, symbol, side, quantity, price, "
                         "fees, tax, trade_date) VALUES (?, ?, 'BUY', '1', ?, '0', '0', ?)",
                         (row[0], row[1], row[2], day.isoformat()))
        conn.commit()
    finally:
        conn.close()

    def killed(*_a: object, **_k: object) -> None:
        raise KeyboardInterrupt  # a kill: nothing in the worker ends the run

    monkeypatch.setattr(settle_worker._Run, "_close", killed)
    with pytest.raises(KeyboardInterrupt):
        start_run(demo, 2, scope="dirty", reason="prompt")
    monkeypatch.undo()
    conn = open_db(demo.path)
    try:
        assert st.get_run(conn, 2) is not None
        assert conn.execute("SELECT status FROM settle_run WHERE run_id = 2").fetchone()[0] \
            == "running"
        assert st.end_without_publish(conn, 2, "interrupted")  # what the service's waiter does
        pending = st.pending_changes(conn, limit=5000)
        assert pending.total_count == 1000
        assert all(r.attempted_run_ids == [2] for r in pending.records)
    finally:
        conn.close()
    assert start_run(demo, 3, scope="dirty", reason="resume", previous=2) == "published"
    conn = open_db(demo.path)
    try:
        settled = conn.execute("SELECT COUNT(*), COUNT(DISTINCT settled_run_id) FROM "
                               "settle_change_log").fetchone()
        assert tuple(settled) == (1000, 1)
        assert conn.execute("SELECT DISTINCT settled_run_id FROM settle_change_log"
                            ).fetchone()[0] == 3
        page = st.run_changes(conn, 3, limit=2000)
        assert page.total_count == 1000 and len(page.records) == 1000
    finally:
        conn.close()
    resumed = _report(demo, 3)
    assert resumed.changes.attempted_by == [2]
    assert "其中 1000 筆變更曾於 #2 嘗試" in resumed.summary_lines
    assert resumed.changes.records_total == 1000
    assert _published(demo) == _fresh(demo, tmp_path)


# ---------------------------------------------------------------------------------------------
# the weekly self-check
# ---------------------------------------------------------------------------------------------


def test_a_selfcheck_of_a_clean_store_finds_nothing(demo: CorpusDB) -> None:
    assert start_run(demo, 2, scope="selfcheck", reason="selfcheck") == "published"
    report = _report(demo, 2)
    assert report.selfcheck is not None and report.selfcheck.differences == 0
    assert not report.summary.selfcheck_diff
    assert job_status_for(report.summary.status, selfcheck_diff=False) == "ok"


def test_a_selfcheck_names_the_first_differing_day_and_table(demo: CorpusDB) -> None:
    _x(demo, "UPDATE settled_day SET total_value = '1' WHERE generation = 1 AND day = ?",
       ("2026-04-07",))
    _x(demo, "UPDATE settled_day SET total_value = '2' WHERE generation = 1 AND day = ?",
       ("2026-06-01",))
    assert start_run(demo, 2, scope="selfcheck", reason="selfcheck") == "published"
    report = _report(demo, 2)
    assert report.selfcheck is not None
    assert report.selfcheck.differences == 2
    assert report.selfcheck.first_day == date(2026, 4, 7)
    assert report.selfcheck.first_table == "settled_day"
    assert report.summary.selfcheck_diff
    assert job_status_for(report.summary.status, selfcheck_diff=True) == "partial"
    # the recompute replaced the tampered rows (it publishes through the ordinary swap)
    conn = open_db(demo.path)
    try:
        row = conn.execute("SELECT total_value FROM settled_day WHERE generation = ? "
                           "AND day = '2026-04-07'", (generation(conn),)).fetchone()
        assert row[0] != "1"
    finally:
        conn.close()


def test_a_selfcheck_skips_the_pending_range(demo: CorpusDB) -> None:
    _backdated_buy(demo, date(2026, 6, 15))
    assert start_run(demo, 2, scope="selfcheck", reason="selfcheck") == "published"
    report = _report(demo, 2)
    assert report.selfcheck is not None
    assert report.selfcheck.compared_through == date(2026, 6, 14)
    assert report.selfcheck.differences == 0
    assert _state(demo).dirty_from == date(2026, 6, 15)  # a self-check claims nothing


# ---------------------------------------------------------------------------------------------
# the option underlying's price (W2-D finding: nothing a close stores reads it)
# ---------------------------------------------------------------------------------------------


def test_nothing_stored_depends_on_an_option_underlyings_price(
        corpus_db: CorpusFactory, tmp_path: Path) -> None:
    """``options_block_of`` reads each open contract's UNDERLYING price — but only into the
    block's display fields (``underlying_price``): KPI 總市值 reads the block's market value,
    which is the contracts' last trades (R2). So a close stores nothing that depends on an
    underlying no ledger holds, and the ``prices`` trigger — which watches ledger symbols and
    benchmark keys only — needs no gate for one: changing such a price marks nothing dirty,
    and a fresh run publishes the very same rows."""
    from portfolio_dash.data_ingestion.register import register_option_contract

    db = corpus_db("golden")
    conn = open_db(db.path)
    try:
        conn.execute("INSERT INTO instruments (symbol, market, quote_ccy, sector, name) "
                     "VALUES ('UNDR', 'US', 'USD', 'Tech', 'underlying only')")
        register_option_contract(conn, "UNDR261218P00010000", commit=False)
        conn.execute("INSERT INTO transactions (account_id, symbol, side, quantity, price, fees, "
                     "tax, trade_date, short_sale) VALUES ('schwab', 'UNDR261218P00010000', "
                     "'SELL', '100', '1.5', '0', '0', '2026-02-02', 1)")
        for day, close in (("2026-02-02", "11"), ("2026-03-02", "12"), ("2026-04-01", "13")):
            conn.execute("INSERT INTO prices (instrument, market, as_of_date, close, source, "
                         "fetched_at) VALUES ('UNDR', 'US', ?, ?, 'test', '2026-04-01T00:00:00')",
                         (day, close))
        conn.commit()
    finally:
        conn.close()
    prepare(db)
    assert start_run(db, 1) == "published"
    before = _published(db)
    conn = open_db(db.path)
    try:
        g = generation(conn)
        march = st.read_period(conn, g, "M", "2026-03")
        assert march is not None and march.kpi.notes.options_value_excluded
        option_rows = [h for h in st.read_period_holdings(conn, g, "M", "2026-03")
                       if h.asset_type == "option"]
        assert [h.symbol for h in option_rows] == ["UNDR261218P00010000"]
        seq = conn.execute("SELECT seq FROM settle_dirty").fetchone()[0]
        conn.execute("UPDATE prices SET close = '99' WHERE instrument = 'UNDR'")
        conn.commit()
        assert conn.execute("SELECT dirty_from, seq FROM settle_dirty").fetchone()[1] == seq
        assert conn.execute("SELECT dirty_from FROM settle_dirty").fetchone()[0] is None
    finally:
        conn.close()
    assert _fresh(db, tmp_path) == before


# ---------------------------------------------------------------------------------------------
# the put-back of a price counter merges with one written during the run
# ---------------------------------------------------------------------------------------------


def test_a_price_counter_put_back_merges_with_the_one_written_during_the_run(
        demo: CorpusDB, monkeypatch: pytest.MonkeyPatch) -> None:
    from decimal import Decimal

    conn = open_db(demo.path)
    try:
        sym = conn.execute("SELECT symbol FROM transactions ORDER BY id LIMIT 1").fetchone()[0]
        rows = conn.execute("SELECT as_of_date, close FROM prices WHERE instrument = ? "
                            "AND as_of_date <= '2026-08-10' ORDER BY as_of_date LIMIT 2",
                            (sym,)).fetchall()
    finally:
        conn.close()
    assert len(rows) == 2

    def bump(row: Any) -> None:
        _x(demo, "UPDATE prices SET close = ? WHERE instrument = ? AND as_of_date = ?",
           (str(Decimal(row[1]) + 1), sym, row[0]))

    bump(rows[0])
    _wrap(monkeypatch, settle_worker._Run, "_close", lambda: bump(rows[1]))
    monkeypatch.setattr(settle_worker._Run, "_bench",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("x")))
    assert start_run(demo, 2, scope="dirty", reason="prompt") == "failed"
    monkeypatch.undo()
    conn = open_db(demo.path)
    try:
        counters = [c for c in st.pending_changes(conn).counters
                    if c.table == "prices" and c.key == sym]
        assert len(counters) == 1
        [counter] = counters
        assert counter.changes == 2 and counter.attempted_run_ids == [2]
        assert counter.earliest == date.fromisoformat(rows[0][0])
        assert counter.latest == date.fromisoformat(rows[1][0])
    finally:
        conn.close()
