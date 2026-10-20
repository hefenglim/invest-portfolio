"""The run report of the settlement worker (PERF plan §6.1 "Tests" — run reports; spec §9.6,
§14.6, §18 "Run report").

- every end the worker writes (published, partial, superseded, failed) leaves a report with
  its sections; 完成 only on a complete publish;
- "Result — history" reports only real changes: a nightly extension reads 「新結算 1 天」 and no
  change; a reporting-currency change compares nothing; a calc-version change is compared with
  the reason 計算規則更新;
- "Result — closes": a back-dated correction writes restatement rows with label, unit, notes,
  reason and source; the go-live restates a retired month-end snapshot once (``cutover``);
- the ETA rests on the last successful run once there is one.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest

from portfolio_dash.portfolio import settle_worker
from portfolio_dash.portfolio import settlement_store as st
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
    restates only when it moved at ``RATIO_RESTATE_DP`` places; nothing in February moved."""
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


def test_moved_compares_money_exactly_and_a_ratio_at_twelve_places() -> None:
    moved = settle_worker._moved
    assert not moved(Decimal("0.015809862654881264168528325"),
                     Decimal("0.015809862654881264168528324"), "ratio")
    assert moved(Decimal("0.0158098626548"), Decimal("0.0158098626558"), "ratio")
    assert moved(Decimal("100.00"), Decimal("100.01"), "TWD")
    assert not moved(Decimal("100.00"), Decimal("100.00"), "TWD")
    assert moved(None, Decimal("0"), "ratio") and moved(Decimal("0"), None, "TWD")
    assert not moved(None, None, "ratio")

