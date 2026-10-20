"""Placeholders: the days of a series unavailable from some day U (package-D review BLOCKER 1
and MAJOR 2).

- **A placeholder never outlives the condition that made it.** Under v0.1.31's trend pair set
  (the quote currencies only — PERF-F2, handed to the worker here) ``unquoted_ccy_cash`` stores
  placeholders from its MYR rebate (2026-03-16). A MYR instrument bought on 2026-05-04 brings
  MYR into that pair set — the series is available again from the first day — while the
  trigger marks the store dirty only from 05-04. The dirty run must value from the first
  placeholder: chained ≡ fresh, byte for byte, on every settle table. (With the fixed pair set
  the widening is defense in depth with no known production trigger: every flow's own
  currency is in the set, and every change that can end a missing-rate cause — a rate on or
  before U, an edit or deletion of the flow — is dated on or before U and marks the store
  dirty from there. Hence the monkeypatch.)
- **A placeholder is never a figure in a report.** A back-dated buy before the first rate
  makes the whole series unavailable: the history reads value → none (never → 0), counts no
  placeholder day as 「新結算」, and a close with no trend point says so in
  ``notes.trend_unavailable`` without calling complete KPIs incomplete.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import date, timedelta
from pathlib import Path

import pytest

from portfolio_dash.portfolio import settle_worker
from portfolio_dash.portfolio import settlement_store as st
from portfolio_dash.portfolio.returns import CashMovementFlow
from portfolio_dash.portfolio.timeseries import trend_symbols
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.ledger import LedgerBundle
from tests.reference import perf_f2
from tests.reference.corpora import open_db
from tests.settlement.conftest import CorpusFactory
from tests.settlement.test_worker import generation, prepare, start_run, yesterday
from tests.settlement.test_worker_runs import _fresh, _published, _report, _x


def _v0131_pair_set(bundle: LedgerBundle,
                    cash_movements: Iterable[CashMovementFlow]) -> set[Currency]:
    """v0.1.31's trend pair set: the ledger's quote currencies only (PERF-F2's edge)."""
    return {bundle.instruments[sym].quote_ccy for sym in trend_symbols(bundle)}


def test_a_placeholder_never_outlives_the_condition_that_made_it(
        corpus_db: CorpusFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # every run below — the dirty one and the fresh one it is compared with — reads the old
    # pair set, the one under which a change dated after U could end a placeholder's cause
    monkeypatch.setattr(settle_worker, "trend_fx_currencies", _v0131_pair_set)
    db = corpus_db("unquoted_ccy_cash")
    prepare(db)
    assert start_run(db, 1) == "published"
    conn = open_db(db.path)
    try:
        assert st.first_placeholder_day(conn, generation(conn)) == date(2026, 3, 16)
    finally:
        conn.close()
    _x(db, "INSERT INTO instruments (symbol, market, quote_ccy, sector, name) "
           "VALUES ('MYQ', 'MY', 'MYR', 'Energy', 'probe')")
    _x(db, "INSERT INTO prices (instrument, market, as_of_date, close, source, fetched_at) "
           "VALUES ('MYQ', 'MY', '2026-05-04', '2.60', 'test', '2026-05-04T00:00:00')")
    _x(db, "INSERT INTO transactions (account_id, symbol, side, quantity, price, fees, tax, "
           "trade_date) VALUES ('moomoo_my', 'MYQ', 'BUY', '100', '2.50', '0', '0', "
           "'2026-05-04')")
    conn = open_db(db.path)
    try:
        assert st.store_state(conn, reporting=db.reporting).dirty_from == date(2026, 5, 4)
    finally:
        conn.close()
    assert start_run(db, 2, scope="dirty", reason="prompt") == "published"
    report = _report(db, 2)
    assert report.range.from_ == date(2026, 3, 16)  # widened to the first placeholder
    assert any("2026-03-16" in line for line in report.summary_lines)
    conn = open_db(db.path)
    try:
        assert st.first_placeholder_day(conn, generation(conn)) is None
        history = report.history
        assert history is not None and history.none_to_value > 0
    finally:
        conn.close()
    assert _published(db) == _fresh(db, tmp_path)  # every settle table, byte for byte


def test_a_placeholder_is_never_a_figure_in_the_report(corpus_db: CorpusFactory) -> None:
    db = corpus_db("demo")
    prepare(db)
    assert start_run(db, 1) == "published"
    conn = open_db(db.path)
    try:
        row = conn.execute("SELECT t.account_id, t.symbol, t.price FROM transactions t JOIN "
                           "instruments i ON i.symbol = t.symbol WHERE i.quote_ccy = 'USD' AND "
                           "t.side = 'BUY' ORDER BY t.id LIMIT 1").fetchone()
    finally:
        conn.close()
    # before the first USD/TWD rate: the series is unavailable from its very first day
    _x(db, "INSERT INTO transactions (account_id, symbol, side, quantity, price, fees, tax, "
           "trade_date) VALUES (?, ?, 'BUY', '1', ?, '0', '0', '2025-12-15')",
       (row[0], row[1], row[2]))
    assert start_run(db, 2, scope="dirty", reason="prompt") == "published"
    conn = open_db(db.path)
    try:
        g = generation(conn)
        days = st.read_days(conn, g)
        assert days and all(d.placeholder for d in days)
        kinds = Counter((r.field, r.kind) for r in st.diff_page(conn, 2, limit=5000).items)
        assert set(kinds) == {("total_value", "value_to_none"),
                              ("net_invested", "value_to_none"),
                              ("net_worth", "value_to_none")}, kinds
        assert all(r.after is None for r in st.diff_page(conn, 2, limit=5000).items)
        closes = st.read_periods(conn, g, kind="M")
        assert closes
        for c in closes:
            assert c.kpi.trend_total_value is None and c.kpi.notes.trend_unavailable
            complete = (c.kpi.total_market_value is not None
                        and c.kpi.total_return is not None and not c.kpi.notes.missing_prices
                        and not c.kpi.notes.missing_fx and not c.kpi.notes.pending_positions)
            assert c.kpi.notes.incomplete == (not complete), c.period
        assert any(not c.kpi.notes.incomplete for c in closes)  # complete KPIs stay complete
    finally:
        conn.close()
    report = _report(db, 2)
    assert report.history is not None
    assert report.history.new_days == 0 and report.history.dropped_days == 0
    assert report.history.value_to_none > 0
    # a day settled without a figure is said so, never counted as a valued day
    assert "新結算 22 天（沒有走勢資料）" in report.summary_lines, report.summary_lines
    assert all("沒有走勢資料" in line for line in report.summary_lines
               if line.startswith("新結算"))


def test_a_ledger_unavailable_since_u_settles_one_day_a_night(corpus_db: CorpusFactory) -> None:
    """The fold verification's finding A: on ``unquoted_ccy_cash`` with its rebate's rate
    dropped (``perf_f2.drop_rebate_rate``) the series stays unavailable from its MYR rebate
    (2026-03-16). A placeholder stands while its cause does (this run's U equals the stored
    first placeholder), so each nightly run settles exactly its new day — never a re-run from
    U."""
    db = corpus_db("unquoted_ccy_cash")
    perf_f2.drop_rebate_rate(db.path)
    prepare(db)
    y = yesterday(db)
    assert start_run(db, 1, through=y - timedelta(days=3)) == "published"
    for n, back in ((2, 2), (3, 1)):
        day = y - timedelta(days=back)
        assert start_run(db, n, scope="nightly", reason="nightly", through=day) == "published"
        report = _report(db, n)
        assert (report.range.from_, report.range.to, report.range.days) == (day, day, 1)
        assert any(line.startswith("新結算 1 天") for line in report.summary_lines), \
            report.summary_lines
        assert not any("重新估值" in line for line in report.summary_lines)
        step = next(s for s in report.steps if s.key == "valuate")
        assert step.done == 1
    conn = open_db(db.path)
    try:
        assert st.first_placeholder_day(conn, generation(conn)) == date(2026, 3, 16)
    finally:
        conn.close()
