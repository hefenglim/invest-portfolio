"""The run report of the settlement worker (PERF plan §6.1 "Tests" — run reports; spec §9.6,
§14.6, §18 "Run report").

- every end the worker writes (published, partial, superseded, failed) leaves a report with
  its sections; 完成 only on a complete publish;
- "Result — history" reports only real changes: a nightly extension reads 「新結算 1 天」 and no
  change; a reporting-currency change compares nothing; a calc-version change is compared with
  the reason 計算規則更新;
- "Result — closes": a back-dated correction writes restatement rows with label, unit, notes,
  reason and source; each close's reason names the claimed changes on or before its own end
  day (owner ruling 2026-10-22); the go-live restates a retired month-end snapshot once
  (``cutover``);
- the ETA rests on the last successful run once there is one.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from portfolio_dash.data_ingestion.store import audit_source, delete_transaction, undo_source
from portfolio_dash.portfolio import settle_worker
from portfolio_dash.portfolio import settlement_store as st
from portfolio_dash.portfolio.settlement_models import RunRequest
from portfolio_dash.shared.enums import Currency
from tests.reference.corpora import CorpusDB, open_db
from tests.settlement.conftest import CorpusFactory
from tests.settlement.test_worker import prepare, start_run, yesterday


def _report(db: CorpusDB, run_id: int) -> Any:
    conn = open_db(db.path)
    try:
        report = st.get_run(conn, run_id)
        assert report is not None
        return report
    finally:
        conn.close()


def _x(db: CorpusDB, sql: str, params: tuple[Any, ...] = ()) -> int:
    conn = open_db(db.path)
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def _first_day(db: CorpusDB) -> date:
    conn = open_db(db.path)
    try:
        first = st.store_state(conn, reporting=db.reporting).first_day
        assert first is not None
        return first
    finally:
        conn.close()


@pytest.fixture
def demo(corpus_db: CorpusFactory) -> CorpusDB:
    db = corpus_db("demo")
    prepare(db)
    return db


def test_a_published_report_has_every_section(demo: CorpusDB) -> None:
    assert start_run(demo, 1, started_by="user:owner", reason="button") == "published"
    r = _report(demo, 1)
    # a frozen report leaves no step 「待執行」: 預熱 runs in the web process after it (W4)
    assert [p.key for p in r.steps if p.status == "pending"] == []
    assert next(p for p in r.steps if p.key == "prewarm").detail == (
        settle_worker.PREWARM_BY_THE_WEB)
    assert r.summary.status == "published" and r.summary.status_text == "完成"
    assert r.summary.finished_at is not None and r.summary.duration_s is not None
    assert r.range.from_ is not None and r.range.to == yesterday(demo)
    first_day = _first_day(demo)
    # ONE convention: the range is the calendar span from the ledger's first row (a deposit
    # before the first trade) and ``days`` counts its calendar days; ``valued_days`` the trend
    # days valued in it
    assert r.range.kind == "full" and r.range.from_ == date(2026, 1, 2) < first_day
    assert r.range.days == (r.range.to - r.range.from_).days + 1
    assert r.range.valued_days == (r.range.to - first_day).days + 1
    assert r.summary.days == r.range.days and r.summary.range_from == r.range.from_
    assert r.versions.generation_before == 0 and r.versions.generation_after == 1
    assert r.versions.calc_version == st.calc_version(demo.reporting)
    assert r.versions.reporting_ccy == demo.reporting
    assert r.validation.status == "done" and r.validation.findings == []
    assert [s.key for s in r.steps][:7] == [
        "load", "check", "replay", "valuate", "close", "bench", "swap"]
    assert all(s.status == "done" and s.elapsed_s is not None and s.detail for s in r.steps[:7])
    assert r.history is not None and r.history.new_days == r.range.valued_days
    assert r.state is not None and r.state.stale_cleared and r.state.pending_positions == []
    assert r.failure is None
    assert r.changes.records_url == "/api/settlement/changes?state=run:1"
    assert r.summary.eta_initial_s is not None
    assert r.summary.trigger_text == "測試啟動" and r.summary.started_by == "user:owner"


def test_a_nightly_extension_reads_new_day_and_no_change(demo: CorpusDB) -> None:
    through = yesterday(demo)
    assert start_run(demo, 1, through=through - timedelta(days=1)) == "published"
    assert start_run(demo, 2, scope="nightly", reason="nightly", through=through) == "published"
    r = _report(demo, 2)
    assert r.history is not None
    assert r.history.new_days == 1 and r.history.counts == {}
    assert "新結算 1 天" in r.summary_lines
    assert r.restatement_ids == []
    assert r.range.days == 1 and r.range.kind == "dirty"
    conn = open_db(demo.path)
    try:
        rows = st.diff_page(conn, 2).items
        assert [(x.day, x.kind) for x in rows] == [(through, "new_day")]
    finally:
        conn.close()
    # the ETA now rests on the last successful run
    progress = json.loads((demo.path.parent / settle_worker.PROGRESS_FILE).read_bytes())
    assert "上次結算" in progress["eta_basis"]


def test_a_reporting_currency_change_compares_nothing(demo: CorpusDB) -> None:
    assert start_run(demo, 1) == "published"
    usd = CorpusDB(demo.path, demo.now, Currency.USD)
    assert start_run(usd, 2) == "published"
    r = _report(usd, 2)
    assert r.history is not None and r.history.currency_changed
    assert "TWD" in (r.history.note or "") and "USD" in (r.history.note or "")
    assert r.restatement_ids == []
    conn = open_db(demo.path)
    try:
        assert st.diff_page(conn, 2).total_count == 0
        assert st.store_state(conn, reporting=Currency.USD).usable
    finally:
        conn.close()


def test_a_calc_version_change_is_compared_with_its_reason(demo: CorpusDB) -> None:
    assert start_run(demo, 1) == "published"
    _x(demo, "UPDATE settle_meta SET calc_version = 'older-code'")
    assert start_run(demo, 2) == "published"
    r = _report(demo, 2)
    assert r.history is not None and r.history.note == "計算規則更新"
    assert r.history.counts == {}  # the same code computed the same rows


def test_a_backdated_correction_restates_the_closes_it_moves(demo: CorpusDB) -> None:
    assert start_run(demo, 1) == "published"
    conn = open_db(demo.path)
    try:
        row = conn.execute("SELECT account_id, symbol, price FROM transactions "
                           "WHERE side = 'BUY' ORDER BY id LIMIT 1").fetchone()
    finally:
        conn.close()
    tx = _x(demo, "INSERT INTO transactions (account_id, symbol, side, quantity, price, fees, "
                  "tax, trade_date) VALUES (?, ?, 'BUY', '100', ?, '0', '0', '2026-03-03')",
            (row[0], row[1], row[2]))
    assert start_run(demo, 2, scope="dirty", reason="prompt") == "published"
    r = _report(demo, 2)
    assert r.restatement_ids
    conn = open_db(demo.path)
    try:
        rows = st.restatements_of_run(conn, 2)
        march = {x.field: x for x in rows if x.period == "2026-03" and x.kind == "M"}
        assert "total_market_value" in march and march["total_market_value"].label == "總市值"
        assert march["total_market_value"].unit == "TWD"
        assert march["trend_total_value"].label == "證券市值"  # never one word for both
        assert all(x.reason == "2026-03-03 交易更正後重新結算" for x in rows)
        assert all(x.source == f"transactions:{tx}" for x in rows)
        assert all(x.notes_before is not None and x.notes_after is not None for x in rows)
        assert not any(x.cutover for x in rows)
        assert not any(x.period < "2026-03" for x in rows)  # an earlier close did not move
        rates = [x for x in rows if x.field in ("xirr", "total_return_rate")]
        assert rates and all(x.unit == "ratio" for x in rates)
        diff = st.diff_page(conn, 2, limit=1000).items
        assert diff and min(x.day for x in diff) == date(2026, 3, 3)
        assert all(x.kind != "new_day" for x in diff)
        assert r.history.first_changed_day == date(2026, 3, 3)
        assert r.history.largest_net_worth_change is not None
        assert r.history.last_common_day is not None
    finally:
        conn.close()


def test_each_restated_close_names_the_changes_on_or_before_its_day(demo: CorpusDB) -> None:
    """Accountant MINOR 2 (owner ruling 2026-10-22): one run, two corrections — a trade of
    2026-02-05 moved to 02-27 and a dividend of 2026-05-08 raised. A close before May can only
    have moved for the trade; a close from May on for both. Every restated close used to read
    the run's EARLIEST change (「2026-02-05 交易更正後重新結算」 on May–July too)."""
    assert start_run(demo, 1) == "published"
    conn = open_db(demo.path)
    try:
        tx = int(conn.execute("SELECT id FROM transactions WHERE symbol = 'ORBT' "
                              "AND trade_date = '2026-02-05'").fetchone()[0])
        dv = int(conn.execute("SELECT id FROM dividends WHERE symbol = 'KEMB' "
                              "AND date = '2026-05-08'").fetchone()[0])
    finally:
        conn.close()
    _x(demo, "UPDATE transactions SET trade_date = '2026-02-27' WHERE id = ?", (tx,))
    _x(demo, "UPDATE dividends SET gross = '7000', net = '7000' WHERE id = ?", (dv,))
    assert start_run(demo, 2, scope="dirty", reason="prompt") == "published"
    conn = open_db(demo.path)
    try:
        rows = st.restatements_of_run(conn, 2)
    finally:
        conn.close()
    by_period = {(x.kind, x.period): (x.reason, x.source) for x in rows}
    trade_only = ("2026-02-05 交易更正後重新結算", f"transactions:{tx}")
    both = ("2026-02-05 交易、2026-05-08 股利更正後重新結算",
            f"transactions:{tx},dividends:{dv}")
    assert by_period[("M", "2026-02")] == trade_only
    assert by_period[("M", "2026-04")] == trade_only
    for month in ("2026-05", "2026-06", "2026-07"):
        assert by_period[("M", month)] == both, month
    # one reason per close: every field of a close carries it
    for x in rows:
        assert (x.reason, x.source) == by_period[(x.kind, x.period)]


def test_a_chained_run_after_a_partial_publish_names_its_predecessors_changes(
        demo: CorpusDB, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review 2026-10-22, item 1: a trade of 2026-02-05 moved to 02-27, then — while that run
    computes — a dividend of 2026-05-08 raised. The run publishes PARTIALLY through 05-07 and
    hands 05-08 on to a chained run; the closes from May on still hold figures from before BOTH
    corrections (the partial publish keeps the previous generation's closes from 05-08), so
    the chained run's restatements must name both — not only the dividend it claimed."""
    assert start_run(demo, 1) == "published"
    conn = open_db(demo.path)
    try:
        tx = int(conn.execute("SELECT id FROM transactions WHERE symbol = 'ORBT' "
                              "AND trade_date = '2026-02-05'").fetchone()[0])
        dv = int(conn.execute("SELECT id FROM dividends WHERE symbol = 'KEMB' "
                              "AND date = '2026-05-08'").fetchone()[0])
    finally:
        conn.close()
    _x(demo, "UPDATE transactions SET trade_date = '2026-02-27' WHERE id = ?", (tx,))
    real = settle_worker._Run._close

    def close_after_a_late_edit(self: Any, *args: Any, **kwargs: Any) -> Any:
        _x(demo, "UPDATE dividends SET gross = '7000', net = '7000' WHERE id = ?", (dv,))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(settle_worker._Run, "_close", close_after_a_late_edit)
    assert start_run(demo, 2, scope="dirty", reason="prompt") == "partial"
    monkeypatch.undo()
    assert _report(demo, 2).range.handed_from == date(2026, 5, 8)
    assert start_run(demo, 3, scope="dirty", reason="chained", previous=2) == "published"
    conn = open_db(demo.path)
    try:
        partial = {(x.kind, x.period): x.reason for x in st.restatements_of_run(conn, 2)}
        chained = {(x.kind, x.period): x.reason for x in st.restatements_of_run(conn, 3)}
    finally:
        conn.close()
    assert partial[("M", "2026-02")] == "2026-02-05 交易更正後重新結算"
    assert all(p < "2026-05" for _k, p in partial)  # restated only below the hand-over
    both = "2026-02-05 交易、2026-05-08 股利更正後重新結算"
    for month in ("2026-05", "2026-06", "2026-07"):
        assert chained[("M", month)] == both, (month, chained)


def test_the_chain_is_walked_back_through_partial_and_unpublished_runs(
        demo: CorpusDB) -> None:
    """``inherited_changes``: a partial predecessor gives what it settled before its hand-over
    day; a predecessor that published nothing is passed through; a published one ends the
    walk; a run that is not a chained / resume run inherits nothing."""
    assert start_run(demo, 1) == "published"
    conn = open_db(demo.path)
    try:
        assert st.inherited_changes(conn, 1) == []
        report = st.get_run(conn, 1)
        assert report is not None
        # run 2 published PARTIALLY through 03-31 having settled one change of 02-05; run 3,
        # chained after it, was superseded; run 4 chains after run 3
        for run_id, status, reason, prev in ((2, "partial", "prompt", None),
                                             (3, "superseded", "chained", 2),
                                             (4, "running", "chained", 3)):
            conn.execute(
                "INSERT INTO settle_run (run_id, status, reason, trigger_text, started_at, "
                "previous_run_id, report) VALUES (?, ?, ?, '', '2026-08-11T00:00:00+08:00', ?, "
                "?)", (run_id, status, reason, prev, report.model_copy(update={
                    "range": report.range.model_copy(update={
                        "handed_from": date(2026, 4, 1) if run_id == 2 else None})})
                    .model_dump_json(by_alias=True)))
        conn.execute("INSERT INTO settle_change_log (seq, table_name, op, row_key, old_day, "
                     "new_day, written_at, settled_run_id) VALUES (1, 'transactions', "
                     "'update', '7', '2026-02-05', '2026-02-27', '2026-08-11T00:00:00', 2)")
        conn.execute("INSERT INTO settle_change_agg (table_name, key, changes, earliest, "
                     "latest, settled_run_id) VALUES ('prices', '2330', 1, '2026-03-02', "
                     "'2026-03-02', 2)")
        conn.commit()
        got = st.inherited_changes(conn, 4)
        assert [(c.table, c.row_key, c.day) for c in got] == [
            ("transactions", "7", date(2026, 2, 5)), ("prices", "2330", date(2026, 3, 2))]
        # a published predecessor ends the walk
        conn.execute("UPDATE settle_run SET status = 'published' WHERE run_id = 3")
        conn.commit()
        assert st.inherited_changes(conn, 4) == []
    finally:
        conn.close()


def test_a_rebuilt_trigger_reads_as_a_rebuild_never_as_a_correction(demo: CorpusDB) -> None:
    """Review 2026-10-22, item 5: a trigger recreated at boot writes the trail line
    「觸發器重建：交易」 (table ``transactions``, ``row_key`` ``*``). The run it starts restates
    what the unrecorded edit moved; its reason says the triggers were rebuilt, never
    「<first day> 交易更正後重新結算」 as if a trade had been corrected on the store's first day."""
    assert start_run(demo, 1) == "published"
    conn = open_db(demo.path)
    try:
        conn.execute(f"DROP TRIGGER {st.TRIGGER_PREFIX}transactions_upd")
        conn.execute("UPDATE transactions SET quantity = '31' WHERE symbol = 'ORBT' "
                     "AND trade_date = '2026-02-05'")  # recorded by nothing
        conn.commit()
        assert st.install_or_verify(conn, calc_version_changed=False).marked_dirty
        st.record_schema_version(conn)
    finally:
        conn.close()
    assert start_run(demo, 2) == "published"
    conn = open_db(demo.path)
    try:
        rows = st.restatements_of_run(conn, 2)
    finally:
        conn.close()
    assert rows
    assert {x.reason for x in rows} == {"觸發器重建（交易）後重新結算"}, {x.reason for x in rows}


def test_an_undo_is_a_batch_and_hand_deletes_of_imported_rows_are_corrections(
        demo: CorpusDB) -> None:
    """Review 2026-10-22, item 6: an import batch's undo and a hand delete of one of its rows
    both carry the row's batch id; only the undo's ``ledger_audit`` tag
    (``store.undo_source``) tells them apart. Two rows undone together are one 匯入批次; a row
    deleted by hand is a correction of its own."""
    assert start_run(demo, 1) == "published"
    conn = open_db(demo.path)
    try:
        ids = [int(r[0]) for r in conn.execute(
            "SELECT id FROM transactions WHERE trade_date BETWEEN '2026-02-01' AND '2026-06-30' "
            "ORDER BY trade_date, id LIMIT 3")]
        conn.executemany("UPDATE transactions SET import_batch_id = 42 WHERE id = ?",
                         [(i,) for i in ids])
        conn.commit()
        with audit_source(undo_source(42)):
            for i in ids[:2]:
                delete_transaction(conn, i, commit=False)
        delete_transaction(conn, ids[2], commit=False)  # by hand
        conn.commit()
        st.create_run(conn, RunRequest(scope="dirty", reason="prompt", started_by=None,
                                       through=yesterday(demo), unconfirmed_closes={}),
                      run_id=2, trigger_text="測試啟動",
                      started_at=datetime(2026, 8, 11, tzinfo=UTC))
        st.claim(conn, 2, scope="dirty", run_through=yesterday(demo))
        got = {c.row_key: c for c in st.claimed_changes(conn, 2)}
    finally:
        conn.close()
    assert [got[str(i)].batch_undo for i in ids] == [True, True, False]
    assert all(got[str(i)].import_batch_id == "42" for i in ids)
    text = settle_worker.correction_reason(list(got.values()), date(2026, 6, 30),
                                           calc_changed=False)[0]
    first, hand = got[str(ids[0])].day, got[str(ids[2])].day
    assert text.startswith(f"{first.isoformat()} 匯入批次（交易）"), text
    assert f"{hand.isoformat()} 交易" in text, text


def _change(day: str, table: str = "transactions", key: str = "1", *, op: str | None = "update",
            batch: str | None = None, kind: str | None = None,
            undo: bool = False) -> st.ClaimedChange:
    return st.ClaimedChange(table=table, row_key=key, day=date.fromisoformat(day), op=op,
                            import_batch_id=batch, kind=kind, batch_undo=undo)


def test_a_batch_counts_records_and_only_an_undo_groups_deletes() -> None:
    """Review 2026-10-22, item 6: a batch is two or more distinct RECORDS — one row inserted
    and then undone is one record, not two trail rows — and a delete groups only when the undo
    made it; hand deletes of imported rows stay corrections."""
    reason = settle_worker.correction_reason
    one_record = [_change("2026-02-05", key="5", op="insert", batch="9"),
                  _change("2026-02-05", key="5", op="delete", batch="9", undo=True)]
    assert reason(one_record, date(2026, 2, 28), calc_changed=False)[0] == (
        "2026-02-05 交易更正後重新結算")
    by_hand = [_change("2026-02-05", key="5", op="delete", batch="9"),
               _change("2026-03-02", key="6", op="delete", batch="9")]
    assert reason(by_hand, date(2026, 3, 31), calc_changed=False)[0] == (
        "2026-02-05 交易、2026-03-02 交易更正後重新結算")
    undone = [_change("2026-02-05", key="5", op="delete", batch="9", undo=True),
              _change("2026-03-02", "dividends", "6", op="delete", batch="9", undo=True)]
    assert reason(undone, date(2026, 3, 31), calc_changed=False) == (
        "2026-02-05 匯入批次（交易／股利）更正後重新結算", "import_batch:9")


def test_rebuilt_triggers_are_one_undated_entry() -> None:
    reason = settle_worker.correction_reason
    rebuilt = [_change("2026-01-05", key="*", kind=st.TRIGGER_REBUILT_KIND),
               _change("2026-01-05", "dividends", "*", kind=st.TRIGGER_REBUILT_KIND)]
    assert reason(rebuilt, date(2026, 1, 31), calc_changed=False) == (
        "觸發器重建（交易／股利）後重新結算", "trigger_rebuilt:dividends/transactions")
    assert reason([*rebuilt, _change("2026-02-05", key="9")], date(2026, 2, 28),
                  calc_changed=False)[0] == (
        "觸發器重建（交易／股利）、2026-02-05 交易更正後重新結算")


def test_a_reason_names_three_changes_and_counts_the_rest() -> None:
    reason = settle_worker.correction_reason
    changes = [
        _change("2026-01-03", key="1", op="insert", batch="7"),
        _change("2026-01-04", "dividends", "2", op="insert", batch="7"),
        _change("2026-02-05", key="9"),
        _change("2026-02-05", key="10"),
        _change("2026-03-02", "fx_rates", "MYR/TWD", op=None),
        _change("2026-06-09", "prices", "2330", op=None),
        _change("2026-07-01", "cash_movements", "4", op="delete"),
    ]
    # an import batch is ONE entry (its inserts), dated by its earliest record; the two trades
    # of 02-05 read once and are both counted
    assert reason(changes, date(2026, 2, 28), calc_changed=False) == (
        "2026-01-03 匯入批次（交易／股利）、2026-02-05 交易等 3 筆更正後重新結算",
        "import_batch:7,transactions:9")
    assert reason(changes, date(2026, 3, 31), calc_changed=False)[0] == (
        "2026-01-03 匯入批次（交易／股利）、2026-02-05 交易、2026-03-02 匯率（MYR/TWD）"
        "等 4 筆更正後重新結算")
    assert reason(changes, date(2026, 7, 31), calc_changed=False)[0].endswith(
        "等 6 筆更正後重新結算")
    # nothing on or before the close: no correction to name
    assert reason(changes, date(2025, 12, 31), calc_changed=False) == ("重新結算", None)
    # a calc-version change keeps its own reason
    assert reason(changes, date(2026, 2, 28), calc_changed=True)[0] == "計算規則更新"


def test_a_lone_or_edited_imported_row_is_its_own_entry() -> None:
    """A batch is two or more imported rows inserted or deleted together; an UPDATE of an
    imported row is a correction of that row, and a batch of one is that one record."""
    reason = settle_worker.correction_reason
    assert reason([_change("2026-02-05", op="update", batch="7"),
                   _change("2026-02-06", key="2", op="update", batch="7")],
                  date(2026, 2, 28), calc_changed=False)[0] == (
        "2026-02-05 交易、2026-02-06 交易更正後重新結算")
    assert reason([_change("2026-02-05", op="delete", batch="8")], date(2026, 2, 28),
                  calc_changed=False) == ("2026-02-05 交易更正後重新結算", "transactions:1")
    # a counter names its symbol or pair, so two on one day never read the same
    assert reason([_change("2026-06-09", "prices", "2330", op=None),
                   _change("2026-06-09", "prices", "2603", op=None)], date(2026, 6, 30),
                  calc_changed=False)[0] == (
        "2026-06-09 價格（2330）、2026-06-09 價格（2603）更正後重新結算")


def test_the_go_live_restates_a_retired_snapshot_once(demo: CorpusDB) -> None:
    _x(demo, "CREATE TABLE IF NOT EXISTS portfolio_snapshots (month TEXT PRIMARY KEY, "
             "as_of TEXT NOT NULL, reporting_ccy TEXT NOT NULL, total_value TEXT, "
             "total_return TEXT, total_return_rate TEXT, xirr TEXT, by_currency TEXT NOT NULL)")
    _x(demo, "INSERT INTO portfolio_snapshots VALUES ('2026-03', '2026-03-31T23:50:00+08:00', "
             "'TWD', '1', '2', '0.1', NULL, '{\"TWD\": \"1\"}')")
    _x(demo, "INSERT INTO portfolio_snapshots VALUES ('2026-04', '2026-04-30T23:50:00+08:00', "
             "'USD', '1', '2', '0.1', NULL, '{}')")
    # the go-live month: the nightly job's row of 05-18 was never May's month-end freeze
    _x(demo, "INSERT INTO portfolio_snapshots VALUES ('2026-05', '2026-05-18T23:50:00+08:00', "
             "'TWD', '1', '2', '0.1', NULL, '{}')")
    assert start_run(demo, 1) == "published"
    conn = open_db(demo.path)
    try:
        rows = st.restatements_of_run(conn, 1)
        assert rows and all(x.cutover and x.period == "2026-03" for x in rows)
        by_field = {x.field: x for x in rows}
        assert by_field["total_market_value"].old_value == 1
        assert by_field["total_return"].old_value == 2
        assert by_field["total_market_value"].source == "portfolio_snapshots"
        assert "xirr" in by_field and by_field["xirr"].old_value is None
    finally:
        conn.close()
    # a later run moves nothing it already restated (the period is no longer a first publish)
    assert start_run(demo, 2) == "published"
    conn = open_db(demo.path)
    try:
        assert st.restatements_of_run(conn, 2) == []
    finally:
        conn.close()


def test_a_failed_and_a_superseded_report_say_what_happened(demo: CorpusDB,
                                                            monkeypatch: pytest.MonkeyPatch
                                                            ) -> None:
    assert start_run(demo, 1) == "published"

    def boom(*_a: object, **_k: object) -> None:
        raise ValueError("injected")

    monkeypatch.setattr(settle_worker._Run, "_close", boom)
    assert start_run(demo, 2) == "failed"
    monkeypatch.undo()
    r = _report(demo, 2)
    assert r.failure is not None and r.failure.phase == "月結與年結"
    assert r.failure.reason_text == "月結與年結時發生錯誤（ValueError）"
    assert r.failure.error_class == "ValueError"  # job_runs.reason 「failed:ValueError」
    assert r.failure.consequence == "保留第 1 版（結算至 2026-08-10），今天這段照常即時計算"
    steps = {s.key: s.status for s in r.steps}
    assert steps["valuate"] == "done" and steps["close"] == "failed"
    assert steps["swap"] == "pending"
    progress = json.loads((demo.path.parent / settle_worker.PROGRESS_FILE).read_bytes())
    assert progress["status"] == "failed"


def test_a_first_run_failure_says_there_is_no_version_yet(demo: CorpusDB,
                                                          monkeypatch: pytest.MonkeyPatch
                                                          ) -> None:
    monkeypatch.setattr(settle_worker._Run, "_valuate",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("x")))
    assert start_run(demo, 1) == "failed"
    r = _report(demo, 1)
    assert r.failure is not None
    assert r.failure.consequence == "目前尚無結算版本，歷史照常即時計算"
    assert r.summary.status_text == "失敗"


def test_the_findings_of_the_tolerant_replay_are_listed_and_the_run_completes(
        corpus_db: CorpusFactory) -> None:
    db = corpus_db("rich_refusals")
    prepare(db)
    assert start_run(db, 1) == "published"
    r = _report(db, 1)
    assert {f.kind for f in r.validation.findings} == {
        "oversold", "orphan_dividend", "unbookable_dividend", "unapplied_action",
        "refused_close"}
    assert all(f.text.startswith("以待釐清處理：") for f in r.validation.findings)
    assert all(f.day is not None for f in r.validation.findings)
    assert r.state is not None and r.state.pending_positions
    check = next(s for s in r.steps if s.key == "check")
    assert "以待釐清處理" in (check.detail or "")


def test_a_cash_pool_without_a_rate_is_named_in_the_notes(corpus_db: CorpusFactory) -> None:
    """``golden`` stores no MYR rate: a MYR deposit makes a cash pool whose reporting value at
    D is unknown — the close names the pair (MINOR 10)."""
    db = corpus_db("golden")
    _x(db, "INSERT INTO cash_movements (account_id, date, kind, ccy, amount) "
           "VALUES ('moomoo_my', '2026-02-03', 'DEPOSIT', 'MYR', '1000')")
    prepare(db)
    assert start_run(db, 1) == "published"
    conn = open_db(db.path)
    try:
        g = int(conn.execute("SELECT generation FROM settle_meta").fetchone()[0])
        feb = st.read_period(conn, g, "M", "2026-02")
        assert feb is not None and feb.kpi.notes.missing_cash_fx == ["MYR/TWD"]
        jan = st.read_period(conn, g, "M", "2026-01")
        assert jan is not None and jan.kpi.notes.missing_cash_fx == []
        pool = [c for c in st.read_period_cash(conn, g, "M", "2026-02")
                if c.account_id == "moomoo_my" and c.ccy.value == "MYR"]
        assert [(c.balance, c.reporting_value) for c in pool] == [(Decimal("1000"), None)]
    finally:
        conn.close()


def test_a_correction_that_moves_no_figure_restates_nothing(corpus_db: CorpusFactory) -> None:
    """Accountant as-built check (2026-10-20): on rich, 2330's close on Thu 2026-02-12 moves
    548 → 560. With no flow on the 12th or the 13th the two daily ratios cancel, so February's
    TWR is unchanged in fact — but the chain-linked index shifts its 27th digit. A ratio
    restates only when it moved beyond ``RATIO_RESTATE_TOLERANCE``; nothing in February moved."""
    rich = corpus_db("rich")
    prepare(rich)
    assert start_run(rich, 1) == "published"
    _x(rich, "UPDATE prices SET close = '560', close_raw = '560' "
             "WHERE instrument = '2330' AND as_of_date = '2026-02-12'")
    assert start_run(rich, 2, scope="dirty", reason="prompt") == "published"
    conn = open_db(rich.path)
    try:
        rows = conn.execute("SELECT period, field, old_value, new_value FROM "
                            "settle_restatement WHERE run_id = 2").fetchall()
    finally:
        conn.close()
    assert [r for r in rows if r[0] == "2026-02"] == [], rows
    for _period, field, old, new in rows:  # whatever did restate moved for real
        if old is not None and new is not None and field in (
                "period_twr", "total_return_rate", "xirr"):
            assert abs(Decimal(old) - Decimal(new)) >= Decimal("1e-12"), (field, old, new)


def test_moved_compares_money_exactly_and_a_ratio_within_a_tolerance() -> None:
    moved = settle_worker._moved
    assert not moved(Decimal("0.015809862654881264168528325"),
                     Decimal("0.015809862654881264168528324"), "ratio")
    assert moved(Decimal("0.0158098626548"), Decimal("0.0158098626568"), "ratio")
    # a huge ratio (a 30x gain in the first month annualises to ~2e17) never raises, its
    # last-digit noise does not restate, a real move does
    huge = Decimal("195277164635138020.1234567890")
    assert not moved(huge, huge + Decimal("0.0000000001"), "ratio")
    assert moved(huge, huge * Decimal("1.0001"), "ratio")
    assert moved(Decimal("100.00"), Decimal("100.01"), "TWD")
    assert not moved(Decimal("100.00"), Decimal("100.00"), "TWD")
    assert moved(None, Decimal("0"), "ratio") and moved(Decimal("0"), None, "TWD")
    assert not moved(None, None, "ratio")


def test_a_huge_xirr_never_fails_the_run_that_recomputes_it(corpus_db: CorpusFactory) -> None:
    """Accountant re-verification (2026-10-20): a ledger that gains 30x in its first month has
    a January 累計 XIRR near 2e17 (the 30-day gate admits it, as the live KPI shows). A dirty
    run that recomputed it compared it by quantizing to 12 places under 28 digits, raised
    ``InvalidOperation`` and failed — and so would every later run."""
    from datetime import datetime, timedelta

    from portfolio_dash.data_ingestion.config_seed import seed_accounts
    from portfolio_dash.shared.enums import Market
    from portfolio_dash.shared.models.enums import Side
    from tests.conftest import init_golden_base
    from tests.reference.corpora import TAIPEI, _closes, _instrument, _trade

    path = corpus_db("golden").path.with_name("huge-xirr.db")
    conn = open_db(path)
    try:
        init_golden_base(conn)
        seed_accounts(conn)
        _instrument(conn, "HUGE", Market.TW, Currency.TWD, board="TWSE")
        days = [date(2025, 12, 31) + timedelta(days=i) for i in range(70)]
        _closes(conn, "HUGE", Market.TW, [d for d in days if d.weekday() < 5],
                lambda n, d: Decimal("1") if d == date(2025, 12, 31) else Decimal("30"),
                fetched_at=datetime(2026, 3, 10, 12))
        _trade(conn, "tw_broker", "HUGE", Side.BUY, "1000", "1", date(2025, 12, 31), fees="20")
        conn.commit()
    finally:
        conn.close()
    db = CorpusDB(path, datetime(2026, 3, 10, 14, 30, tzinfo=TAIPEI), Currency.TWD)
    prepare(db)
    assert start_run(db, 1) == "published"
    _x(db, "UPDATE transactions SET fees = '21' WHERE symbol = 'HUGE'")
    assert start_run(db, 2, scope="dirty", reason="prompt") == "published"
    conn = open_db(path)
    try:
        jan = conn.execute("SELECT field, old_value, new_value FROM settle_restatement "
                           "WHERE run_id = 2 AND period = '2026-01' AND field = 'xirr'"
                           ).fetchall()
    finally:
        conn.close()
    assert jan and Decimal(jan[0][1]) > Decimal("1e16")  # the huge XIRR, restated, not fatal

