"""The settlement worker against the v0.1.31 references (PERF plan §6.1 "Tests" — run against
the references, the cross-check at D; spec §18 "Store", "Period closes").

A full run on every corpus publishes:

- one settled day per day of ``[first day, through]``, each the v0.1.31 trend point of that day
  (``tests/reference/payloads/<corpus>/dashboard.json``, written by v0.1.31's own code) — on
  ``unquoted_ccy_cash`` (PERF-F2: v0.1.31 shows no trend) the days before the unconvertible
  flow are the truncated-world points of the month-end references and the rest placeholders;
- one close per month end and year end, whose KPI block and notes are the v0.1.31 "truncated
  world" dashboard at that month end (``month_end/<YYYY-MM>.json``) and whose trend figures are
  that world's point D.

The helpers here (``prepare``, ``start_run`` …) are shared by the other worker test modules.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from portfolio_dash.portfolio import settle_worker
from portfolio_dash.portfolio import settlement_store as st
from portfolio_dash.portfolio.settlement_models import RunRequest, RunScope, RunStatus
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.fx import convert
from portfolio_dash.shared.wire import to_wire
from tests.reference.corpora import CORPUS_NAMES, CorpusDB, open_db
from tests.settlement.conftest import CorpusFactory

PAYLOADS = Path(__file__).resolve().parents[1] / "reference" / "payloads"
STARTED = datetime(2026, 10, 18, 22, 0, tzinfo=UTC)
ONE = timedelta(days=1)


# ---------------------------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------------------------


def manifest(name: str) -> dict[str, Any]:
    entry: dict[str, Any] = json.loads((PAYLOADS / "manifest.json").read_bytes())[
        "corpora"][name]
    return entry


def reference(name: str, member: str) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads((PAYLOADS / name / member).read_bytes())
    return doc


def yesterday(db: CorpusDB) -> date:
    return db.now.date() - ONE


def prepare(db: CorpusDB) -> None:
    """Install the trigger set on a corpus copy and record it as verified (what boot does)."""
    conn = open_db(db.path)
    try:
        st.install_or_verify(conn, calc_version_changed=False)
        st.record_schema_version(conn)
    finally:
        conn.close()


def start_run(db: CorpusDB, run_id: int, *, scope: RunScope = "full", reason: str = "button",
              through: date | None = None, previous: int | None = None,
              unconfirmed: dict[date, list[str]] | None = None,
              started_by: str | None = None) -> RunStatus | None:
    """What the service does — the ``preparing`` report with its ``RunRequest`` — then the
    worker, inline."""
    conn = open_db(db.path)
    try:
        st.create_run(conn, RunRequest(scope=scope, reason=reason, started_by=started_by,
                                       through=through or yesterday(db),
                                       unconfirmed_closes=unconfirmed or {}),
                      run_id=run_id, trigger_text="測試啟動", started_at=STARTED,
                      previous_run_id=previous)
    finally:
        conn.close()
    return settle_worker.run(run_id, db_path=db.path, reporting=db.reporting)


def wire_day(d: st.SettledDay) -> dict[str, Any]:
    return {"date": d.day.isoformat(), "total_value": to_wire(d.total_value),
            "net_invested": to_wire(d.net_invested), "incomplete": d.incomplete,
            "net_worth": to_wire(d.net_worth)}


def generation(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT generation FROM settle_meta").fetchone()[0])


def store_rows(conn: sqlite3.Connection, start: date, end: date) -> dict[str, dict[str, Any]]:
    """The published generation's rows in ``[start, end]`` (``generation_rows``)."""
    return st.generation_rows(conn, generation(conn), start=start, end=end)


_KPI_FIELDS = ("total_return", "total_return_rate", "xirr", "xirr_window_days")


def rate_oracle(conn: sqlite3.Connection, base: str, quote: str, day: date
                ) -> tuple[Decimal, date] | None:
    """*base* → *quote* on or before *day*, straight from ``fx_rates``: the direct pair, else
    ``1 /`` the inverse pair — an independent transcription of the app's rule."""
    if base == quote:
        return Decimal("1"), day
    for b, q, invert in ((base, quote, False), (quote, base, True)):
        row = conn.execute("SELECT rate, as_of_date FROM fx_rates WHERE base = ? AND quote = ? "
                           "AND as_of_date <= ? ORDER BY as_of_date DESC LIMIT 1",
                           (b, q, day.isoformat())).fetchone()
        if row is not None:
            rate = Decimal(row[0])
            return (Decimal("1") / rate if invert else rate), date.fromisoformat(row[1])
    return None


def kpi_pairs(ref: dict[str, Any]) -> set[str]:
    """The pairs the KPI block needs at D, from the reference itself: every currency of the
    returns and of a held position (stocks and contracts) to the reporting currency."""
    rep_ccy = ref["reporting_currency"]
    ccys: set[str] = set()
    if ref.get("returns"):
        ccys |= set(ref["returns"]["by_currency"])
    ccys |= {h["quote_ccy"] for h in ref["holdings"]}
    options = ref.get("options") or {}
    ccys |= set((options.get("market_value") or {}).get("by_currency") or {})
    return {f"{c}/{rep_ccy}" for c in ccys if c != rep_ccy}


def assert_close_is_truncated_world(label: str, kpi: Any, ref: dict[str, Any], d: date, *,
                                    conn: sqlite3.Connection | None = None,
                                    holdings: Sequence[Any] | None = None) -> None:
    """A stored close's KPI block, notes and positions ≡ v0.1.31's truncated-world dashboard
    at *d*; with *conn*, every rate it used ≡ the stored ``fx_rates`` row, by value."""
    k = ref["kpis"]
    assert to_wire(kpi.total_market_value) == k["total_market_value"], label
    for f in _KPI_FIELDS:
        assert to_wire(getattr(kpi, f)) == k[f], f"{label} {f}"
    assert kpi.xirr_reason == ref["freshness"]["xirr_unavailable_reason"], label
    view = ref["currency_view"]
    expected_view = {} if view is None else view["by_currency_value"]
    assert to_wire(kpi.by_currency_value) == expected_view, label
    fresh = ref["freshness"]
    assert kpi.notes.missing_prices == fresh["missing_prices"], label
    assert [(p.symbol, p.price_date.isoformat() if p.price_date else None)
            for p in kpi.notes.stale_prices] == [
        (p["symbol"], p["as_of"]) for p in fresh["prices"] if p["stale"] and p["as_of"]], label
    ref_fx = {f"{e['base']}/{e['quote']}": e for e in fresh["fx"]}
    used = {u.pair: u for u in kpi.fx_used}
    needed = kpi_pairs(ref)
    # exactly the pairs the KPIs need: used or missing, nothing else
    assert set(used) | set(kpi.notes.missing_fx) == needed, label
    for pair, u in used.items():
        assert u.as_of.isoformat() == ref_fx[pair]["as_of"], f"{label} {pair}"
        if conn is not None:
            base, quote = pair.split("/")
            oracle = rate_oracle(conn, base, quote, d)
            assert oracle is not None and u.rate == oracle[0], f"{label} {pair} rate"
    # missing: equal to the reference's, restricted to the pairs the KPIs read (the rest are
    # the 換匯損益 summary's, which a close leaves out — spec §11.2)
    assert kpi.notes.missing_fx == [p for p in fresh["missing_fx"] if p in needed], label
    assert [s.pair for s in kpi.notes.stale_fx] == [
        p for p, e in sorted(ref_fx.items()) if e["stale"] and e["as_of"] and p in used], label
    points = [p for p in ref["trend"]["points"] if p["date"] == d.isoformat()]
    if points:
        p = points[0]
        assert to_wire(kpi.trend_total_value) == p["total_value"], label
        assert to_wire(kpi.net_invested) == p["net_invested"], label
        assert to_wire(kpi.net_worth) == p["net_worth"], label
        assert kpi.notes.incomplete == p["incomplete"], label
        assert kpi.notes.trend_unavailable is None, label
    else:
        assert kpi.trend_total_value is None and kpi.net_invested is None, label
        assert kpi.net_worth is None and kpi.period_twr is None, label
        assert kpi.notes.trend_unavailable, label
        # an absent trend is not an incomplete KPI: the KPIs' own completeness
        kpi_incomplete = (k["total_market_value"] is None or k["total_return"] is None
                          or bool(fresh["missing_prices"]) or bool(kpi.notes.missing_fx)
                          or bool(kpi.notes.pending_positions))
        assert kpi.notes.incomplete == kpi_incomplete, label
    if holdings is not None:
        assert_positions_are_the_dashboards(label, holdings, ref)


def assert_positions_are_the_dashboards(label: str, holdings: Sequence[Any],
                                        ref: dict[str, Any]) -> None:
    """The close's positions ≡ the truncated world's holdings rows (stocks) and 選擇權 open rows
    (contracts): shares, both cost totals, the price and its date, value and unrealized."""
    stocks = {(h.account_id, h.symbol): h for h in holdings if h.asset_type == "stock"}
    rows = {(r["account_id"], r["symbol"]): r for r in ref["holdings"]}
    assert set(stocks) == set(rows), label
    for key, r in rows.items():
        h = stocks[key]
        got = {"shares": to_wire(h.shares), "original_cost_total": to_wire(h.original_cost_total),
               "adjusted_cost_total": to_wire(h.adjusted_cost_total),
               "market_price": to_wire(h.price), "price_as_of": to_wire(h.price_as_of),
               "market_value": to_wire(h.market_value), "unrealized_pnl": to_wire(h.unrealized)}
        assert got == {f: r[f] for f in got}, f"{label} {key}"
    contracts = {(h.account_id, h.symbol): h for h in holdings if h.asset_type == "option"}
    opened = {(r["account_id"], r["symbol"]): r
              for r in ((ref.get("options") or {}).get("open") or [])}
    assert set(contracts) == set(opened), label
    for key, r in opened.items():
        h = contracts[key]
        got = {"share_equivalents": to_wire(h.shares), "last_price": to_wire(h.price),
               "last_price_date": to_wire(h.price_as_of),
               "market_value": to_wire(h.market_value), "unrealized": to_wire(h.unrealized)}
        assert got == {f: r[f] for f in got}, f"{label} {key}"


# ---------------------------------------------------------------------------------------------
# The references
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", CORPUS_NAMES)
def test_a_full_run_settles_the_v0131_series_and_closes(corpus_db: CorpusFactory,
                                                        name: str) -> None:
    db = corpus_db(name)
    prepare(db)
    assert start_run(db, 1) == "published"
    through = yesterday(db)
    conn = open_db(db.path)
    try:
        state = st.store_state(conn, reporting=db.reporting)
        assert state.usable and state.settled_through == through
        assert state.first_day is not None
        days = st.read_days(conn, state.generation)
        assert [d.day for d in days] == [state.first_day + timedelta(days=i) for i in
                                         range((through - state.first_day).days + 1)]
        trend = reference(name, "dashboard.json")["trend"]
        by_day = {d.day: d for d in days}
        if trend["available"]:
            ref_points = [p for p in trend["points"] if p["date"] <= through.isoformat()]
            assert [wire_day(by_day[date.fromisoformat(p["date"])]) for p in ref_points] == \
                ref_points
            assert len(ref_points) == len(days)
            assert not any(settle_worker.is_placeholder(d) for d in days)
        # every month end: the close ≡ the truncated world (and its point D ≡ the settled day)
        ends = [date.fromisoformat(x) for x in manifest(name)["month_ends"]]
        compared = 0
        for d in ends:
            if d < state.first_day or d > through:
                continue
            ref = reference(name, f"month_end/{d:%Y-%m}.json")
            close = st.read_period(conn, state.generation, "M", f"{d:%Y-%m}")
            assert close is not None, f"{name} {d}: no close"
            assert_close_is_truncated_world(
                f"{name} {d}", close.kpi, ref, d, conn=conn,
                holdings=st.read_period_holdings(conn, state.generation, "M", f"{d:%Y-%m}"))
            points = [p for p in ref["trend"]["points"] if p["date"] == d.isoformat()]
            if points:
                assert wire_day(by_day[d]) == points[0], f"{name} {d}"
            else:
                assert settle_worker.is_placeholder(by_day[d]), f"{name} {d}"
            compared += 1
        assert compared, f"{name}: no month end compared"
    finally:
        conn.close()


def test_perf_f2_days_before_the_unconvertible_flow_keep_their_points(
        corpus_db: CorpusFactory) -> None:
    """``unquoted_ccy_cash``: v0.1.31 shows no trend at all (PERF-F2, confirmed, not fixed),
    but the truncated world of January and February has one — those days are settled with it,
    and from the 50 MYR rebate (2026-03-16) on every day is a placeholder."""
    db = corpus_db("unquoted_ccy_cash")
    prepare(db)
    assert start_run(db, 1) == "published"
    conn = open_db(db.path)
    try:
        days = {d.day: d for d in st.read_days(conn, generation(conn))}
        rebate = date(2026, 3, 16)
        assert not any(settle_worker.is_placeholder(d) for k, d in days.items() if k < rebate)
        assert all(settle_worker.is_placeholder(d) for k, d in days.items() if k >= rebate)
        march = st.read_period(conn, generation(conn), "M", "2026-03")
        assert march is not None and march.kpi.trend_total_value is None
        assert march.kpi.period_twr is None and march.kpi.period_twr_reason
        assert st.store_state(conn, reporting=db.reporting).bench_state is None
    finally:
        conn.close()


@pytest.mark.parametrize("name", CORPUS_NAMES)
def test_market_value_minus_options_is_the_trend_value_on_every_complete_day(
        corpus_db: CorpusFactory, name: str) -> None:
    """Spec §18 "Period closes": on a complete D, KPI 總市值 − the options' value ≡ the settled
    day's 證券市值 (both are the stock subtotal at the same prices and FX)."""
    db = corpus_db(name)
    prepare(db)
    assert start_run(db, 1) == "published"
    conn = open_db(db.path)
    try:
        g = generation(conn)
        checked = 0
        for close in st.read_periods(conn, g):
            k = close.kpi
            if k.notes.incomplete or k.total_market_value is None or k.trend_total_value is None:
                continue
            stock_value = k.total_market_value - _options_reporting(conn, g, close, db)
            assert stock_value == k.trend_total_value, (
                f"{name} {close.period}: {k.total_market_value} − options ≠ "
                f"{k.trend_total_value}")
            checked += 1
        # golden and its two variants hold an unpriced position at every month end
        if name in ("demo", "doubled_demo", "rich", "rich_refusals", "epoch1990"):
            assert checked, f"{name}: no complete close"
    finally:
        conn.close()


def _options_reporting(conn: sqlite3.Connection, g: int, close: st.SettledPeriod,
                       db: CorpusDB) -> Decimal:
    """The options' market value at D in the reporting currency, as the 選擇權 block forms it:
    summed per currency, then converted at the rate the close used for that currency."""
    rates = {u.pair: u.rate for u in close.kpi.fx_used}
    by_ccy: dict[Currency, Decimal] = {}
    for h in st.read_period_holdings(conn, g, close.kind, close.period):
        if h.asset_type == "option" and h.market_value is not None:
            by_ccy[h.quote_ccy] = by_ccy.get(h.quote_ccy, Decimal("0")) + h.market_value
    total = Decimal("0")
    for ccy in sorted(by_ccy, key=lambda c: c.value):
        rate = Decimal("1") if ccy == db.reporting else rates[f"{ccy.value}/{db.reporting.value}"]
        total += convert(by_ccy[ccy], rate)
    return total


def test_the_worker_writes_its_own_pid_and_the_end_into_the_progress_file(
        corpus_db: CorpusFactory) -> None:
    import os

    db = corpus_db("golden")
    prepare(db)
    assert start_run(db, 1) == "published"
    progress = json.loads((db.path.parent / settle_worker.PROGRESS_FILE).read_bytes())
    assert progress["run_id"] == 1 and progress["pid"] == os.getpid()
    assert progress["status"] == "published" and progress["percent"] == 100
    assert [p["key"] for p in progress["phases"]][:7] == [
        "load", "check", "replay", "valuate", "close", "bench", "swap"]
    assert all(p["status"] == "done" for p in progress["phases"][:7])
    assert progress["eta_basis"]


def test_an_empty_ledger_publishes_nothing(tmp_path: Path) -> None:
    from tests.conftest import init_golden_base

    path = tmp_path / "empty.db"
    conn = open_db(path)
    init_golden_base(conn)
    conn.commit()
    conn.close()
    db = CorpusDB(path, datetime(2026, 6, 11, 14, 30, tzinfo=UTC), Currency.TWD)
    prepare(db)
    assert start_run(db, 1) == "published"
    conn = open_db(path)
    try:
        state = st.store_state(conn, reporting=db.reporting)
        assert state.generation == 0 and state.settled_through is None
        report = st.get_run(conn, 1)
        assert report is not None
        assert "帳本尚無資料，無需結算" in report.summary_lines
        assert report.summary.status_text == "完成"
    finally:
        conn.close()
