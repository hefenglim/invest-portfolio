"""The change trail and a run's claim on it (spec 2026-10-18 §9.4 "Claiming the dirt", "Every
other end"; plan §6.1 run range table; plan W1-B "Dirt and trail API", "Claim and put-back").
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from portfolio_dash.data_ingestion.store import insert_transaction
from portfolio_dash.portfolio import settlement_store as st
from portfolio_dash.portfolio.settlement_models import RunRequest, RunScope
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Market
from portfolio_dash.shared.models.enums import Side
from tests.reference.corpora import open_db
from tests.settlement.conftest import CorpusFactory

STARTED = datetime(2026, 6, 11, 6, 30, tzinfo=UTC)
FIRST = date(2026, 1, 5)  # golden's first trade
SETTLED = date(2026, 6, 10)


@pytest.fixture
def store(corpus_db: CorpusFactory) -> Iterator[sqlite3.Connection]:
    conn = open_db(corpus_db("golden").path)
    try:
        st.install_or_verify(conn, calc_version_changed=False)
        conn.execute("UPDATE settle_meta SET generation = 1, settled_through = ?, "
                     "held_through = ?, first_day = ? WHERE id = 1",
                     (SETTLED.isoformat(), SETTLED.isoformat(), FIRST.isoformat()))
        conn.commit()
        yield conn
    finally:
        conn.close()


def _buy(conn: sqlite3.Connection, day: date) -> int:
    return insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                              quantity=Decimal("1"), price=Decimal("500"), fees=Decimal("0"),
                              tax=Decimal("0"), trade_date=day)


def _price(conn: sqlite3.Connection, day: date, close: str) -> None:
    upsert_prices(conn, [PriceRow(instrument="2330", market=Market.TW, as_of=day,
                                  close=Decimal(close), source="test")], fetched_at=STARTED)


def _report(conn: sqlite3.Connection, run_id: int, scope: RunScope = "dirty",
            through: date = SETTLED) -> None:
    st.create_run(conn, RunRequest(scope=scope, reason="button", started_by="user:owner",
                                   through=through),
                  run_id=run_id, trigger_text="{started_by} 按下「重新結算」", started_at=STARTED)


def _meta(conn: sqlite3.Connection) -> tuple[object, ...]:
    return tuple(conn.execute("SELECT run_id, run_from, run_through FROM settle_meta").fetchone())


def _dirt(conn: sqlite3.Connection) -> str | None:
    value = conn.execute("SELECT dirty_from FROM settle_dirty").fetchone()[0]
    return None if value is None else str(value)


def test_a_dirty_claim_takes_the_dirt_and_tags_every_pending_row(
        store: sqlite3.Connection) -> None:
    _buy(store, date(2026, 3, 3))
    _price(store, date(2026, 4, 1), "590")
    _report(store, 7)
    c = st.claim(store, 7, scope="dirty", run_through=SETTLED)
    assert (c.scope, c.run_from, c.run_through) == ("dirty", date(2026, 3, 3), SETTLED)
    assert (c.records, c.counters, c.dirty_from) == (1, 1, date(2026, 3, 3))
    assert c.days == (SETTLED - date(2026, 3, 3)).days + 1
    assert _meta(store) == (7, "2026-03-03", SETTLED.isoformat())
    assert _dirt(store) is None
    assert store.execute("SELECT status, range_from, range_to FROM settle_run").fetchone()[:] == (
        "running", "2026-03-03", SETTLED.isoformat())
    page = st.run_changes(store, 7)
    assert page.total_count == 1 and [r.state for r in page.records] == ["claimed"]
    assert [c.state for c in page.counters] == ["claimed"]
    assert st.pending_changes(store).total_count == 0
    report = st.get_run(store, 7)
    assert report is not None and report.range.kind == "dirty" and report.range.days == c.days
    assert report.versions.generation_before == 1


def test_a_clean_dirty_claim_starts_after_the_settled_day(store: sqlite3.Connection) -> None:
    _report(store, 1, through=date(2026, 6, 12))
    c = st.claim(store, 1, scope="nightly", run_through=date(2026, 6, 12))
    assert (c.run_from, c.days) == (date(2026, 6, 11), 2)


def test_a_full_claim_starts_on_the_first_ledger_day_and_generation_0_is_always_full(
        store: sqlite3.Connection) -> None:
    store.execute("UPDATE settle_meta SET generation = 0, settled_through = NULL, "
                  "held_through = NULL, first_day = NULL")
    store.commit()
    assert st.first_ledger_day(store) == FIRST
    _report(store, 1)
    c = st.claim(store, 1, scope="dirty", run_through=SETTLED)
    assert (c.scope, c.run_from) == ("full", FIRST)


def test_the_first_ledger_day_ignores_unregistered_symbols(store: sqlite3.Connection) -> None:
    insert_transaction(store, account_id="tw_broker", symbol="UNREG", side=Side.BUY,
                       quantity=Decimal("1"), price=Decimal("1"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2025, 12, 1))
    assert st.first_ledger_day(store) == FIRST


def test_a_selfcheck_claims_nothing_and_stops_before_the_dirt(store: sqlite3.Connection) -> None:
    _buy(store, date(2026, 5, 1))
    _report(store, 3, scope="selfcheck")
    c = st.claim(store, 3, scope="selfcheck", run_through=SETTLED)
    assert (c.scope, c.run_from, c.run_through) == ("selfcheck", FIRST, date(2026, 4, 30))
    assert (c.records, c.counters) == (0, 0)
    assert _dirt(store) == "2026-05-01", "a self-check leaves the dirt"
    assert _meta(store) == (3, None, "2026-04-30"), "readers must not see the history dirty"
    assert st.pending_changes(store).total_count == 1


def test_a_claim_refuses_a_second_active_run_or_a_report_not_preparing(
        store: sqlite3.Connection) -> None:
    _report(store, 1)
    _report(store, 2)
    st.claim(store, 1, scope="dirty", run_through=SETTLED)
    with pytest.raises(st.ClaimRefused, match="run 1 is active"):
        st.claim(store, 2, scope="dirty", run_through=SETTLED)
    with pytest.raises(st.ClaimRefused, match="running, not preparing"):
        st.claim(store, 1, scope="dirty", run_through=SETTLED)
    with pytest.raises(st.ClaimRefused, match="no report"):
        st.end_without_publish(store, 1, "cancelled")
        st.claim(store, 99, scope="dirty", run_through=SETTLED)


def test_a_claim_cleans_orphan_generations(store: sqlite3.Connection) -> None:
    store.execute("INSERT INTO settled_day VALUES (5, '2026-01-05', '1', '1', NULL, NULL, 0, 0, 0)")
    store.commit()
    _report(store, 1)
    st.claim(store, 1, scope="dirty", run_through=SETTLED)
    assert store.execute("SELECT COUNT(*) FROM settled_day WHERE generation = 5"
                         ).fetchone()[0] == 0


def test_a_claim_re_verifies_the_trigger_set_and_records_it(store: sqlite3.Connection) -> None:
    assert not st.triggers_verified(store)
    _report(store, 1)
    c = st.claim(store, 1, scope="dirty", run_through=SETTLED)
    assert c.triggers_verified and st.triggers_verified(store)
    store.execute(f"DROP TRIGGER {st.TRIGGER_PREFIX}prices_del")
    _report(store, 2)
    st.end_without_publish(store, 1, "failed")
    c = st.claim(store, 2, scope="dirty", run_through=SETTLED)
    assert not c.triggers_verified and not st.triggers_verified(store)


def test_put_back_merges_into_a_same_symbol_pending_counter(store: sqlite3.Connection) -> None:
    _price(store, date(2026, 3, 2), "580")
    _price(store, date(2026, 6, 9), "601")
    _report(store, 4)
    st.claim(store, 4, scope="dirty", run_through=SETTLED)
    _price(store, date(2026, 4, 1), "590")  # a change committed during the run
    assert st.put_back(store, 4) == (0, 1)
    rows = store.execute("SELECT key, changes, earliest, latest, claimed_run_id, settled_run_id, "
                         "attempted_run_ids FROM settle_change_agg").fetchall()
    assert [tuple(r) for r in rows] == [("2330", 3, "2026-03-02", "2026-06-09", None, None, "4")]
    [counter] = st.pending_changes(store).counters
    assert counter.attempted_run_ids == [4] and counter.state == "pending"


def test_put_back_records_the_attempt_and_the_next_run_settles_it(
        store: sqlite3.Connection) -> None:
    _buy(store, date(2026, 3, 3))
    _report(store, 4)
    st.claim(store, 4, scope="dirty", run_through=SETTLED)
    st.end_without_publish(store, 4, "failed")
    [record] = st.pending_changes(store).records
    assert record.attempted_run_ids == [4] and record.claimed_run_id is None
    assert st.run_changes(store, 4).total_count == 1, "a run lists what it handed back"
    _report(store, 5)
    st.claim(store, 5, scope="dirty", run_through=SETTLED)
    assert st.settle_claimed(store, 5) == (1, 0)
    [record] = st.run_changes(store, 5).records
    assert (record.state, record.settled_run_id, record.attempted_run_ids) == ("settled", 5, [4])


@pytest.mark.parametrize(("arrival", "expected"), [
    (date(2026, 2, 1), "2026-02-01"),  # earlier than the run's start: kept
    (date(2026, 5, 1), "2026-03-03"),  # later: the run's start comes back
    (None, "2026-03-03"),
])
def test_an_end_keeps_an_earlier_arrival(store: sqlite3.Connection, arrival: date | None,
                                         expected: str) -> None:
    _buy(store, date(2026, 3, 3))
    _report(store, 6)
    st.claim(store, 6, scope="dirty", run_through=SETTLED)
    if arrival is not None:
        _buy(store, arrival)
    assert st.end_without_publish(store, 6, "cancelled")
    assert _dirt(store) == expected
    assert _meta(store) == (None, None, None)
    assert store.execute("SELECT status FROM settle_run").fetchone()[0] == "cancelled"
    assert not st.end_without_publish(store, 6, "interrupted"), "an end is decided once"


def test_an_end_of_a_run_that_never_claimed_touches_no_other_state(
        store: sqlite3.Connection) -> None:
    _report(store, 1)
    _report(store, 2)
    st.claim(store, 1, scope="dirty", run_through=SETTLED)
    assert st.end_without_publish(store, 2, "failed")
    assert _meta(store)[0] == 1, "the active run keeps the store"


def test_the_change_pages_page_and_summarise_every_row(store: sqlite3.Connection) -> None:
    for day in range(1, 6):
        _buy(store, date(2026, 2, day))
    _price(store, date(2026, 3, 2), "580")
    page = st.pending_changes(store, limit=2, offset=1)
    assert page.total_count == 5 and len(page.records) == 2
    assert [r.new_day for r in page.records] == [date(2026, 2, 2), date(2026, 2, 3)]
    tables = {t.table: (t.label, t.changes, t.keys, t.earliest, t.latest) for t in page.tables}
    assert tables == {"transactions": ("交易", 5, None, date(2026, 2, 1), date(2026, 2, 5)),
                      "prices": ("價格", 1, 1, date(2026, 3, 2), date(2026, 3, 2))}
    assert [t.table for t in page.tables] == ["transactions", "prices"]
