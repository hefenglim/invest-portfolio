"""Month and year closes served (PERF spec §11.4, §14.6, §18 "Read path" / "Period closes";
plan §7.1, package G): ``GET /api/snapshots`` and ``GET /api/settlement/periods*``.

Stores are built by the REAL worker (inline) on the reference corpora. The oracles:

- v0.1.31's truncated-world dashboards (``tests/reference/payloads/<corpus>/month_end``) for
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
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import count
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from portfolio_dash.api.deps import get_conn, get_now, get_reporting
from portfolio_dash.data_ingestion.store import list_cash_movements, list_fx_conversions
from portfolio_dash.data_ingestion.store import load_ledger_bundle as load_bundle
from portfolio_dash.portfolio import settle_worker
from portfolio_dash.portfolio import settlement_store as st
from portfolio_dash.portfolio.settlement_models import PeriodHolding, PeriodKpi
from portfolio_dash.shared.models.enums import DividendType
from portfolio_dash.shared.models.ledger import valued_rows_as_of
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
         params: dict[str, Any] | None = None) -> Any:
    conn = open_db(db.path)
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
            if name == "unquoted_ccy_cash":
                # PERF-F2 (deferred, owner 2026-10-19): the LIVE series is unavailable, so a
                # live close shows no trend figure — the trend card shows none either — while
                # the truncated world of Jan / Feb has one. The KPIs are the world's.
                ref = {**ref, "trend": {**ref["trend"], "points": []}}
            detail = _get(app, db, f"/api/settlement/periods/M/{d:%Y-%m}")
            holdings = [PeriodHolding.model_validate(h) for h in detail["holdings"]]
            assert_close_is_truncated_world(f"{name} {d}", _kpi(row), ref, d, conn=conn,
                                            holdings=holdings)
            compared += 1
        assert compared == len(manifest(name)["month_ends"])
    finally:
        conn.close()


@pytest.mark.parametrize("name", [n for n in CORPUS_NAMES if n != "unquoted_ccy_cash"])
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
        app: FastAPI, settled: Settled, monkeypatch: pytest.MonkeyPatch) -> None:
    """PERF-F2's shape, served live: no trend figure and no 當期報酬, in the build's own words
    (帳本中有交易日期缺少匯率資料) — never a figure, never an empty reason."""
    db = settled("unquoted_ccy_cash")
    monkeypatch.setenv("PD_SETTLE", "off")
    for row in _periods(app, db, "M"):
        kpi = row["kpi"]
        assert kpi["trend_total_value"] is None and kpi["period_twr"] is None
        assert kpi["notes"]["trend_unavailable"] and kpi["period_twr_reason"]


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
    assert st.dividends_receivable([on], bundle.instruments, dv.date - ONE, refused=[on]) == []


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


def _quote_jobs(db: CorpusDB, *, partial_on: date, first: date, last: date) -> None:
    """quotes_tw alone, at 14:00 Taipei every weekday of ``[first, last]``: ``ok``, but
    ``partial`` (2330 failed) on *partial_on*."""
    conn = open_db(db.path)
    try:
        for job_id, cron, tz, on in (("quotes_tw", "0 14 * * mon-fri", "Asia/Taipei", 1),
                                     ("quotes_us", "30 16 * * mon-fri", "America/New_York", 0),
                                     ("quotes_my", "30 17 * * mon-fri", "Asia/Kuala_Lumpur", 0)):
            conn.execute("INSERT OR REPLACE INTO schedule_config (job_id, enabled, cron, "
                         "timezone) VALUES (?, ?, ?, ?)", (job_id, on, cron, tz))
        day = first
        while day <= last:
            if day.weekday() < 5:
                at = datetime(day.year, day.month, day.day, 14, 0, 2, tzinfo=TPE)
                status, held = ("partial", ["2330"]) if day == partial_on else ("ok", [])
                conn.execute(
                    "INSERT INTO job_runs (job_id, started_at, finished_at, status, detail, "
                    "results) VALUES ('quotes_tw', ?, ?, ?, 'd', ?)",
                    (at.isoformat(), (at + timedelta(minutes=1)).isoformat(), status,
                     json.dumps({"held_failed": held})))
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
