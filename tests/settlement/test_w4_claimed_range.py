"""A run's claimed RANGE is not a claimed CHANGE (final senior review, 2026-10-20).

Readers' ``Dd`` (``StoreState.dirty_day``) is the earlier of ``dirty_from`` and the ``dirty_from``
the active run claimed — not its ``run_from``. A full run on a clean store claims no change, so
for the whole run every settled day is served as current: no 更新前版本, no withheld drawdown
alert, no 待結算 TWR. A run that claimed a change keeps that change's day dirty until its swap
(review A #1). The stored benchmark still waits for anything pending OR claimed (§10 step 5,
``claimed_day``).
"""

from __future__ import annotations

from datetime import date

from portfolio_dash.portfolio import settlement_store as st
from portfolio_dash.portfolio.settlement_models import RunRequest
from tests.reference.corpora import CorpusDB, open_db
from tests.settlement.conftest import CorpusFactory
from tests.settlement.test_worker import prepare, start_run, yesterday

BACK_DATED = date(2026, 3, 4)


def _claim_full(db: CorpusDB, run_id: int) -> st.StoreState:
    conn = open_db(db.path)
    try:
        st.create_run(conn, RunRequest(scope="full", reason="button", through=yesterday(db)),
                      run_id=run_id, trigger_text="測試啟動", started_at=db.now)
        st.claim(conn, run_id, scope="full", run_through=yesterday(db))
        conn.commit()
        return st.store_state(conn, reporting=db.reporting)
    finally:
        conn.close()


def _insert_back_dated_buy(db: CorpusDB) -> None:
    from decimal import Decimal

    from portfolio_dash.data_ingestion.store import insert_transaction
    from portfolio_dash.shared.models.enums import Side

    conn = open_db(db.path)
    try:
        insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                           quantity=Decimal("1"), price=Decimal("500"), fees=Decimal("0"),
                           tax=Decimal("0"), trade_date=BACK_DATED)
        conn.commit()
    finally:
        conn.close()


def test_a_full_run_on_a_clean_store_marks_nothing_stale(corpus_db: CorpusFactory) -> None:
    db = corpus_db("demo")
    prepare(db)
    assert start_run(db, 1) == "published"
    state = _claim_full(db, 2)
    assert state.run_id == 2 and state.run_from is not None  # the range is claimed ...
    assert state.run_dirty_from is None                       # ... no change is
    assert state.dirty_day is None
    assert st.stale_range(state, db.now.date()) is None
    assert state.claimed_day == state.run_from  # the stored benchmark still waits for it


def test_a_full_run_that_claimed_a_change_keeps_its_day_dirty(corpus_db: CorpusFactory) -> None:
    db = corpus_db("demo")
    prepare(db)
    assert start_run(db, 1) == "published"
    _insert_back_dated_buy(db)
    state = _claim_full(db, 2)
    assert state.dirty_from is None and state.run_dirty_from == BACK_DATED
    assert state.dirty_day == BACK_DATED
    stale = st.stale_range(state, db.now.date())
    assert stale is not None and stale[0] == BACK_DATED


def test_the_claimed_change_day_is_cleared_with_the_run(corpus_db: CorpusFactory) -> None:
    db = corpus_db("demo")
    prepare(db)
    assert start_run(db, 1) == "published"
    _insert_back_dated_buy(db)
    assert start_run(db, 2, scope="dirty", reason="prompt") == "published"
    conn = open_db(db.path)
    try:
        state = st.store_state(conn, reporting=db.reporting)
    finally:
        conn.close()
    assert (state.run_id, state.run_from, state.run_dirty_from, state.dirty_day) == (
        None, None, None, None)
