"""The dashboard's read path over the settlement store (PERF spec §10, §18 "Read path"; plan
§7.1, package G).

Every store here is built by the REAL worker (``settle_worker.run``, inline) on the reference
corpora — never hand-written generations. The oracle is v0.1.31's own output: the reference
payloads (``tests/reference/payloads``, with PERF-F2's intended difference on
``unquoted_ccy_cash``, ``tests/reference/perf_f2.py``, and the served trend points' ``pnl``,
``tests/reference/trend_pnl.py`` — which is also what proves a day served from the STORE carries
it) and the frozen v0.1.31 engine (``tests/reference``).

- **clean** and **dirty inside the live window**: ``GET /api/dashboard`` is byte-identical to
  the v0.1.31 reference on every corpus (invariant 1), and the store was actually read;
- **stale** (a change dated before the window): the payload differs from a full live build
  only on the trend days ``[Dd, live_from)`` (and by the drawdown alert the stale history
  silences, §10 step 6);
- the benchmark is finished from the stored state only under the three conditions, and goes
  live after a flow, a pending range, a claimed range and an action on a benchmark key;
- the live part's bounded load at ``live_from`` keeps the carried-forward close on a weekend,
  a holiday and inside a suspension;
- a placeholder day, or a day the generation does not hold, is never served — it is live;
- ``PD_SETTLE=off`` and an unusable store serve every day live.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import count
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.portfolio import dashboard
from portfolio_dash.portfolio import settlement_store as st
from portfolio_dash.portfolio.dashboard import _build_dashboard
from portfolio_dash.portfolio.dashboard_models import TrendSeries, with_trend_pnl
from portfolio_dash.portfolio.settlement_models import RunRequest
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.reference import perf_f2, trend_pnl
from tests.reference import trend_v0131 as ref
from tests.reference.canon import canon, first_difference
from tests.reference.corpora import (
    CORPORA,
    CORPUS_NAMES,
    TAIPEI,
    TEMPLATE_DIR_NAME,
    CorpusDB,
    copy_corpus,
    copy_db,
    open_db,
)
from tests.reference.dashboard_inputs import load_inputs
from tests.reference.payload_gen import dashboard_body, new_app
from tests.settlement.conftest import CorpusFactory
from tests.settlement.test_worker import STARTED, prepare, start_run

pytestmark = pytest.mark.enable_socket   # TestClient's anyio portal (a Windows socketpair)

PAYLOADS = Path(__file__).resolve().parents[1] / "reference" / "payloads"
ONE = timedelta(days=1)

Settled = Callable[[str], CorpusDB]


@pytest.fixture(scope="module")
def app() -> FastAPI:
    return new_app()


@pytest.fixture(scope="module")
def settled(tmp_path_factory: pytest.TempPathFactory) -> Settled:
    """``settled(name)`` — a PRIVATE copy of corpus *name* with a store the real worker
    published (a full run through yesterday). One run per corpus per module, one file copy per
    call, so a test may write freely."""
    templates = tmp_path_factory.getbasetemp() / TEMPLATE_DIR_NAME
    work = tmp_path_factory.mktemp("read_path")
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


def _reference(name: str) -> bytes:
    return trend_pnl.expected_bytes(name, "dashboard.json")


def _body(app: FastAPI, db: CorpusDB) -> bytes:
    return dashboard_body(app, db.path, db.now, db.reporting)


def _live_body(app: FastAPI, db: CorpusDB, monkeypatch: pytest.MonkeyPatch) -> bytes:
    """The full live build of the same database and clock (``PD_SETTLE=off``)."""
    with monkeypatch.context() as m:
        m.setenv("PD_SETTLE", "off")
        return _body(app, db)


def _build(db: CorpusDB) -> Any:
    conn = open_db(db.path)
    try:
        return _build_dashboard(conn, now=db.now, reporting=db.reporting)
    finally:
        conn.close()


def _state(db: CorpusDB) -> st.StoreState:
    conn = open_db(db.path)
    try:
        return st.store_state(conn, reporting=db.reporting)
    finally:
        conn.close()


def _x(db: CorpusDB, sql: str, params: tuple[Any, ...] = ()) -> None:
    conn = open_db(db.path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _first_difference(expected: bytes, actual: bytes) -> str | None:
    a, b = json.loads(expected), json.loads(actual)
    return first_difference(canon(a), canon(b))


def _round_trip_close(db: CorpusDB, *, start: date, end: date) -> date:
    """Restate one stored close of a ledger symbol dated in ``[start, end]`` and put it back:
    the triggers mark the store dirty from that day (a price restatement), the data ends
    byte-identical. Returns the day."""
    conn = open_db(db.path)
    try:
        row = conn.execute(
            "SELECT instrument, as_of_date, close, close_raw FROM prices WHERE as_of_date "
            "BETWEEN ? AND ? AND instrument IN (SELECT symbol FROM transactions UNION "
            "SELECT symbol FROM opening_inventory) ORDER BY as_of_date, instrument LIMIT 1",
            (start.isoformat(), end.isoformat())).fetchone()
        assert row is not None, f"{db.path.name}: no close in [{start}, {end}]"
        sym, day, close, raw = row[0], row[1], row[2], row[3]
        conn.execute("UPDATE prices SET close = '1', close_raw = '1' WHERE instrument = ? "
                     "AND as_of_date = ?", (sym, day))
        conn.execute("UPDATE prices SET close = ?, close_raw = ? WHERE instrument = ? "
                     "AND as_of_date = ?", (close, raw, sym, day))
        conn.commit()
    finally:
        conn.close()
    return date.fromisoformat(day)


# ---------------------------------------------------------------------------------------------
# Byte identity: clean, and dirty inside the live window
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", CORPUS_NAMES)
def test_a_clean_store_serves_the_v0131_payload(app: FastAPI, settled: Settled,
                                                name: str) -> None:
    db = settled(name)
    data = _build(db)
    history = data.history_state
    today = db.now.date()
    # guard the guard: the store WAS read — every day before today from it
    assert history.generation == 1 and history.live_from == today, history
    assert history.stale_from is None
    if data.trend.available:
        assert data.trend.points[0].date < today
    body = _body(app, db)
    assert body == _reference(name), f"{name}: {_first_difference(_reference(name), body)}"


@pytest.mark.parametrize("name", CORPUS_NAMES)
def test_a_store_dirty_inside_the_window_serves_the_v0131_payload(
        app: FastAPI, settled: Settled, name: str) -> None:
    db = settled(name)
    today = db.now.date()
    window = st.window_start(today)
    day = _round_trip_close(db, start=window, end=today - ONE)
    state = _state(db)
    assert state.dirty_from == day and state.dirty_from >= window
    data = _build(db)
    assert data.history_state.live_from == day, data.history_state
    assert data.history_state.stale_from is None
    body = _body(app, db)
    assert body == _reference(name), f"{name}: {_first_difference(_reference(name), body)}"


@pytest.mark.parametrize("name", ["golden", "demo"])
def test_a_store_settled_some_days_ago_serves_the_v0131_payload(
        app: FastAPI, corpus_db: CorpusFactory, name: str) -> None:
    """A gap in the nightly runs: ``live_from = S + 1`` days before today, still exact."""
    db = corpus_db(name)
    prepare(db)
    through = db.now.date() - timedelta(days=9)
    assert start_run(db, 1, through=through) == "published"
    assert _build(db).history_state.live_from == through + ONE
    body = _body(app, db)
    assert body == _reference(name), f"{name}: {_first_difference(_reference(name), body)}"


# ---------------------------------------------------------------------------------------------
# The stale case
# ---------------------------------------------------------------------------------------------

#: A back-dated buy before the live window, per corpus: (account, symbol, price).
_BACKDATED = {
    "golden": ("tw_broker", "2330", "600"),
    "demo": ("tw_broker", "2330", "600"),
    "rich": ("tw_broker", "2330", "600"),
}


@pytest.mark.parametrize("name", sorted(_BACKDATED))
def test_the_stale_case_differs_only_on_the_stale_days(
        app: FastAPI, settled: Settled, name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    db = settled(name)
    today = db.now.date()
    window = st.window_start(today)
    day = window - timedelta(days=40)
    account, symbol, price = _BACKDATED[name]
    _x(db, "INSERT INTO transactions (account_id, symbol, side, quantity, price, fees, tax, "
           "trade_date) VALUES (?, ?, 'BUY', '100', ?, '20', '0', ?)",
       (account, symbol, price, day.isoformat()))
    state = _state(db)
    assert state.dirty_from == day
    first_live = st.live_from(state, today)
    assert first_live == window
    data = _build(db)
    history = data.history_state
    assert (history.stale_from, history.stale_to, history.live_from) == (
        day, window - ONE, window)

    stale = json.loads(_body(app, db))
    live = json.loads(_live_body(app, db, monkeypatch))
    assert list(stale) == list(live)
    for key in live:
        if key in ("trend", "alerts"):
            continue
        assert stale[key] == live[key], f"{name}: {key} differs"
    # the alerts: the same, but for the drawdown rule the stale history silences
    assert ([a for a in stale["alerts"] if a["rule"] != "portfolio_drawdown"]
            == [a for a in live["alerts"] if a["rule"] != "portfolio_drawdown"])
    assert not any(a["rule"] == "portfolio_drawdown" for a in stale["alerts"])
    s_trend, l_trend = stale["trend"], live["trend"]
    assert {k: v for k, v in s_trend.items() if k != "points"} == {
        k: v for k, v in l_trend.items() if k != "points"}
    assert [p["date"] for p in s_trend["points"]] == [p["date"] for p in l_trend["points"]]
    differing = [s["date"] for s, lv in zip(s_trend["points"], l_trend["points"], strict=True)
                 if s != lv]
    assert differing, f"{name}: the stale days show nothing of the old version"
    assert all(day.isoformat() <= d < first_live.isoformat() for d in differing), differing


# ---------------------------------------------------------------------------------------------
# The kill switch and an unusable store
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["golden", "rich"])
def test_settle_off_serves_every_day_live(app: FastAPI, settled: Settled, name: str,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    db = settled(name)
    # a stale store, so serving it would show: the switch must ignore it entirely
    today = db.now.date()
    _x(db, "INSERT INTO transactions (account_id, symbol, side, quantity, price, fees, tax, "
           "trade_date) VALUES ('tw_broker', '2330', 'BUY', '100', '600', '20', '0', ?)",
       ((st.window_start(today) - timedelta(days=40)).isoformat(),))
    monkeypatch.setenv("PD_SETTLE", "off")
    data = _build(db)
    assert data.history_state.live_from is None and not data.history_state.stale
    monkeypatch.undo()
    off = _live_body(app, db, monkeypatch)
    # a fresh live build of the same file, with the store dropped, is the oracle
    bare = copy_db(db.path, db.path.with_name(db.path.stem + "-bare.db"))
    conn = open_db(bare)
    try:
        st.drop_store(conn)
        conn.commit()
    finally:
        conn.close()
    assert off == _body(app, CorpusDB(bare, db.now, db.reporting))


@pytest.mark.parametrize("name", ["golden", "rich"])
def test_an_unusable_store_serves_every_day_live(
        app: FastAPI, settled: Settled, name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A calc-version mismatch (a release before its boot run finished): never stale, never
    missing — the v0.1.31 payload, computed live."""
    db = settled(name)
    monkeypatch.setattr(st, "CODE_DIGEST", "a-different-release")
    assert not _state(db).usable
    data = _build(db)
    assert data.history_state.live_from is None
    body = _body(app, db)
    assert body == _reference(name), f"{name}: {_first_difference(_reference(name), body)}"


# ---------------------------------------------------------------------------------------------
# The benchmark: stored state under the three conditions, else live
# ---------------------------------------------------------------------------------------------


class _Spy:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.stored = 0
        self.live = 0
        real_stored = dashboard._finish_stored
        real_live = dashboard._live_counterfactual

        def stored(*a: Any, **kw: Any) -> Any:
            self.stored += 1
            return real_stored(*a, **kw)

        def live(*a: Any, **kw: Any) -> Any:
            self.live += 1
            return real_live(*a, **kw)

        monkeypatch.setattr(dashboard, "_finish_stored", stored)
        monkeypatch.setattr(dashboard, "_live_counterfactual", live)


@pytest.mark.parametrize("name", ["rich", "epoch1990"])
def test_a_clean_store_finishes_the_stored_benchmark_state(
        app: FastAPI, settled: Settled, name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    db = settled(name)
    assert _state(db).bench_state is not None
    spy = _Spy(monkeypatch)
    body = _body(app, db)
    assert (spy.stored, spy.live) == (1, 0)
    assert body == _reference(name), f"{name}: {_first_difference(_reference(name), body)}"
    assert json.loads(body)["benchmark"]["available"]


def _flow_after_s(db: CorpusDB) -> None:
    _x(db, "INSERT INTO transactions (account_id, symbol, side, quantity, price, fees, tax, "
           "trade_date) VALUES ('tw_broker', '2330', 'BUY', '10', '600', '20', '0', ?)",
       (db.now.date().isoformat(),))


def _pending_range(db: CorpusDB) -> None:
    today = db.now.date()
    _round_trip_close(db, start=st.window_start(today), end=today - ONE)


def _claimed_range(db: CorpusDB) -> None:
    """A run that has claimed the dirt and not published yet (its first write, §9.4)."""
    _pending_range(db)
    conn = open_db(db.path)
    try:
        through = db.now.date() - ONE
        st.create_run(conn, RunRequest(scope="dirty", reason="prompt", through=through),
                      run_id=2, trigger_text="測試啟動", started_at=STARTED)
        st.claim(conn, 2, scope="dirty", run_through=through)
        state = st.store_state(conn, reporting=db.reporting)
        assert state.dirty_from is None and state.run_from is not None
    finally:
        conn.close()


def _benchmark_key_action(db: CorpusDB) -> None:
    """A SPLIT on ``0050`` — a benchmark storage key, registered here as a stock the owner
    could hold — dated after ``settled_through``."""
    conn = open_db(db.path)
    try:
        upsert_instrument(conn, Instrument(symbol="0050", market=Market.TW,
                                           quote_ccy=Currency.TWD, sector="ETF",
                                           name="Index ETF", board="TWSE", is_etf=True))
        conn.execute(
            "INSERT INTO corporate_actions (account_id, kind, from_symbol, to_symbol, date, "
            "ratio_from, ratio_to) VALUES ('tw_broker', 'SPLIT', '0050', '0050', ?, '1', '4')",
            (db.now.date().isoformat(),))
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("case", ["flow", "pending", "claimed", "action"])
def test_the_benchmark_goes_live(app: FastAPI, settled: Settled, case: str,
                                 monkeypatch: pytest.MonkeyPatch) -> None:
    db = settled("rich")
    {"flow": _flow_after_s, "pending": _pending_range, "claimed": _claimed_range,
     "action": _benchmark_key_action}[case](db)
    state = _state(db)
    if case in ("flow", "action"):
        assert state.dirty_day is None, case  # dated after S: only the third condition holds
    spy = _Spy(monkeypatch)
    body = json.loads(_body(app, db))
    assert (spy.stored, spy.live) == (0, 1), case
    monkeypatch.undo()
    live = json.loads(_live_body(app, db, monkeypatch))
    assert body["benchmark"] == live["benchmark"], case
    assert body == live, case   # every case here is clean or dirty inside the window


# ---------------------------------------------------------------------------------------------
# Bounded load at live_from: a weekend, a holiday, a suspension
# ---------------------------------------------------------------------------------------------

_NOW = datetime(2026, 6, 11, 14, 30, tzinfo=TAIPEI)


def _weekdays(start: date, end: date, *, skip: frozenset[date] = frozenset(),
              gap: tuple[date, date] | None = None) -> list[date]:
    out, day = [], start
    while day <= end:
        if (day.weekday() < 5 and day not in skip
                and not (gap is not None and gap[0] <= day <= gap[1])):
            out.append(day)
        day += ONE
    return out


#: (shape, the first live day, the days BND has a close). BND is bought on 2025-10-06.
_SHAPES = {
    # a Saturday; the last close is the Friday before
    "weekend": (date(2026, 1, 3), _weekdays(date(2025, 10, 1), date(2026, 6, 10))),
    # New Year's Day, a Thursday with no close; the last one is Dec 31
    "holiday": (date(2026, 1, 1), _weekdays(date(2025, 10, 1), date(2026, 6, 10),
                                            skip=frozenset({date(2026, 1, 1)}))),
    # inside a five-week suspension; the last close is weeks before the first live day
    "suspended": (date(2025, 12, 15), _weekdays(date(2025, 10, 1), date(2026, 6, 10),
                                                gap=(date(2025, 11, 10), date(2025, 12, 19)))),
}


def _frozen_trend(conn: sqlite3.Connection, now: datetime, reporting: Currency) -> TrendSeries:
    """v0.1.31's steps 9 / 9b on the frozen functions and unbounded (1900-) loads, as served
    (``with_trend_pnl``)."""
    i = load_inputs(conn, now, reporting)
    trend = ref.ref_daily_value_series(i.bundle, i.price_history, i.trend_fx_history,
                                       i.reporting, end=i.as_of,
                                       cash_movements=i.cash_movements)
    if not trend.available:
        return trend
    cash = ref.ref_daily_cash_series(i.cash_movements, i.cash_convs, i.cash_txs, i.cash_divs,
                                     i.instruments, i.cash_fx_history, i.reporting,
                                     end=i.as_of)
    # the series as SERVED: each point's pnl (owner ruling 2026-10-22), which the dashboard
    # sets on every day it serves (stored and live) and the frozen engine never did
    return with_trend_pnl(ref.ref_compose_net_worth(trend, cash))


@pytest.fixture(params=sorted(_SHAPES))
def bounded(request: pytest.FixtureRequest, corpus_db: CorpusFactory
            ) -> Iterator[tuple[str, CorpusDB, date]]:
    shape: str = request.param
    first_live, closes = _SHAPES[shape]
    db = CorpusDB(corpus_db("golden").path, _NOW, Currency.TWD)
    conn = open_db(db.path)
    try:
        upsert_instrument(conn, Instrument(symbol="BND", market=Market.TW,
                                           quote_ccy=Currency.TWD, sector="Utilities",
                                           name="Bounded Co", board="TWSE"))
        insert_transaction(conn, account_id="tw_broker", symbol="BND", side=Side.BUY,
                           quantity=Decimal("2000"), price=Decimal("40"), fees=Decimal("20"),
                           tax=Decimal("0"), trade_date=date(2025, 10, 6))
        upsert_prices(conn, [PriceRow(instrument="BND", market=Market.TW, as_of=d,
                                      close=Decimal(40) + Decimal(n % 9) / Decimal(4),
                                      source="test") for n, d in enumerate(closes)],
                      fetched_at=datetime(2026, 6, 10, 12, 0))
        conn.commit()
    finally:
        conn.close()
    prepare(db)
    assert start_run(db, 1, through=first_live - ONE) == "published"
    yield shape, db, first_live


def test_the_live_part_carries_the_last_close_into_its_first_day(
        bounded: tuple[str, CorpusDB, date]) -> None:
    shape, db, first_live = bounded
    data = _build(db)
    assert data.history_state.live_from == first_live, shape
    conn = open_db(db.path)
    try:
        frozen = _frozen_trend(conn, db.now, db.reporting)
    finally:
        conn.close()
    a, b = canon(frozen), canon(data.trend)
    assert a == b, f"{shape}: {first_difference(a, b)}"
    point = next(p for p in data.trend.points if p.date == first_live)
    assert not point.incomplete, f"{shape}: the carried-forward close was lost"


# ---------------------------------------------------------------------------------------------
# Never a day served from nowhere: placeholders, a moved first day
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def missing_rate(corpus_db: CorpusFactory) -> CorpusDB:
    """``unquoted_ccy_cash`` with its rebate's rate dropped (``perf_f2.drop_rebate_rate``) and
    a store the real worker published: placeholders from the MYR rebate (2026-03-16), which has
    no rate on or before its date."""
    db = corpus_db("unquoted_ccy_cash")
    perf_f2.drop_rebate_rate(db.path)
    prepare(db)
    assert start_run(db, 1) == "published"
    return db


def test_placeholder_days_are_served_live_while_the_live_series_is_available(
        app: FastAPI, missing_rate: CorpusDB, monkeypatch: pytest.MonkeyPatch) -> None:
    """The store holds placeholders from the MYR rebate (2026-03-16, no rate on or before it).
    The missing rate arrives later, dated on the rebate day: the live series is available again
    while the store is dirty only from that day — before the live window, so its days up to
    ``live_from`` would be the previous generation's — and the days from the first placeholder
    are valued live, never read as 0."""
    db = missing_rate
    _x(db, "INSERT INTO fx_rates (base, quote, as_of_date, rate, source, fetched_at) "
           "VALUES ('MYR', 'TWD', '2026-03-16', '7.05', 'test', '2026-06-11T00:00:00')")
    state = _state(db)
    assert state.dirty_from == date(2026, 3, 16) < st.window_start(db.now.date())
    conn = open_db(db.path)
    try:
        assert st.first_placeholder_day(conn, state.generation) == date(2026, 3, 16)
    finally:
        conn.close()
    data = _build(db)
    assert data.trend.available
    assert data.history_state.live_from == date(2026, 3, 16)
    assert _body(app, db) == _live_body(app, db, monkeypatch)


def test_an_option_open_only_on_stored_days_keeps_the_series_flag(
        app: FastAPI, settled: Settled, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review G #2: ``options_value_excluded`` is a series aggregate (spec §4) — the OR of
    the stored days' ``options_open`` before ``live_from`` and the live days' flag. ``rich``
    buys back its last open put TODAY: no live day holds an option, so only the stored days
    carry the flag, and the payload (``options.trend_excludes_value``, its note) must still
    be v0.1.31's — the full live build's."""
    db = settled("rich")
    _x(db, "INSERT INTO transactions (account_id, symbol, side, quantity, price, fees, tax, "
           "trade_date) VALUES ('schwab', 'XYZ261218P00025000', 'BUY', '100', '0.30', '0', "
           "'0', ?)", (db.now.date().isoformat(),))
    data = _build(db)
    assert data.history_state.live_from == db.now.date()   # one live day, no option on it
    assert data.trend.options_value_excluded
    assert data.options is not None and data.options.trend_excludes_value
    assert data.options.open == []                          # nothing open on the live day
    body = _body(app, db)
    assert body == _live_body(app, db, monkeypatch), _first_difference(
        _live_body(app, db, monkeypatch), body)
    assert json.loads(body)["options"]["trend_excludes_value"] is True


def test_a_series_unavailable_live_serves_no_trend(
        app: FastAPI, missing_rate: CorpusDB, monkeypatch: pytest.MonkeyPatch) -> None:
    """While the live series is unavailable (a flow with no rate on or before its date) the
    trend is, exactly as the full live build answers — the store's points before the
    placeholder are never served alone."""
    db = missing_rate
    data = _build(db)
    assert not data.trend.available and data.trend.points == []
    # the series' ONE sentence: the first flow with no rate, its date and its pair (MINOR 4)
    assert data.freshness.trend_unavailable_reason == (
        "2026-03-16 起缺少 MYR/TWD 匯率（當日或之前無匯率），走勢無法計算")
    assert data.history_state.generation == 1
    assert _body(app, db) == _live_body(app, db, monkeypatch)


def test_a_clean_store_of_the_unquoted_rebate_corpus_is_the_live_build(
        app: FastAPI, settled: Settled, monkeypatch: pytest.MonkeyPatch) -> None:
    """PERF-F2: the worker and the live build load the trend's FX over ONE pair set
    (``timeseries.trend_fx_currencies``), so the store settled on ``unquoted_ccy_cash`` — its
    MYR rebate converted, no placeholder — serves exactly what the full live build shows."""
    db = settled("unquoted_ccy_cash")
    data = _build(db)
    assert data.trend.available
    assert data.history_state.generation == 1
    assert data.history_state.live_from == db.now.date()
    body = _body(app, db)
    assert body == _live_body(app, db, monkeypatch), _first_difference(
        _live_body(app, db, monkeypatch), body)


def test_a_first_day_the_generation_does_not_hold_is_served_live(
        app: FastAPI, settled: Settled, monkeypatch: pytest.MonkeyPatch) -> None:
    """A buy back-dated before the series' first day (and before the window): the stored rows
    start later than the live series, so nothing is served from the store — no stale days, no
    gap — and the payload is the full live build's."""
    db = settled("golden")
    _x(db, "INSERT INTO transactions (account_id, symbol, side, quantity, price, fees, tax, "
           "trade_date) VALUES ('tw_broker', '2330', 'BUY', '100', '600', '20', '0', "
           "'2025-12-01')")
    data = _build(db)
    assert data.trend.points[0].date == date(2025, 12, 1)
    assert data.history_state.live_from == date(2025, 12, 1)
    assert not data.history_state.stale
    assert _body(app, db) == _live_body(app, db, monkeypatch)


def test_a_store_settled_past_the_clock_serves_only_the_clocks_days(
        settled: Settled) -> None:
    """A reader whose day is before ``settled_through`` (a clock behind the store) reads the
    stored days up to its own day and values nothing after it."""
    db = settled("golden")
    earlier = CorpusDB(db.path, db.now - timedelta(days=5), db.reporting)
    data = _build(earlier)
    today = earlier.now.date()
    assert data.trend.points[-1].date == today
    assert data.history_state.live_from == today + ONE
    conn = open_db(db.path)
    try:
        frozen = _frozen_trend(conn, earlier.now, earlier.reporting)
    finally:
        conn.close()
    a, b = canon(frozen), canon(data.trend)
    assert a == b, first_difference(a, b)
