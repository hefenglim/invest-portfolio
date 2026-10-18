"""The run-report store (spec 2026-10-18 §9.6; plan W1-B "Run-report store"): every run leaves
one ``settle_run`` row created ``preparing`` by the parent, its JSON stored with canonical
Decimal strings, the summary columns the authority."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from portfolio_dash.portfolio import settlement_store as st
from portfolio_dash.portfolio.settlement_models import (
    DiffRow,
    RunFailure,
    RunRequest,
    RunSummary,
)
from tests.reference.corpora import open_db
from tests.settlement.conftest import CorpusFactory

STARTED = datetime(2026, 6, 11, 6, 30, tzinfo=UTC)
THROUGH = date(2026, 6, 10)


@pytest.fixture
def conn(corpus_db: CorpusFactory) -> Iterator[sqlite3.Connection]:
    c = open_db(corpus_db("golden").path)
    try:
        yield c
    finally:
        c.close()


def _request(**kw: object) -> RunRequest:
    base: dict[str, object] = {"scope": "dirty", "reason": "prompt",
                               "started_by": "user:owner", "through": THROUGH}
    return RunRequest.model_validate(base | kw)


def test_create_run_writes_a_preparing_report_with_its_inputs(conn: sqlite3.Connection) -> None:
    req = _request(unconfirmed_closes={date(2026, 5, 29): ["AAPL"]})
    assert st.create_run(conn, req, run_id=12, trigger_text="{started_by} 於 14:30 啟動",
                         started_at=STARTED, eta_initial_s=18.5) == 12
    row = conn.execute("SELECT status, reason, started_by, report FROM settle_run").fetchone()
    assert tuple(row[:3]) == ("preparing", "prompt", "user:owner")
    stored = json.loads(row[3])
    assert stored["inputs"] == {"scope": "dirty", "reason": "prompt",
                                "started_by": "user:owner", "through": "2026-06-10",
                                "unconfirmed_closes": {"2026-05-29": ["AAPL"]}}
    report = st.get_run(conn, 12)
    assert report is not None and report.inputs == req
    assert (report.summary.status, report.summary.status_text) == ("preparing", "準備中")
    assert report.summary.eta_initial_s == 18.5
    assert [r.run_id for r in st.active_or_unfinished_runs(conn)] == [12]


def test_create_run_refuses_an_unknown_reason(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="unknown run reason"):
        st.create_run(conn, _request(reason="whim"), run_id=1, trigger_text="t",
                      started_at=STARTED)
    st.create_run(conn, _request(reason="job:quotes_us"), run_id=2, trigger_text="t",
                  started_at=STARTED)


def test_a_chain_is_linked_both_ways(conn: sqlite3.Connection) -> None:
    st.create_run(conn, _request(), run_id=1, trigger_text="t", started_at=STARTED)
    st.end_without_publish(conn, 1, "interrupted")
    st.update_run(conn, 1, resumed=True)
    st.create_run(conn, _request(reason="resume"), run_id=2, trigger_text="t",
                  started_at=STARTED, previous_run_id=1)
    first, second = st.get_run(conn, 1), st.get_run(conn, 2)
    assert first is not None and second is not None
    assert (first.summary.next_run_id, second.summary.previous_run_id) == (2, 1)
    assert first.summary.status_text == "中斷，已續跑（#2）"


def test_update_run_sets_columns_and_refuses_status_or_unknown_ones(
        conn: sqlite3.Connection) -> None:
    st.create_run(conn, _request(), run_id=1, trigger_text="t", started_at=STARTED)
    report = st.get_run(conn, 1)
    assert report is not None
    st.update_run(conn, 1, eta_initial_s=3.0, report=report.model_copy(update={
        "notify_pending": True}))
    again = st.get_run(conn, 1)
    assert again is not None and again.notify_pending and again.summary.eta_initial_s == 3.0
    with pytest.raises(ValueError, match="updatable"):
        st.update_run(conn, 1, status="published")
    with pytest.raises(ValueError, match="updatable"):
        st.update_run(conn, 1, bogus=1)
    with pytest.raises(ValueError):
        st.update_run(conn, 1, report={"not": "a report"})


def test_finalise_ends_an_active_run_and_keeps_an_end_already_decided(
        conn: sqlite3.Connection) -> None:
    st.create_run(conn, _request(), run_id=1, trigger_text="t", started_at=STARTED)
    report = st.get_run(conn, 1)
    assert report is not None
    with pytest.raises(ValueError, match="only swap"):
        st.finalise_run(conn, 1, "published", report)
    failed = report.model_copy(update={
        "failure": RunFailure(phase="valuate", reason_text="記憶體不足", cancelled_by=None),
        "summary": report.summary.model_copy(update={
            "finished_at": datetime(2026, 6, 11, 6, 30, 14, tzinfo=UTC)})})
    assert st.finalise_run(conn, 1, "failed", failed) == "failed"
    final = st.get_run(conn, 1)
    assert final is not None
    assert (final.summary.status, final.summary.status_text) == ("failed", "失敗")
    assert final.summary.duration_s == 14.0
    assert final.failure is not None and final.failure.phase == "valuate"
    assert st.finalise_run(conn, 1, "cancelled", final) == "failed"
    assert st.active_or_unfinished_runs(conn) == []
    with pytest.raises(ValueError, match="end status"):
        st.finalise_run(conn, 1, "running", final)


def test_cancelled_by_is_recorded(conn: sqlite3.Connection) -> None:
    st.create_run(conn, _request(), run_id=1, trigger_text="t", started_at=STARTED)
    report = st.get_run(conn, 1)
    assert report is not None
    report = report.model_copy(update={"failure": RunFailure(
        reason_text="使用者取消", cancelled_by="user:owner")})
    st.finalise_run(conn, 1, "cancelled", report)
    final = st.get_run(conn, 1)
    assert final is not None and final.summary.cancelled_by == "user:owner"


def test_list_runs_pages_newest_first_and_filters_runs_with_changes(
        conn: sqlite3.Connection) -> None:
    for run_id in (1, 2, 3):
        st.create_run(conn, _request(reason="nightly"), run_id=run_id, trigger_text="t",
                      started_at=STARTED)
        st.end_without_publish(conn, run_id, "failed")
    conn.execute("INSERT INTO settle_change_agg (table_name, key, changes, earliest, latest, "
                 "claimed_run_id, settled_run_id) VALUES ('prices', '2330', 1, '2026-01-02', "
                 "'2026-01-02', 2, 2)")
    conn.commit()
    page = st.list_runs(conn, limit=2)
    assert page.total_count == 3 and [r.run_id for r in page.items] == [3, 2]
    assert all(isinstance(r, RunSummary) for r in page.items)
    changed = st.list_runs(conn, only_changes=True)
    assert [r.run_id for r in changed.items] == [2] and changed.items[0].had_changes
    assert st.get_run(conn, 404) is None


def test_the_diff_round_trips_canonical_strings_and_pages(conn: sqlite3.Connection) -> None:
    st.create_run(conn, _request(), run_id=1, trigger_text="t", started_at=STARTED)
    rows = [
        DiffRow(run_id=1, day=date(2026, 3, 2), field="net_worth", kind="changed",
                before=Decimal("100.50"), after=Decimal("0.0000001"),
                flags_before={"incomplete": False}, flags_after={"incomplete": True}),
        DiffRow(run_id=1, day=date(2026, 3, 2), field="incomplete", kind="flag_flip"),
        DiffRow(run_id=1, day=date(2026, 3, 3), field="total_value", kind="value_to_none",
                before=Decimal("5")),
        DiffRow(run_id=1, day=date(2026, 3, 4), field="total_value", kind="new_day",
                after=Decimal("7")),
    ]
    assert st.write_diff(conn, 1, rows) == 4
    stored = conn.execute("SELECT after FROM settle_run_diff WHERE field = 'net_worth'"
                          ).fetchone()[0]
    assert stored == "0.0000001", "never 1E-7"
    page = st.diff_page(conn, 1, limit=2)
    assert page.total_count == 4 and page.items == rows[1::-1][::-1] or page.items == [
        rows[1], rows[0]]
    assert st.diff_page(conn, 1, field="total_value", kind="new_day").items == [rows[3]]
    with pytest.raises(ValueError, match="written for run"):
        st.write_diff(conn, 2, rows[:1])
    history = st.history_from_diff(conn, 1)
    assert history.counts == {"incomplete": 1, "net_worth": 1, "total_value": 1}
    assert (history.flag_flips, history.value_to_none, history.new_days) == (1, 1, 1)
    assert (history.first_changed_day, history.last_changed_day) == (date(2026, 3, 2),
                                                                     date(2026, 3, 3))
    assert history.largest_net_worth_change is not None
    assert history.largest_net_worth_change.after == Decimal("0.0000001")


def test_restatements_are_stored_and_read_by_close(conn: sqlite3.Connection) -> None:
    ids = st.write_restatements(conn, 7, [
        st.RestatementDraft(kind="M", period="2026-05", field="total_market_value",
                            label="總市值", unit="TWD", old_value=Decimal("1.10"),
                            new_value=None, reason="價格補登", source="prices:2330",
                            at=STARTED, notes_before={"missing_prices": ["2330"]},
                            notes_after={}, cutover=True),
        st.RestatementDraft(kind="Y", period="2025", field="xirr", label="XIRR",
                            unit="ratio", old_value=None, new_value=Decimal("0.05"),
                            reason="r", at=STARTED),
    ])
    [may] = st.restatements_for(conn, "M", "2026-05")
    assert may.id == ids[0] and may.old_value == Decimal("1.10") and may.new_value is None
    assert may.notes_before == {"missing_prices": ["2330"]} and may.cutover
    assert may.run_id == 7 and may.source == "prices:2330"
    assert [r.id for r in st.restatements_of_run(conn, 7)] == ids
