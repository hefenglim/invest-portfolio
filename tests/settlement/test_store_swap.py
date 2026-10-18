"""Generations and the swap (spec 2026-10-18 §6 "Generations", §9.4 "Publishing", "Every other
end", §9.6 retention; plan W1-B "Generations API", "swap", and the "Swap" test rows).

Each test drives runs on a private ``golden`` copy with synthetic settled rows (value
``100`` / ``200`` per run): the swap's arithmetic is about which rows land in which
generation, not about what a day is worth.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from portfolio_dash.data_ingestion.store import insert_transaction
from portfolio_dash.portfolio import settlement_store as st
from portfolio_dash.portfolio.settlement_models import (
    DiffRow,
    HistoryResult,
    PeriodCash,
    PeriodHolding,
    PeriodKpi,
    RunReport,
    RunRequest,
    RunScope,
)
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.enums import Side
from tests.reference.corpora import open_db
from tests.settlement.conftest import CorpusFactory

TWD = Currency.TWD
STARTED = datetime(2026, 6, 11, 6, 30, tzinfo=UTC)
FIRST = date(2026, 1, 5)
THROUGH = date(2026, 6, 10)
CALC = st.calc_version(TWD)
ONE = timedelta(days=1)


def _span(a: date, b: date) -> list[date]:
    return [a + timedelta(days=i) for i in range((b - a).days + 1)]


def _day(d: date, value: str) -> st.SettledDay:
    v = Decimal(value)
    return st.SettledDay(day=d, total_value=v, net_invested=Decimal("50"), net_worth=v + 1,
                         cash_total=Decimal("1"), cash_incomplete=False, incomplete=False,
                         options_open=False)


def _periods(start: date, end: date, value: str) -> list[st.SettledPeriod]:
    return [st.SettledPeriod(kind=k, period=p, end_day=e,
                             kpi=PeriodKpi(reporting_ccy=TWD, total_market_value=Decimal(value)))
            for k, p, e in st.period_ends(FIRST, end) if e >= start]


@pytest.fixture
def conn(corpus_db: CorpusFactory) -> Iterator[sqlite3.Connection]:
    c = open_db(corpus_db("golden").path)
    try:
        st.install_or_verify(c, calc_version_changed=False)
        yield c
    finally:
        c.close()


def _start(conn: sqlite3.Connection, run_id: int, *, scope: RunScope = "dirty",
           through: date = THROUGH) -> st.Claim:
    st.create_run(conn, RunRequest(scope=scope, reason="button", through=through),
                  run_id=run_id, trigger_text="t", started_at=STARTED)
    return st.claim(conn, run_id, scope=scope, run_through=through)


def _compute(conn: sqlite3.Connection, c: st.Claim, value: str, *,
             through: date = THROUGH) -> int:
    """The worker's writes: copy what precedes the range, then the range's rows."""
    assert c.run_from is not None
    new = st.next_generation(conn)
    if c.generation > 0:
        st.copy_before(conn, c.generation, new, c.run_from)
    st.write_days(conn, new, [_day(d, value) for d in _span(c.run_from, through)])
    periods = _periods(c.run_from, through, value)
    st.write_periods(conn, new, periods)
    for p in periods:
        st.write_period_holdings(conn, new, p.kind, p.period, [PeriodHolding(
            account_id="tw_broker", symbol="2330", asset_type="stock", quote_ccy=TWD,
            shares=Decimal("1"), original_cost_total=Decimal("1"),
            adjusted_cost_total=Decimal("1"), market_value=Decimal(value))])
        st.write_period_cash(conn, new, p.kind, p.period, [PeriodCash(
            account_id="tw_broker", ccy=TWD, balance=Decimal(value))])
    return new


def _close_values(conn: sqlite3.Connection, generation: int, period: str) -> tuple[str, ...]:
    """A month close's KPI value, its position's market value and its cash balance."""
    close = st.read_period(conn, generation, "M", period)
    [holding] = st.read_period_holdings(conn, generation, "M", period)
    [cash] = st.read_period_cash(conn, generation, "M", period)
    assert close is not None
    return (str(close.kpi.total_market_value), str(holding.market_value), str(cash.balance))


def _publish(conn: sqlite3.Connection, run_id: int, *, through: date = THROUGH,
             bench: str | None = '{"units":"1"}') -> st.SwapResult:
    return st.swap(conn, run_id, calc_version=CALC, bench_state=bench, through=through,
                   first_day=FIRST, reporting=TWD)


def _values(conn: sqlite3.Connection, generation: int) -> dict[date, str]:
    return {d.day: str(d.total_value) for d in st.read_days(conn, generation)}


def _buy(conn: sqlite3.Connection, day: date) -> None:
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("1"), price=Decimal("500"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=day)


def _first_generation(conn: sqlite3.Connection) -> None:
    c = _start(conn, 1, scope="full")
    _compute(conn, c, "100")
    assert _publish(conn, 1).status == "published"


# --- a full publish ----------------------------------------------------------------------------


def test_a_full_publish_writes_the_generation_and_its_state(conn: sqlite3.Connection) -> None:
    _buy(conn, date(2026, 2, 2))  # before anything is settled: not recorded
    c = _start(conn, 1, scope="full")
    assert (c.scope, c.run_from, c.generation) == ("full", FIRST, 0)
    assert _compute(conn, c, "100") == 1
    st.write_period_holdings(conn, 1, "Y", "2025", [PeriodHolding(
        account_id="tw_broker", symbol="2330", asset_type="stock", quote_ccy=TWD,
        shares=Decimal("1000"), original_cost_total=Decimal("500000.00"),
        adjusted_cost_total=Decimal("500000.00"), price=Decimal("600"),
        flags={"oversold": False})])
    st.write_period_cash(conn, 1, "Y", "2025", [PeriodCash(
        account_id="tw_broker", ccy=TWD, balance=Decimal("-0.50"))])
    result = _publish(conn, 1)
    assert (result.status, result.generation, result.settled_through, result.held_through) == (
        "published", 1, THROUGH, THROUGH)
    state = st.store_state(conn, reporting=TWD)
    assert (state.generation, state.settled_through, state.held_through, state.first_day) == (
        1, THROUGH, THROUGH, FIRST)
    assert (state.calc_ok, state.currency_ok, state.bench_state) == (True, True,
                                                                   '{"units":"1"}')
    assert state.run_id is None and state.run_from is None and state.run_through is None
    assert set(_values(conn, 1)) == set(_span(FIRST, THROUGH))
    assert [(p.kind, p.period) for p in st.read_periods(conn, 1)][:2] == [("M", "2026-01"),
                                                                          ("M", "2026-02")]
    [holding] = st.read_period_holdings(conn, 1, "Y", "2025")
    assert str(holding.original_cost_total) == "500000.00"  # the exponent is kept
    assert holding.flags == {"oversold": False}
    assert str(st.read_period_cash(conn, 1, "Y", "2025")[0].balance) == "-0.50"
    run = st.get_run(conn, 1)
    assert run is not None and run.summary.status == "published"
    assert (run.summary.generation_after, run.summary.settled_through_after) == (1, THROUGH)
    assert run.versions.calc_version == CALC and run.versions.reporting_ccy == TWD


def test_a_dirty_run_copies_what_precedes_it_and_deletes_the_old_generation(
        conn: sqlite3.Connection) -> None:
    _first_generation(conn)
    _buy(conn, date(2026, 3, 3))
    c = _start(conn, 2)
    assert (c.run_from, c.records) == (date(2026, 3, 3), 1)
    _compute(conn, c, "200")
    result = _publish(conn, 2)
    assert (result.status, result.generation, result.settled_records) == ("published", 2, 1)
    values = _values(conn, 2)
    assert values[date(2026, 3, 2)] == "100" and values[date(2026, 3, 3)] == "200"
    assert conn.execute("SELECT COUNT(*) FROM settled_day WHERE generation = 1"
                        ).fetchone()[0] == 0
    assert _close_values(conn, 2, "2026-02") == ("100", "100", "100")  # copied before
    assert _close_values(conn, 2, "2026-03") == ("200", "200", "200")
    assert st.run_changes(conn, 2).records[0].state == "settled"
    assert st.list_runs(conn, only_changes=True).total_count == 1


# --- a partial publish ---------------------------------------------------------------------------


def test_a_partial_publish_keeps_the_held_days_and_hands_the_rest_on(
        conn: sqlite3.Connection) -> None:
    _first_generation(conn)
    _buy(conn, date(2026, 3, 3))
    _buy(conn, date(2026, 5, 20))  # claimed, but its effect lies after N: handed back
    c = _start(conn, 2)
    new = _compute(conn, c, "200")
    st.write_diff(conn, 2, [DiffRow(run_id=2, day=d, field="total_value", kind="changed",
                                    before=Decimal("100"), after=Decimal("200"))
                            for d in _span(date(2026, 3, 3), THROUGH)])
    ids = st.write_restatements(conn, 2, [
        st.RestatementDraft(kind="M", period=p, field="total_market_value", label="總市值",
                            unit="TWD", old_value=Decimal("100"), new_value=Decimal("200"),
                            reason="計算規則更新", at=STARTED) for p in ("2026-03", "2026-05")])
    report = st.get_run(conn, 2)
    assert report is not None
    st.update_run(conn, 2, report=report.model_copy(update={
        "history": HistoryResult(note="keep me"), "restatement_ids": ids}))
    n = date(2026, 5, 10)
    _buy(conn, n)  # a change committed during the run
    result = _publish(conn, 2)
    assert (result.status, result.generation, result.settled_through, result.held_through,
            result.handed_from) == ("partial", new, n - ONE, THROUGH, n)
    assert (result.settled_records, result.put_back_records) == (1, 1)
    values = _values(conn, new)
    assert set(values) == set(_span(FIRST, THROUGH))
    assert values[date(2026, 3, 2)] == "100" and values[n - ONE] == "200"
    assert values[n] == "100" and values[THROUGH] == "100", "the held days are G's"
    assert _close_values(conn, new, "2026-02") == ("100", "100", "100")
    assert _close_values(conn, new, "2026-04") == ("200", "200", "200")
    assert _close_values(conn, new, "2026-05") == ("100", "100", "100"), "G's held close"
    assert conn.execute("SELECT COUNT(*) FROM settled_period_holding WHERE generation = ?",
                        (new,)).fetchone()[0] == 5
    state = st.store_state(conn, reporting=TWD)
    assert state.bench_state is None and state.dirty_from == n
    # this run's diff and restatements are trimmed to the published range
    assert st.diff_page(conn, 2, limit=1000).total_count == (n - date(2026, 3, 3)).days
    assert [r.period for r in st.restatements_of_run(conn, 2)] == ["2026-03"]
    final = st.get_run(conn, 2)
    assert final is not None and final.summary.status == "partial"
    assert final.restatement_ids == ids[:1]
    assert (final.range.published_to, final.range.handed_from) == (n - ONE, n)
    history = final.history
    assert history is not None and history.note == "keep me"
    assert history.counts == {"total_value": (n - date(2026, 3, 3)).days}
    assert history.last_changed_day == n - ONE
    assert history.last_common_day is not None and history.last_common_day.day == n - ONE
    assert history.last_common_day.before == Decimal("101")  # G's net worth on that day
    assert history.last_common_day.after == Decimal("201")
    # records: the 03-03 change settled; the 05-20 one back to pending beside the new 05-10
    pending = st.pending_changes(conn)
    assert sorted(r.new_day for r in pending.records if r.new_day) == [n, date(2026, 5, 20)]
    assert [r.attempted_run_ids for r in pending.records if r.new_day == date(2026, 5, 20)
            ] == [[2]]
    # and the chained run starts at N and publishes in full
    chained = _start(conn, 3)
    assert chained.run_from == n
    _compute(conn, chained, "300")
    assert _publish(conn, 3).status == "published"
    assert _values(conn, new + 1)[n - ONE] == "200"
    assert _values(conn, new + 1)[n] == "300"


def test_a_publish_that_extends_new_days_keeps_them_after_a_partial(
        conn: sqlite3.Connection) -> None:
    _first_generation(conn)
    _buy(conn, date(2026, 3, 3))
    later = THROUGH + timedelta(days=20)
    c = _start(conn, 2, through=later)
    _compute(conn, c, "200", through=later)
    n = THROUGH + timedelta(days=5)
    _buy(conn, n)
    result = _publish(conn, 2, through=later)
    assert (result.settled_through, result.held_through) == (n - ONE, n - ONE)
    assert max(_values(conn, 2)) == n - ONE


# --- the claimed range is never short of the held days (wave-1 review B #1) ----------------------


def test_a_chained_run_asked_to_stop_inside_the_held_days_publishes_them_all(
        conn: sqlite3.Connection) -> None:
    _first_generation(conn)
    _buy(conn, date(2026, 6, 2))
    c2 = _start(conn, 2)
    _compute(conn, c2, "200")
    _buy(conn, date(2026, 6, 5))  # during run 2
    assert _publish(conn, 2).status == "partial"
    state = st.store_state(conn, reporting=TWD)
    assert (state.settled_through, state.held_through) == (date(2026, 6, 4), THROUGH)
    c3 = _start(conn, 3, through=date(2026, 6, 7))  # S <= through < H
    assert (c3.requested_through, c3.run_through, c3.through_clamped) == (
        date(2026, 6, 7), THROUGH, True)
    assert c3.run_from == date(2026, 6, 5)
    with pytest.raises(st.SwapRefused) as refused:
        _publish(conn, 3, through=date(2026, 6, 7))
    assert refused.value.reason == "through_mismatch"
    _compute(conn, c3, "300", through=c3.run_through)
    assert _publish(conn, 3, through=c3.run_through).status == "published"
    state = st.store_state(conn, reporting=TWD)
    assert (state.settled_through, state.held_through, state.dirty_from) == (
        THROUGH, THROUGH, None)
    assert st.stale_range(state, date(2026, 6, 11)) is None
    assert _values(conn, state.generation)[date(2026, 6, 8)] == "300"
    assert st.pending_changes(conn).total_count == 0


def test_a_dirty_run_asked_to_stop_before_the_settled_day_settles_through_it(
        conn: sqlite3.Connection) -> None:
    _first_generation(conn)
    _buy(conn, date(2026, 6, 8))  # a correction inside the settled range
    c = _start(conn, 2, through=date(2026, 6, 5))  # e.g. final_through moved back
    assert (c.run_from, c.run_through, c.through_clamped, c.days) == (
        date(2026, 6, 8), THROUGH, True, 3)
    _compute(conn, c, "200", through=c.run_through)
    result = _publish(conn, 2, through=c.run_through)
    assert (result.status, result.settled_through, result.settled_records) == (
        "published", THROUGH, 1)
    assert _values(conn, 2)[date(2026, 6, 8)] == "200"


def test_a_claim_that_needs_no_clamp_says_so(conn: sqlite3.Connection) -> None:
    _first_generation(conn)
    later = THROUGH + timedelta(days=2)
    c = _start(conn, 2, through=later)
    assert (c.requested_through, c.run_through, c.through_clamped) == (later, later, False)


# --- the other outcomes ------------------------------------------------------------------------


@pytest.mark.parametrize(("arrival", "status"), [
    (date(2026, 3, 3), "superseded"),  # N == range_from: nothing of the range is publishable
    (date(2026, 3, 4), "partial"),     # one day later: range_from alone is published
])
def test_the_range_start_boundary(conn: sqlite3.Connection, arrival: date, status: str) -> None:
    _first_generation(conn)
    _buy(conn, date(2026, 3, 3))
    c = _start(conn, 2)
    assert c.run_from == date(2026, 3, 3)
    _compute(conn, c, "200")
    _buy(conn, arrival)
    result = _publish(conn, 2)
    assert result.status == status
    if status == "partial":
        assert result.settled_through == date(2026, 3, 3)
        assert _values(conn, 2)[date(2026, 3, 3)] == "200"
    else:
        assert result.generation == 1 and st.store_state(conn, reporting=TWD).dirty_from == (
            date(2026, 3, 3))


def test_a_superseded_run_moves_nothing(conn: sqlite3.Connection) -> None:
    _first_generation(conn)
    _buy(conn, date(2026, 3, 3))
    c = _start(conn, 2)
    _compute(conn, c, "200")
    _buy(conn, date(2026, 2, 1))  # before the run's range
    result = _publish(conn, 2)
    assert (result.status, result.generation, result.settled_through) == (
        "superseded", 1, THROUGH)
    assert result.put_back_records == 1
    state = st.store_state(conn, reporting=TWD)
    assert (state.generation, state.dirty_from, state.run_id) == (1, date(2026, 2, 1), None)
    assert conn.execute("SELECT COUNT(*) FROM settled_day WHERE generation = 2"
                        ).fetchone()[0] == 0
    assert set(_values(conn, 1).values()) == {"100"}
    assert st.get_run(conn, 2).summary.status == "superseded"  # type: ignore[union-attr]


def test_an_orphan_run_cannot_swap(conn: sqlite3.Connection) -> None:
    _first_generation(conn)
    _buy(conn, date(2026, 3, 3))
    c = _start(conn, 2)
    _compute(conn, c, "200")
    st.end_without_publish(conn, 2, "interrupted")  # boot's cleanup
    with pytest.raises(st.SwapRefused) as refused:
        _publish(conn, 2)
    assert refused.value.reason == "not_current_run"
    assert st.store_state(conn, reporting=TWD).generation == 1


def test_a_failed_completeness_assertion_refuses_the_swap_and_changes_nothing(
        conn: sqlite3.Connection) -> None:
    _first_generation(conn)
    _buy(conn, date(2026, 3, 3))
    c = _start(conn, 2)
    new = _compute(conn, c, "200")
    conn.execute("DELETE FROM settled_day WHERE generation = ? AND day = '2026-04-04'", (new,))
    conn.commit()
    with pytest.raises(st.SwapRefused) as refused:
        _publish(conn, 2)
    assert refused.value.reason == "incomplete"
    state = st.store_state(conn, reporting=TWD)
    assert (state.generation, state.run_id) == (1, 2)
    assert conn.execute("SELECT status FROM settle_run WHERE run_id = 2").fetchone()[0] == (
        "running")
    conn.execute("DELETE FROM settled_period WHERE generation = ? AND period = '2026-04'",
                 (new,))
    conn.execute("INSERT INTO settled_day VALUES (?, '2026-04-04', '1', '1', NULL, NULL, 0, 0, 0)",
                 (new,))
    conn.commit()
    with pytest.raises(st.SwapRefused, match="closes missing"):
        _publish(conn, 2)
    assert st.finalise_run(conn, 2, "failed", st.get_run(conn, 2)) == "failed"  # type: ignore[arg-type]
    assert st.store_state(conn, reporting=TWD).generation == 1


def test_cancel_after_the_swap_leaves_the_published_generation(
        conn: sqlite3.Connection) -> None:
    _first_generation(conn)
    report = st.get_run(conn, 1)
    assert report is not None
    assert not st.end_without_publish(conn, 1, "cancelled")
    assert st.finalise_run(conn, 1, "cancelled", report) == "published"
    assert st.store_state(conn, reporting=TWD).generation == 1


def test_an_empty_ledger_publishes_nothing_on_an_empty_store(conn: sqlite3.Connection) -> None:
    c = _start(conn, 1, scope="full")
    assert c.generation == 0
    result = st.swap(conn, 1, calc_version=CALC, bench_state=None, through=THROUGH,
                     first_day=None, reporting=TWD)
    assert (result.status, result.generation) == ("published", 0)
    assert st.store_state(conn, reporting=TWD).generation == 0


def test_the_writers_refuse_the_published_generation(conn: sqlite3.Connection) -> None:
    _first_generation(conn)
    with pytest.raises(ValueError, match="published"):
        st.write_days(conn, 1, [_day(THROUGH, "1")])
    with pytest.raises(ValueError, match="ends on"):
        st.write_periods(conn, 2, [st.SettledPeriod(kind="M", period="2026-01",
                                                    end_day=date(2026, 1, 30),
                                                    kpi=PeriodKpi(reporting_ccy=TWD))])


def test_write_days_commits_once_per_month(conn: sqlite3.Connection) -> None:
    commits: list[int] = []
    conn.set_trace_callback(lambda sql: commits.append(1) if sql == "COMMIT" else None)
    st.write_days(conn, 1, [_day(d, "1") for d in _span(date(2026, 1, 30), date(2026, 3, 2))])
    conn.set_trace_callback(None)
    assert len(commits) == 3


def test_cleanup_orphans_deletes_only_generations_above_the_published_one(
        conn: sqlite3.Connection) -> None:
    _first_generation(conn)
    st.write_days(conn, 2, [_day(THROUGH, "1")])
    st.write_days(conn, 3, [_day(THROUGH, "1")])
    assert st.cleanup_orphans(conn) == 2
    assert len(st.read_days(conn, 1)) == len(_span(FIRST, THROUGH))


# --- retention --------------------------------------------------------------------------------


def test_pruning_keeps_the_latest_runs_and_every_restatement(conn: sqlite3.Connection) -> None:
    for run_id in range(1, 9):
        st.create_run(conn, RunRequest(scope="dirty", reason="nightly", through=THROUGH),
                      run_id=run_id, trigger_text="t", started_at=STARTED)
        st.end_without_publish(conn, run_id, "failed")
        st.write_diff(conn, run_id, [DiffRow(run_id=run_id, day=THROUGH, field="net_worth",
                                             kind="changed")])
        st.write_restatements(conn, run_id, [st.RestatementDraft(
            kind="M", period="2026-05", field="xirr", label="XIRR", unit="ratio",
            old_value=None, new_value=Decimal("0.1"), reason="r", at=STARTED)])
    conn.execute("INSERT INTO settle_change_log (seq, table_name, op, row_key, written_at, "
                 "settled_run_id) VALUES (1, 'transactions', 'insert', '1', 'x', 2)")
    st.create_run(conn, RunRequest(scope="dirty", reason="nightly", through=THROUGH),
                  run_id=9, trigger_text="t", started_at=STARTED)  # active: never pruned
    conn.commit()
    assert st.prune(conn, keep=3) == 6
    remaining = [r.run_id for r in st.list_runs(conn).items]
    assert remaining == [9, 8, 7]
    assert conn.execute("SELECT COUNT(*) FROM settle_run_diff").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM settle_change_log").fetchone()[0] == 0
    assert len(st.restatements_for(conn, "M", "2026-05")) == 8


def test_the_swap_prunes_to_400(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO settle_run (run_id, status, reason, trigger_text, started_at, report) "
        "VALUES (?, 'failed', 'nightly', 't', '2026-01-01T00:00:00+00:00', ?)",
        [(i, RunReport.model_validate({
            "summary": {"run_id": i, "status": "failed", "status_text": "失敗",
                        "reason": "nightly", "trigger_text": "t",
                        "started_at": "2026-01-01T00:00:00Z"},
            "inputs": {"scope": "dirty", "reason": "nightly", "through": "2026-06-10"},
        }).model_dump_json(by_alias=True)) for i in range(1, 402)])
    conn.commit()
    c = _start(conn, 500, scope="full")
    _compute(conn, c, "1")
    assert _publish(conn, 500).pruned == 2
    assert st.list_runs(conn, limit=1).total_count == 400
