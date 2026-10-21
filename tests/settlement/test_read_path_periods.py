"""Month and year closes served (PERF spec §11.4, §14.6, §18 "Read path" / "Period closes";
plan §7.1, package G): ``GET /api/snapshots`` and ``GET /api/settlement/periods*``.

Stores are built by the REAL worker (inline) on the reference corpora. The oracles:

- v0.1.31's truncated-world dashboards (``tests/reference/payloads/<corpus>/month_end``, with
  PERF-F2's intended difference on ``unquoted_ccy_cash``, ``tests/reference/perf_f2.py``) for
  every close the read path computes LIVE — ``PD_SETTLE=off``, every past month live;
- the worker's own published closes for the same periods: a close served live and the close
  the worker stores for it are the same close, field for field (only ``source`` differs), so
  a month never changes its figures when the nightly run settles it.

And the read-time rules: a correction dated last month changes last month's row at once; 暫定
while the month-end quote job is ``partial``; 待結算 (``period_twr`` ``None``) across a stale
join; 更新前版本 and no ``dividends_receivable`` on a stale row; the restatements of a close.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import count
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from portfolio_dash.api import snapshots
from portfolio_dash.api.deps import get_conn, get_now, get_reporting
from portfolio_dash.data_ingestion.store import list_cash_movements, list_fx_conversions
from portfolio_dash.data_ingestion.store import load_ledger_bundle as load_bundle
from portfolio_dash.portfolio import settle_worker
from portfolio_dash.portfolio import settlement_store as st
from portfolio_dash.portfolio.cash import UNBOOKED_DIVIDEND_NOTE
from portfolio_dash.portfolio.dashboard import build_dashboard_keyed
from portfolio_dash.portfolio.returns import DividendRefusals, dividend_key
from portfolio_dash.portfolio.settlement_models import PeriodHolding, PeriodKpi
from portfolio_dash.shared import result_cache
from portfolio_dash.shared.config import get_settings
from portfolio_dash.shared.models.enums import DividendType
from portfolio_dash.shared.models.ledger import valued_rows_as_of
from tests.reference import perf_f2
from tests.reference.corpora import (
    CORPORA,
    CORPUS_NAMES,
    TEMPLATE_DIR_NAME,
    CorpusDB,
    copy_corpus,
    copy_db,
    open_db,
)
from tests.reference.payload_gen import new_app
from tests.settlement.conftest import CorpusFactory
from tests.settlement.test_worker import (
    assert_close_is_truncated_world,
    manifest,
    prepare,
    reference,
    start_run,
)

pytestmark = pytest.mark.enable_socket   # TestClient's anyio portal (a Windows socketpair)

ONE = timedelta(days=1)
TPE = ZoneInfo("Asia/Taipei")

Settled = Callable[[str], CorpusDB]


@pytest.fixture(scope="module")
def app() -> FastAPI:
    return new_app()


@pytest.fixture(scope="module")
def settled(tmp_path_factory: pytest.TempPathFactory) -> Settled:
    """``settled(name)``: a private copy of corpus *name* with a store the real worker
    published (a full run through yesterday)."""
    templates = tmp_path_factory.getbasetemp() / TEMPLATE_DIR_NAME
    work = tmp_path_factory.mktemp("read_path_periods")
    published: dict[str, Path] = {}
    serial = count(1)

    def make(name: str) -> CorpusDB:
        if name not in published:
            db = copy_corpus(name, templates, work / f"{name}-settled.db")
            prepare(db)
            assert start_run(db, 1) == "published", name
            published[name] = db.path
        corpus = CORPORA[name]
        return CorpusDB(copy_db(published[name], work / f"{name}-{next(serial)}.db"),
                        corpus.now, corpus.reporting)

    return make


def _get(app: FastAPI, db: CorpusDB, url: str, *, now: datetime | None = None,
         params: dict[str, Any] | None = None,
         trace: Callable[[str], None] | None = None) -> Any:
    conn = open_db(db.path)
    conn.set_trace_callback(trace)
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_now] = lambda: now or db.now
    app.dependency_overrides[get_reporting] = lambda: db.reporting
    try:
        resp = TestClient(app).get(url, params=params)
    finally:
        app.dependency_overrides.clear()
        conn.close()
    assert resp.status_code == 200, resp.text
    return resp.json()


def _x(db: CorpusDB, sql: str, params: tuple[Any, ...] = ()) -> None:
    conn = open_db(db.path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _periods(app: FastAPI, db: CorpusDB, kind: str, **kw: Any) -> list[dict[str, Any]]:
    body = _get(app, db, "/api/settlement/periods", params={"kind": kind, "limit": 120}, **kw)
    rows: list[dict[str, Any]] = body["rows"]
    assert body["total_count"] == len(rows)
    return rows


def _without(row: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k not in keys}


# ---------------------------------------------------------------------------------------------
# The ledger's first row: the worker's own rule
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", CORPUS_NAMES)
def test_the_first_period_is_the_workers(corpus_db: CorpusFactory, name: str) -> None:
    db = corpus_db(name)
    conn = open_db(db.path)
    try:
        raw = load_bundle(conn)
        registered = raw.without_unregistered() if raw.unregistered_symbols else raw
        moves, convs = list_cash_movements(conn), list_fx_conversions(conn)
    finally:
        conn.close()
    for through in (db.now.date(), db.now.date() - timedelta(days=40)):
        cut = registered.valued_as_of(through)
        mine = st.ledger_first_row(cut, moves, convs, through)
        theirs = settle_worker._first_row(cut, valued_rows_as_of(moves, through), convs,
                                          through)
        assert mine == theirs, (name, through)


# ---------------------------------------------------------------------------------------------
# A live close ≡ v0.1.31's truncated world, and ≡ the close the worker stores
# ---------------------------------------------------------------------------------------------


def _kpi(row: dict[str, Any]) -> PeriodKpi:
    return PeriodKpi.model_validate(row["kpi"])


@pytest.mark.parametrize("name", CORPUS_NAMES)
def test_every_live_month_is_the_v0131_truncated_world(
        app: FastAPI, settled: Settled, name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    db = settled(name)
    monkeypatch.setenv("PD_SETTLE", "off")
    rows = {r["period"]: r for r in _periods(app, db, "M")}
    assert all(r["source"] == "live" for r in rows.values())
    conn = open_db(db.path)
    try:
        compared = 0
        for text in manifest(name)["month_ends"]:
            d = date.fromisoformat(text)
            row = rows[f"{d:%Y-%m}"]
            assert row["end_day"] == text
            ref = reference(name, f"month_end/{d:%Y-%m}.json")
            detail = _get(app, db, f"/api/settlement/periods/M/{d:%Y-%m}")
            holdings = [PeriodHolding.model_validate(h) for h in detail["holdings"]]
            assert_close_is_truncated_world(f"{name} {d}", _kpi(row), ref, d, conn=conn,
                                            holdings=holdings)
            compared += 1
        assert compared == len(manifest(name)["month_ends"])
    finally:
        conn.close()


@pytest.mark.parametrize("name", CORPUS_NAMES)
@pytest.mark.parametrize("kind", ["M", "Y"])
def test_a_close_served_live_is_the_close_the_worker_stores(
        app: FastAPI, settled: Settled, name: str, kind: str,
        monkeypatch: pytest.MonkeyPatch) -> None:
    db = settled(name)
    stored = _periods(app, db, kind)
    detail_stored = {r["period"]: _get(app, db, f"/api/settlement/periods/{kind}/{r['period']}")
                     for r in stored}
    with monkeypatch.context() as m:
        m.setenv("PD_SETTLE", "off")
        live = _periods(app, db, kind)
        detail_live = {r["period"]: _get(app, db,
                                         f"/api/settlement/periods/{kind}/{r['period']}")
                       for r in live}
    assert [r["period"] for r in stored] == [r["period"] for r in live]
    past = [r for r in stored if r["source"] == "store"]
    assert len(past) == len(stored) - 1, "every past period comes from the store"
    for s, lv in zip(stored, live, strict=True):
        assert _without(s, "source") == _without(lv, "source"), (name, kind, s["period"])
        a, b = detail_stored[s["period"]], detail_live[s["period"]]
        assert _without(a, "source") == _without(b, "source"), (name, kind, s["period"])


def test_the_unavailable_live_series_is_named_on_a_live_close(
        app: FastAPI, corpus_db: CorpusFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """A live series unavailable for a MISSING rate (``unquoted_ccy_cash`` with its rebate's
    rate dropped, ``perf_f2.drop_rebate_rate``), served live: no trend figure and no 當期報酬,
    in the build's own words — the series' ONE sentence, naming the first flow with no rate
    and its pair (MINOR 4, owner ruling 2026-10-22) — never a figure, never an empty
    reason."""
    db = corpus_db("unquoted_ccy_cash")
    perf_f2.drop_rebate_rate(db.path)
    prepare(db)
    assert start_run(db, 1) == "published"
    monkeypatch.setenv("PD_SETTLE", "off")
    sentence = "2026-03-16 起缺少 MYR/TWD 匯率（當日或之前無匯率），走勢無法計算"
    for row in _periods(app, db, "M"):
        kpi = row["kpi"]
        assert kpi["trend_total_value"] is None and kpi["period_twr"] is None
        assert kpi["notes"]["trend_unavailable"] == sentence, row["period"]
        assert kpi["period_twr_reason"] == sentence, row["period"]


# ---------------------------------------------------------------------------------------------
# GET /api/snapshots: the month rows
# ---------------------------------------------------------------------------------------------


def test_the_snapshot_rows_are_the_month_closes(app: FastAPI, settled: Settled) -> None:
    db = settled("demo")
    body = _get(app, db, "/api/snapshots", params={"limit": 12})
    periods = _periods(app, db, "M")
    rows = body["rows"]
    assert body["total_count"] == len(periods)
    assert [r["month"] for r in rows] == [p["period"] for p in periods][:12]
    assert all(r["kind"] == "M" for r in rows)
    current = rows[0]
    assert current["month"] == f"{db.now:%Y-%m}"
    assert current["as_of"] == db.now.isoformat()
    for r, p in zip(rows, periods, strict=False):
        k = p["kpi"]
        assert (r["total_value"], r["total_return"], r["total_return_rate"], r["xirr"]) == (
            k["total_market_value"], k["total_return"], k["total_return_rate"], k["xirr"])
        assert r["by_currency"] == k["by_currency_value"]
        assert (r["xirr_reason"], r["period_twr"], r["period_twr_reason"]) == (
            k["xirr_reason"], k["period_twr"], k["period_twr_reason"])
        assert r["notes"] == k["notes"]
        assert r["provisional"] is None and r["stale"] is False and r["restated"] is None
        if r is not current:
            assert r["as_of"] == p["end_day"]


def test_the_current_month_is_the_kpi_band(app: FastAPI, settled: Settled) -> None:
    """本月即時值: the live row as at today carries the dashboard's KPIs."""
    db = settled("rich")
    dash = _get(app, db, "/api/dashboard")
    row = _get(app, db, "/api/snapshots", params={"limit": 1})["rows"][0]
    k = dash["kpis"]
    assert (row["total_value"], row["total_return"], row["total_return_rate"], row["xirr"]) == (
        k["total_market_value"], k["total_return"], k["total_return_rate"], k["xirr"])
    assert row["xirr_reason"] == dash["freshness"]["xirr_unavailable_reason"]
    # the live current period says so (its 當期報酬 is to date); every closed one does not
    rows = _get(app, db, "/api/snapshots", params={"limit": 12})["rows"]
    assert [r["current"] for r in rows] == [True] + [False] * (len(rows) - 1)
    years = _periods(app, db, "Y")
    assert [y["current"] for y in years] == [True] + [False] * (len(years) - 1)


def test_a_year_row_never_crowds_out_a_month(app: FastAPI, settled: Settled) -> None:
    db = settled("rich")   # 2025-10 .. 2026-06, a year end inside
    rows = _get(app, db, "/api/snapshots", params={"limit": 12})["rows"]
    assert [r["month"] for r in rows] == [
        "2026-06", "2026-05", "2026-04", "2026-03", "2026-02", "2026-01", "2025-12", "2025-11",
        "2025-10"]
    years = _periods(app, db, "Y")
    assert [y["period"] for y in years] == ["2026", "2025"]
    assert years[0]["source"] == "live" and years[1]["source"] == "store"


# ---------------------------------------------------------------------------------------------
# A correction dated last month is visible at once
# ---------------------------------------------------------------------------------------------


def test_a_correction_dated_last_month_changes_last_months_row_at_once(
        app: FastAPI, settled: Settled) -> None:
    db = settled("demo")
    today = db.now.date()
    last = (today.replace(day=1) - ONE)
    label = f"{last:%Y-%m}"
    before = {r["month"]: r for r in _get(app, db, "/api/snapshots")["rows"]}
    _x(db, "INSERT INTO transactions (account_id, symbol, side, quantity, price, fees, tax, "
           "trade_date) VALUES ('tw_broker', '2330', 'BUY', '1000', '600', '855', '0', ?)",
       ((last - timedelta(days=10)).isoformat(),))
    after = {r["month"]: r for r in _get(app, db, "/api/snapshots")["rows"]}
    assert after[label]["total_value"] != before[label]["total_value"]
    assert after[label]["stale"] is False
    month = next(r for r in _periods(app, db, "M") if r["period"] == label)
    assert month["source"] == "live"
    # the nightly run settles it: the stored close is the row already shown — now marked
    # 已重述, because the close the previous run published has moved (§14.6)
    assert start_run(db, 2, scope="dirty", reason="nightly") == "published"
    settled_row = next(r for r in _periods(app, db, "M") if r["period"] == label)
    assert settled_row["source"] == "store" and settled_row["restated"] is True
    assert _without(settled_row, "source", "restated", "restatements") == _without(
        month, "source", "restated", "restatements")


# ---------------------------------------------------------------------------------------------
# 待結算 across a stale join; 更新前版本; dividends_receivable hidden on a stale row
# ---------------------------------------------------------------------------------------------


def test_period_twr_is_pending_across_a_stale_join(
        app: FastAPI, settled: Settled, monkeypatch: pytest.MonkeyPatch) -> None:
    db = settled("rich")   # now 2026-06-10: W = 2026-05-01
    fresh = {r["period"]: r for r in _periods(app, db, "M")}
    _x(db, "INSERT INTO transactions (account_id, symbol, side, quantity, price, fees, tax, "
           "trade_date) VALUES ('tw_broker', '2330', 'BUY', '100', '600', '20', '0', "
           "'2026-03-22')")
    rows = {r["period"]: r for r in _periods(app, db, "M")}
    snaps = {r["month"]: r for r in _get(app, db, "/api/snapshots")["rows"]}
    # stale [03-22, 04-30], live from 05-01
    for period in ("2026-03", "2026-04"):   # stored, previous generation
        r = rows[period]
        assert r["source"] == "store" and r["stale"] is True, period
        assert r["kpi"]["notes"]["dividends_receivable"] is None, period
        assert snaps[period]["stale"] is True
    for period in ("2026-03", "2026-04", "2026-05"):   # overlaps the stale days / joins at 05-01
        r = rows[period]
        assert r["kpi"]["period_twr"] is None, period
        assert r["kpi"]["period_twr_reason"] == st.TWR_PENDING_ZH, period
        assert snaps[period]["period_twr"] is None
        assert snaps[period]["period_twr_reason"] == st.TWR_PENDING_ZH
    assert rows["2026-05"]["source"] == "live" and rows["2026-05"]["stale"] is False
    for period in ("2026-02", "2026-01", "2025-12"):   # before the change: untouched
        assert rows[period]["kpi"]["period_twr"] == fresh[period]["kpi"]["period_twr"], period
        assert rows[period]["stale"] is False
    assert any(fresh[p]["kpi"]["period_twr"] is not None for p in ("2026-03", "2026-04"))
    # the current month joins two live ends: its return is measured, and it is the truth's.
    # Not to the last digit: the TWR is a ratio of two values of ONE chain-linked index
    # (``I(D) / I(previous end)``, the worker's definition), and that index runs through the
    # stale days — a different magnitude rounds the 28th digit differently. An analysis
    # figure, shown as a percentage with two decimals; equal to 24 decimals.
    monkeypatch.setenv("PD_SETTLE", "off")
    truth = {r["period"]: r for r in _periods(app, db, "M")}
    june = rows["2026-06"]["kpi"]
    assert june["period_twr"] is not None and june["period_twr_reason"] is None
    drift = abs(Decimal(june["period_twr"]) - Decimal(truth["2026-06"]["kpi"]["period_twr"]))
    assert drift < Decimal("1e-24"), drift


def test_dividends_receivable_is_read_at_d(settled: Settled) -> None:
    """A non-STOCK dividend between its ex-date and its payment date at D is listed, a 配股
    never (it counts from its ex-date), a row without an ex-date never."""
    db = settled("golden")
    conn = open_db(db.path)
    try:
        bundle = load_bundle(conn)
    finally:
        conn.close()
    dv = bundle.dividends[0]
    ex = dv.date - timedelta(days=20)
    on = dv.model_copy(update={"ex_date": ex})
    got = st.dividends_receivable([on], bundle.instruments, dv.date - timedelta(days=1))
    assert [(g.symbol, g.ex_date, g.pay_date, g.net) for g in got] == [
        (on.symbol, ex, on.date, on.net)]
    assert st.dividends_receivable([on], bundle.instruments, dv.date) == []   # paid
    assert st.dividends_receivable([on], bundle.instruments, ex - ONE) == []
    stock = on.model_copy(update={"type": DividendType.STOCK})
    assert st.dividends_receivable([stock], bundle.instruments, dv.date - ONE) == []
    assert st.dividends_receivable([dv.model_copy(update={"ex_date": None})],
                                   bundle.instruments, dv.date - ONE) == []
    # X1: a payout the replay refuses is LISTED, marked, with the replay's sentence
    refusals = DividendRefusals(refused=(on,),
                                reasons={dividend_key(on): ("orphan", "沒有對應持倉的股利")})
    (marked,) = st.dividends_receivable([on], bundle.instruments, dv.date - ONE,
                                        refusals=refusals)
    assert (marked.booked, marked.unbooked_note, marked.unbooked_reason) == (
        False, UNBOOKED_DIVIDEND_NOTE, "沒有對應持倉的股利")
    (plain,) = st.dividends_receivable([on], bundle.instruments, dv.date - ONE,
                                       refusals=DividendRefusals())
    assert (plain.booked, plain.unbooked_note, plain.unbooked_reason) == (True, None, None)


def test_a_month_with_a_receivable_dividend_lists_it(app: FastAPI, settled: Settled) -> None:
    db = settled("golden")   # 2330's cash dividend is paid 2026-03-01
    _x(db, "UPDATE dividends SET ex_date = '2026-02-20' WHERE symbol = '2330'")
    rows = {r["month"]: r for r in _get(app, db, "/api/snapshots")["rows"]}
    feb = rows["2026-02"]["notes"]["dividends_receivable"]
    assert [(d["symbol"], d["ex_date"], d["pay_date"]) for d in feb] == [
        ("2330", "2026-02-20", "2026-03-01")]
    # the edit is stale from the payout (03-01) to the window: March is the previous
    # generation's, so today's receivable list is not set beside its KPIs
    assert rows["2026-03"]["stale"] is True
    assert rows["2026-03"]["notes"]["dividends_receivable"] is None
    assert start_run(db, 2, scope="dirty", reason="prompt") == "published"
    rows = {r["month"]: r for r in _get(app, db, "/api/snapshots")["rows"]}
    assert rows["2026-03"]["stale"] is False
    assert rows["2026-03"]["notes"]["dividends_receivable"] == []
    assert len(rows["2026-02"]["notes"]["dividends_receivable"]) == 1


# ---------------------------------------------------------------------------------------------
# 暫定 while the month-end quote job is partial
# ---------------------------------------------------------------------------------------------


_JOBS = {"quotes_tw": ("0 14 * * mon-fri", ZoneInfo("Asia/Taipei"), (14, 0), "2330"),
         "quotes_us": ("30 16 * * mon-fri", ZoneInfo("America/New_York"), (16, 30), "AAPL"),
         "quotes_my": ("30 17 * * mon-fri", ZoneInfo("Asia/Kuala_Lumpur"), (17, 30), "1155")}


def _quote_jobs(db: CorpusDB, *, partial_on: date, first: date, last: date,
                jobs: tuple[str, ...] = ("quotes_tw",),
                partial: frozenset[tuple[str, date]] | None = None) -> None:
    """The *jobs* (only these enabled), at their cron time every weekday of ``[first, last]``
    in their own timezone: ``ok``, but ``partial`` (the market's held symbol failed) on
    ``(quotes_tw, partial_on)`` or on every ``(job, day)`` of *partial*."""
    misses = partial if partial is not None else frozenset({("quotes_tw", partial_on)})
    conn = open_db(db.path)
    try:
        for job_id, (cron, tz, _at, _sym) in _JOBS.items():
            conn.execute("INSERT OR REPLACE INTO schedule_config (job_id, enabled, cron, "
                         "timezone) VALUES (?, ?, ?, ?)",
                         (job_id, int(job_id in jobs), cron, str(tz)))
        day = first
        while day <= last:
            if day.weekday() < 5:
                for job_id in jobs:
                    _cron, tz, (hour, minute), sym = _JOBS[job_id]
                    at = datetime(day.year, day.month, day.day, hour, minute, 2, tzinfo=tz)
                    status, held = (("partial", [sym]) if (job_id, day) in misses
                                    else ("ok", []))
                    conn.execute(
                        "INSERT INTO job_runs (job_id, started_at, finished_at, status, "
                        "detail, results) VALUES (?, ?, ?, ?, 'd', ?)",
                        (job_id, at.isoformat(), (at + timedelta(minutes=1)).isoformat(),
                         status, json.dumps({"held_failed": held})))
            day += ONE
        conn.commit()
    finally:
        conn.close()


def test_a_partial_month_end_quote_job_marks_the_month_provisional(
        app: FastAPI, corpus_db: CorpusFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    db = corpus_db("demo")
    month_end = date(2026, 7, 31)   # a Friday
    _quote_jobs(db, partial_on=month_end, first=date(2026, 7, 1), last=date(2026, 8, 7))
    prepare(db)
    # the nightly stops before the blocked day, as final_through caps it
    assert start_run(db, 1, through=month_end - ONE) == "published"
    monkeypatch.delenv("PD_DISABLE_SCHEDULER", raising=False)
    monday = datetime(2026, 8, 3, 10, 0, tzinfo=TPE)
    july = next(r for r in _get(app, db, "/api/snapshots", now=monday)["rows"]
                if r["month"] == "2026-07")
    assert july["provisional"] == "月底收盤價尚未確定：台股報價工作未完成"
    period = next(r for r in _periods(app, db, "M", now=monday) if r["period"] == "2026-07")
    assert period["provisional"] == july["provisional"] and period["source"] == "live"
    # five days on, the 4-day rule settles it: no longer 暫定, the close named unconfirmed
    later = datetime(2026, 8, 5, 10, 0, tzinfo=TPE)
    july = next(r for r in _get(app, db, "/api/snapshots", now=later)["rows"]
                if r["month"] == "2026-07")
    assert july["provisional"] is None
    assert [m["symbol"] for m in july["notes"]["unconfirmed_closes"]] == ["2330"]


def test_provisional_depends_on_the_prices_never_on_the_store(
        app: FastAPI, corpus_db: CorpusFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same 暫定 with the store off: it is a fact about the quote runs (§11.4)."""
    db = corpus_db("demo")
    month_end = date(2026, 7, 31)
    _quote_jobs(db, partial_on=month_end, first=date(2026, 7, 1), last=date(2026, 8, 7))
    monkeypatch.delenv("PD_DISABLE_SCHEDULER", raising=False)
    monkeypatch.setenv("PD_SETTLE", "off")
    monday = datetime(2026, 8, 3, 10, 0, tzinfo=TPE)
    rows = {r["month"]: r for r in _get(app, db, "/api/snapshots", now=monday)["rows"]}
    assert rows["2026-07"]["provisional"] == "月底收盤價尚未確定：台股報價工作未完成"
    assert rows["2026-06"]["provisional"] is None
    assert rows["2026-08"]["provisional"] is None   # the current month is live, never 暫定


def test_the_provisional_note_names_the_end_days_market(
        app: FastAPI, corpus_db: CorpusFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review G #6: 台股 partial on 07-30 and 美股 partial on 07-31 (its New York date) — July's
    close is the US one, so its 暫定 names 美股, never the market of the FIRST blocked day."""
    db = corpus_db("demo")
    _quote_jobs(db, partial_on=date(2026, 7, 30), first=date(2026, 7, 1),
                last=date(2026, 7, 31), jobs=("quotes_tw", "quotes_us"),
                partial=frozenset({("quotes_tw", date(2026, 7, 30)),
                                   ("quotes_us", date(2026, 7, 31))}))
    monkeypatch.delenv("PD_DISABLE_SCHEDULER", raising=False)
    monkeypatch.setenv("PD_SETTLE", "off")
    monday = datetime(2026, 8, 3, 10, 0, tzinfo=TPE)
    july = next(r for r in _get(app, db, "/api/snapshots", now=monday)["rows"]
                if r["month"] == "2026-07")
    assert july["provisional"] == "月底收盤價尚未確定：美股報價工作未完成"


def test_one_listing_reads_the_quote_runs_and_the_schedule_once(
        app: FastAPI, corpus_db: CorpusFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Post-v0.1.32 item 3: the 暫定 walk asks ``final_through`` once per day from the end day
    back to ``through`` — here three probes (07-31, 07-30, 07-29) after the first answer — and
    each answer read ``job_runs`` (its rows and the go-lives) and ``schedule_config`` again,
    on every listing request, cache hits included. One request now reads each once, and the
    label is the same."""
    db = corpus_db("demo")
    _quote_jobs(db, partial_on=date(2026, 7, 29), first=date(2026, 7, 1),
                last=date(2026, 7, 31))
    monkeypatch.delenv("PD_DISABLE_SCHEDULER", raising=False)
    monkeypatch.setenv("PD_SETTLE", "off")
    # the corpus is the app's database, so the closes are cached: a hit computes nothing and
    # the labelling is the only reader of the two tables
    monkeypatch.setenv("DB_PATH", str(db.path))
    get_settings.cache_clear()
    result_cache.reset()
    saturday = datetime(2026, 8, 1, 10, 0, tzinfo=TPE)
    expected = "月底收盤價尚未確定：台股報價工作未完成"
    try:
        miss = _get(app, db, "/api/snapshots", now=saturday)
        reads: Counter[str] = Counter()

        def trace(sql: str) -> None:
            for table in ("job_runs", "schedule_config"):
                if table in sql:
                    reads[table] += 1

        hit = _get(app, db, "/api/snapshots", now=saturday, trace=trace)
        assert result_cache.stats()["periods:M"].hits == 1
    finally:
        result_cache.reset()
        get_settings.cache_clear()
    assert hit == miss
    assert next(r for r in hit["rows"] if r["month"] == "2026-07")["provisional"] == expected
    assert reads == {"job_runs": 2, "schedule_config": 1}


# ---------------------------------------------------------------------------------------------
# Review G: the served live_from never lies after the store's; the boundary; refused payouts;
# a write between the cached build and the read transaction
# ---------------------------------------------------------------------------------------------


def _one_trade(db: CorpusDB, on: date) -> int:
    """Keep only the ledger's first 2330 trade, dated *on* (no dividend, no opening)."""
    conn = open_db(db.path)
    try:
        keep = int(conn.execute("SELECT id FROM transactions WHERE symbol = '2330' "
                                "ORDER BY trade_date, id LIMIT 1").fetchone()[0])
        conn.execute("DELETE FROM transactions WHERE id <> ?", (keep,))
        conn.execute("DELETE FROM dividends")
        conn.execute("DELETE FROM opening_inventory")
        conn.execute("UPDATE transactions SET trade_date = ? WHERE id = ?",
                     (on.isoformat(), keep))
        conn.commit()
    finally:
        conn.close()
    return keep


#: (corpus, the trade's settled date, its later date, the month that must be live) — the
#: series' first day moves AFTER the store's live_from: inside the window, then with W
#: between the two dates (the stale case).
_REDATED = {
    "golden": (date(2026, 5, 20), date(2026, 6, 5), "2026-05", ()),
    "demo": (date(2026, 7, 15), date(2026, 8, 5), "2026-07", ()),
    "demo-stale": (date(2026, 5, 15), date(2026, 8, 5), "2026-07", ("2026-05", "2026-06")),
}


@pytest.mark.parametrize("case", sorted(_REDATED))
def test_a_first_trade_re_dated_later_never_serves_the_old_close(
        app: FastAPI, corpus_db: CorpusFactory, case: str,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Review G #1: the first trade re-dated later than the store's live_from left no stored
    day to serve, and the served live_from became the NEW first day — so the closes between
    the two were read from the previous generation, holding the old trade, unmarked."""
    settled_on, moved_to, live_month, stale_months = _REDATED[case]
    db = corpus_db(case.split("-")[0])
    keep = _one_trade(db, settled_on)
    prepare(db)
    assert start_run(db, 1) == "published"
    _x(db, "UPDATE transactions SET trade_date = ? WHERE id = ?", (moved_to.isoformat(), keep))
    rows = {r["month"]: r for r in _get(app, db, "/api/snapshots")["rows"]}
    periods = {r["period"]: r for r in _periods(app, db, "M")}
    with monkeypatch.context() as m:
        m.setenv("PD_SETTLE", "off")
        truth = {r["month"]: r for r in _get(app, db, "/api/snapshots")["rows"]}
    assert periods[live_month]["source"] == "live"
    if stale_months:
        # while history is stale, a period holding the first live day reads 待結算 (§11.2),
        # whatever the TWR's own reason would be; the figures are the truth's
        assert rows[live_month]["period_twr"] is None
        assert rows[live_month]["period_twr_reason"] == st.TWR_PENDING_ZH
        assert _without(rows[live_month], "period_twr_reason") == _without(
            truth[live_month], "period_twr_reason"), live_month
    else:
        assert rows[live_month] == truth[live_month], live_month
    assert rows[live_month]["stale"] is False
    for month in stale_months:   # before W: the previous generation's — and SAID so
        assert periods[month]["source"] == "store" and rows[month]["stale"] is True, month


def test_a_correction_dated_on_a_month_end_changes_that_month_at_once(
        app: FastAPI, settled: Settled, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review G #3, the ``end < live_from`` boundary: a correction dated ON 07-31 makes
    ``live_from`` 07-31, so July (ending that day) is live — never the stored close."""
    db = settled("demo")
    before = {r["month"]: r for r in _get(app, db, "/api/snapshots")["rows"]}
    _x(db, "INSERT INTO transactions (account_id, symbol, side, quantity, price, fees, tax, "
           "trade_date) VALUES ('tw_broker', '2330', 'BUY', '1000', '600', '855', '0', "
           "'2026-07-31')")
    rows = {r["month"]: r for r in _get(app, db, "/api/snapshots")["rows"]}
    with monkeypatch.context() as m:
        m.setenv("PD_SETTLE", "off")
        truth = {r["month"]: r for r in _get(app, db, "/api/snapshots")["rows"]}
    assert rows["2026-07"]["total_value"] != before["2026-07"]["total_value"]
    assert rows["2026-07"] == truth["2026-07"]
    assert rows["2026-07"]["stale"] is False
    assert next(r for r in _periods(app, db, "M") if r["period"] == "2026-07")[
        "source"] == "live"
    assert next(r for r in _periods(app, db, "M") if r["period"] == "2026-06")[
        "source"] == "store"


@pytest.mark.parametrize("settle", ["on", "off"])
def test_a_refused_receivable_payout_is_listed_marked(
        app: FastAPI, settled: Settled, settle: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review G #5 / X1: an AAPL payout in an account that never held AAPL (refused, no
    position) between its ex-date and its payment date is LISTED at February's end, marked
    「未入帳（待釐清）」 with the replay's own sentence — never dropped."""
    db = settled("golden")
    _x(db, "INSERT INTO dividends (account_id, symbol, date, ex_date, type, gross, "
           "withholding, net) VALUES ('tw_broker', 'AAPL', '2026-03-05', '2026-02-20', "
           "'CASH', '100', '0', '100')")
    _x(db, "UPDATE dividends SET ex_date = '2026-02-20' WHERE symbol = '2330'")
    monkeypatch.setenv("PD_SETTLE", settle)
    feb = next(r for r in _get(app, db, "/api/snapshots")["rows"]
               if r["month"] == "2026-02")
    listed = {d["symbol"]: d for d in feb["notes"]["dividends_receivable"]}
    assert set(listed) == {"2330", "AAPL"}
    refused = listed["AAPL"]
    assert refused["booked"] is False
    assert refused["unbooked_note"] == UNBOOKED_DIVIDEND_NOTE
    assert refused["unbooked_reason"] and "AAPL" in refused["unbooked_reason"]
    assert listed["2330"]["booked"] is True and listed["2330"]["unbooked_reason"] is None


def test_a_write_between_the_cached_build_and_the_read_transaction_rebuilds(
        app: FastAPI, settled: Settled, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review G #8: a ledger write committed after the (cached) build and before the closes'
    read transaction. Under ``PD_SETTLE=off`` the store views are both ``None``; the
    connection's ``data_version`` moved, so the build is redone inside the transaction and
    the response is ONE state — today's trend point carries the trade the KPIs carry."""
    db = settled("rich")
    monkeypatch.setenv("PD_SETTLE", "off")
    real = build_dashboard_keyed
    injected: list[bool] = []

    def build_then_write(*a: Any, **kw: Any) -> Any:
        out = real(*a, **kw)
        if not injected:
            injected.append(True)
            _x(db, "INSERT INTO transactions (account_id, symbol, side, quantity, price, fees, "
                   "tax, trade_date) VALUES ('tw_broker', '2330', 'BUY', '1000', '600', '855', "
                   "'0', ?)", (db.now.date().isoformat(),))
        return out

    monkeypatch.setattr(snapshots, "build_dashboard_keyed", build_then_write)
    raced = _periods(app, db, "M")[0]
    monkeypatch.setattr(snapshots, "build_dashboard_keyed", real)
    truth = _periods(app, db, "M")[0]
    assert injected and raced["period"] == truth["period"] == f"{db.now:%Y-%m}"
    assert raced == truth


@pytest.fixture
def cached_app_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[CorpusDB], None]]:
    """Make a corpus copy the app's database, so the listings are cached (the result cache
    serves only that file)."""
    def use(db: CorpusDB) -> None:
        monkeypatch.setenv("DB_PATH", str(db.path))
        get_settings.cache_clear()
        result_cache.reset()

    yield use
    result_cache.reset()
    get_settings.cache_clear()


_TODAYS_BUY = ("INSERT INTO transactions (account_id, symbol, side, quantity, price, fees, tax, "
               "trade_date) VALUES ('tw_broker', '2330', 'BUY', '1000', '600', '855', '0', ?)")


def test_a_job_writing_during_a_listing_miss_leaves_the_listing_cached(
        app: FastAPI, settled: Settled, cached_app_db: Callable[[CorpusDB], None],
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Post-v0.1.32 item 4. The listing's key is taken before its computation, whose closes
    open their read snapshot only after the dashboard build — seconds on a miss on prod. A
    job committing in between is IN the closes (the build is redone inside the snapshot,
    review G #8), yet the entry was filed under the older key, so the next listing computed
    everything again (~7 s on prod). It is now filed under the state it was read from: the
    next listing is a hit, and the same answer."""
    db = settled("rich")
    cached_app_db(db)
    real = build_dashboard_keyed
    injected: list[bool] = []

    def build_then_a_job_writes(*a: Any, **kw: Any) -> Any:
        out = real(*a, **kw)
        if not injected:
            injected.append(True)
            _x(db, _TODAYS_BUY, (db.now.date().isoformat(),))
        return out

    monkeypatch.setattr(snapshots, "build_dashboard_keyed", build_then_a_job_writes)
    first = _get(app, db, "/api/snapshots")
    second = _get(app, db, "/api/snapshots")
    monkeypatch.setattr(snapshots, "build_dashboard_keyed", real)
    stats = result_cache.stats()["periods:M"]
    assert injected and (stats.misses, stats.hits) == (1, 1)
    assert second == first
    result_cache.reset()                               # the truth, computed afresh
    assert _get(app, db, "/api/snapshots") == first


def test_a_job_writing_after_the_closes_snapshot_opened_is_computed_again(
        app: FastAPI, settled: Settled, cached_app_db: Callable[[CorpusDB], None],
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The other side: a commit AFTER the closes' snapshot opened is not in them, so the
    entry stays under the key taken before — the next listing computes the new state."""
    db = settled("rich")
    cached_app_db(db)
    monkeypatch.setenv("PD_SETTLE", "off")             # every close live, so _live_closes runs
    real = snapshots._live_closes
    injected: list[bool] = []

    def closes_then_a_job_writes(*a: Any, **kw: Any) -> Any:
        out = real(*a, **kw)
        if not injected:
            injected.append(True)
            _x(db, _TODAYS_BUY, (db.now.date().isoformat(),))
        return out

    monkeypatch.setattr(snapshots, "_live_closes", closes_then_a_job_writes)
    before = _get(app, db, "/api/snapshots")["rows"][0]
    after = _get(app, db, "/api/snapshots")["rows"][0]
    stats = result_cache.stats()["periods:M"]
    assert injected and (stats.misses, stats.hits) == (2, 0)
    assert before["month"] == after["month"] == f"{db.now:%Y-%m}"
    assert before["total_value"] != after["total_value"]   # the buy is in the second only


# ---------------------------------------------------------------------------------------------
# Restated closes (§14.6)
# ---------------------------------------------------------------------------------------------


def test_a_restated_close_carries_its_restatements(app: FastAPI, settled: Settled) -> None:
    db = settled("rich")
    conn = open_db(db.path)
    try:
        conn.execute("UPDATE prices SET close = '700', close_raw = '700' WHERE "
                     "instrument = '2330' AND as_of_date = '2026-01-30'")
        conn.commit()
    finally:
        conn.close()
    assert start_run(db, 2, scope="dirty", reason="prompt") == "published"
    conn = open_db(db.path)
    try:
        written = st.restatements_for(conn, "M", "2026-01")
    finally:
        conn.close()
    assert written, "the restated close wrote no restatement"
    row = next(r for r in _get(app, db, "/api/snapshots")["rows"] if r["month"] == "2026-01")
    assert row["restated"]["cutover"] is False
    assert [x["id"] for x in row["restated"]["rows"]] == [w.id for w in written]
    first = row["restated"]["rows"][0]
    assert first["reason"] and first["label"] and first["unit"]
    assert {"old_value", "new_value", "at", "field"} <= set(first)
    period = next(r for r in _periods(app, db, "M") if r["period"] == "2026-01")
    assert period["restated"] is True
    assert [x["id"] for x in period["restatements"]] == [w.id for w in written]


# ---------------------------------------------------------------------------------------------
# The detail route
# ---------------------------------------------------------------------------------------------


def test_the_detail_route_answers_400_and_404(app: FastAPI, settled: Settled) -> None:
    db = settled("golden")
    conn = open_db(db.path)
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_now] = lambda: db.now
    app.dependency_overrides[get_reporting] = lambda: db.reporting
    try:
        client = TestClient(app)
        assert client.get("/api/settlement/periods/M/2026-13").status_code == 400
        assert client.get("/api/settlement/periods/Q/2026").status_code == 400
        missing = client.get("/api/settlement/periods/M/2025-12")   # before the first row
        assert missing.status_code == 404 and "2025-12" in missing.json()["error"]["message"]
        assert client.get("/api/settlement/periods/M/2026-07").status_code == 404   # future
        assert client.get("/api/settlement/periods", params={"kind": "W"}).status_code == 400
        assert client.get("/api/settlement/periods", params={"limit": 0}).status_code == 400
        current = client.get("/api/settlement/periods/M/2026-06").json()
        assert current["source"] == "live" and current["end_day"] == "2026-06-11"
        assert {h["symbol"] for h in current["holdings"]} == {"2330", "AAPL"}
        assert current["cash"] is not None
    finally:
        app.dependency_overrides.clear()
        conn.close()


def test_the_listing_never_writes(app: FastAPI, settled: Settled) -> None:
    """A GET never writes (DEF-065): the closes read inside one read transaction."""
    db = settled("rich")
    conn = open_db(db.path)
    trace: list[str] = []
    conn.set_trace_callback(trace.append)
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_now] = lambda: db.now
    app.dependency_overrides[get_reporting] = lambda: db.reporting
    try:
        client = TestClient(app)
        for url in ("/api/snapshots", "/api/settlement/periods?kind=Y",
                    "/api/settlement/periods/M/2026-02"):
            assert client.get(url).status_code == 200, url
    finally:
        app.dependency_overrides.clear()
        conn.set_trace_callback(None)
        conn.close()
    writes = [s for s in trace if s.split(None, 1)[0].upper() in (
        "INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "REPLACE")]
    assert not writes, writes[:3]
