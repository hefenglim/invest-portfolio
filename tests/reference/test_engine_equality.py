"""The permanent engine-equality pin (PERF plan W0.2 (b), spec §8 item 6, §18 "Engine").

On every corpus at its fixed clock:

* the LIVE ``build_book`` equals the FROZEN v0.1.31 one on every event day, on the
  dashboard path (``through(d)``, ``allow_oversell=True``) — what the trend replays — and on
  the strict path and the uncut ledger;
* the live trend / net-worth / benchmark functions equal their frozen copies on the inputs
  ``build_dashboard`` assembles (``dashboard_inputs.py``);
* and the frozen pipeline reproduces the COMMITTED v0.1.31 payload's ``trend`` and
  ``benchmark`` blocks, which ties the frozen copies to v0.1.31's own output.

Equality is :func:`tests.reference.canon.canon` equality — exact Decimal digits and exponents,
insertion order, excluded fields included — never plain ``==``. The next wave swaps the live
engine for a one-pass one; this file must stay green throughout.
"""

import json
import sqlite3
from collections.abc import Callable, Iterator
from datetime import date
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from portfolio_dash.data_ingestion.store import load_ledger_bundle
from portfolio_dash.portfolio import benchmark_counterfactual as live_bench
from portfolio_dash.portfolio import cost_basis as live_cb
from portfolio_dash.portfolio import networth as live_nw
from portfolio_dash.portfolio import returns as live_returns
from portfolio_dash.portfolio import timeseries as live_ts
from portfolio_dash.portfolio.dashboard_models import TrendSeries
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.ledger import LedgerBundle
from portfolio_dash.shared.wire import to_wire
from tests.reference import trend_v0131 as ref
from tests.reference.canon import canon, first_difference
from tests.reference.corpora import CORPUS_NAMES, copy_corpus, open_db
from tests.reference.cost_basis_v0131 import ref_books, ref_build_book
from tests.reference.dashboard_inputs import DashboardInputs, benchmark_closes, load_inputs

_PAYLOADS = Path(__file__).resolve().parent / "payloads"

Corpus = tuple[str, sqlite3.Connection, DashboardInputs]


@pytest.fixture(params=CORPUS_NAMES)
def corpus(request: pytest.FixtureRequest, corpus_template_dir: Path,
           tmp_path: Path) -> Iterator[Corpus]:
    name: str = request.param
    db = copy_corpus(name, corpus_template_dir, tmp_path / f"{name}.db")
    conn = open_db(db.path)
    try:
        yield name, conn, load_inputs(conn, db.now, db.reporting)
    finally:
        conn.close()


def _outcome(fn: Callable[[], Any]) -> Any:
    """``canon`` of a result, or of the refusal: the two engines define their OWN exception
    classes, so a refusal is compared by class name, message and identifying attributes."""
    try:
        return canon(fn())
    except Exception as exc:  # noqa: BLE001 - the refusal IS the outcome under comparison
        attrs = {k: str(getattr(exc, k)) for k in ("account_id", "symbol", "trade_date")
                 if hasattr(exc, k)}
        return ("raised", type(exc).__name__, str(exc), tuple(sorted(attrs.items())))


def _assert_same(label: str, ref_form: Any, live_form: Any) -> None:
    assert ref_form == live_form, f"{label}: {first_difference(ref_form, live_form)}"


def _event_days(bundle: LedgerBundle) -> list[date]:
    return sorted(bundle.counts_on_dates())


def test_books_equal_on_every_event_day(corpus: Corpus) -> None:
    name, _conn, inputs = corpus
    days = _event_days(inputs.bundle)
    assert days, f"{name}: a corpus with no event day pins nothing"
    refs = ref_books(inputs.bundle, days)
    for d, ref_book in zip(days, refs, strict=True):
        live_book = live_cb.build_book(inputs.bundle.through(d), allow_oversell=True)
        _assert_same(f"{name} {d}", canon(ref_book), canon(live_book))


def test_strict_and_uncut_books_equal(corpus: Corpus) -> None:
    name, conn, inputs = corpus
    uncut = load_ledger_bundle(conn).without_unregistered()
    for label, bundle in (("valued", inputs.bundle), ("uncut", uncut)):
        for strict in (True, False):
            _assert_same(
                f"{name} {label} strict={strict}",
                _outcome(partial(ref_build_book, bundle, allow_oversell=not strict)),
                _outcome(partial(live_cb.build_book, bundle, allow_oversell=not strict)))


def _frozen_trend(inputs: DashboardInputs) -> TrendSeries:
    """``build_dashboard`` step 9 / 9b, on the frozen functions."""
    if not inputs.has_events:
        return TrendSeries(points=[], reporting_currency=inputs.reporting, available=False)
    trend = ref.ref_daily_value_series(
        inputs.bundle, inputs.price_history, inputs.trend_fx_history, inputs.reporting,
        end=inputs.as_of, cash_movements=inputs.cash_movements)
    if not trend.available:
        return trend
    cash = ref.ref_daily_cash_series(
        inputs.cash_movements, inputs.cash_convs, inputs.cash_txs, inputs.cash_divs,
        inputs.instruments, inputs.cash_fx_history, inputs.reporting, end=inputs.as_of)
    return ref.ref_compose_net_worth(trend, cash)


def test_series_equal(corpus: Corpus) -> None:
    name, conn, i = corpus
    book = ref_build_book(i.bundle, allow_oversell=True)

    def flows(fn: Callable[..., Any]) -> Any:
        return fn(i.bundle, i.trend_fx_history, i.reporting, cash_movements=i.cash_movements,
                  refused_dividends=book.refused_dividends)

    ref_flows = flows(ref.ref_build_reporting_flows)
    _assert_same(f"{name} flows", canon(ref_flows), canon(flows(live_ts.build_reporting_flows)))
    _assert_same(f"{name} replay_refusals", canon(ref.ref_replay_refusals(i.bundle)),
                 canon(live_returns.replay_refusals(i.bundle)))

    if not i.has_events:
        return
    ref_trend = ref.ref_daily_value_series(
        i.bundle, i.price_history, i.trend_fx_history, i.reporting, end=i.as_of,
        cash_movements=i.cash_movements)
    live_trend = live_ts.daily_value_series(
        i.bundle, i.price_history, i.trend_fx_history, i.reporting, end=i.as_of,
        cash_movements=i.cash_movements)
    _assert_same(f"{name} daily_value_series", canon(ref_trend), canon(live_trend))

    cash_args = (i.cash_movements, i.cash_convs, i.cash_txs, i.cash_divs, i.instruments,
                 i.cash_fx_history, i.reporting)
    ref_cash = ref.ref_daily_cash_series(*cash_args, end=i.as_of)
    live_cash = live_nw.daily_cash_series(*cash_args, end=i.as_of)
    _assert_same(f"{name} daily_cash_series", canon(ref_cash), canon(live_cash))
    _assert_same(f"{name} compose_net_worth", canon(ref.ref_compose_net_worth(ref_trend, ref_cash)),
                 canon(live_nw.compose_net_worth(live_trend, live_cash)))

    if ref_flows is not None:
        closes = benchmark_closes(conn, i, ref_flows)
        _assert_same(f"{name} counterfactual", canon(ref.ref_counterfactual(ref_flows, closes)),
                     canon(live_bench.counterfactual(ref_flows, closes)))


def _payload(name: str) -> dict[str, Any]:
    body: dict[str, Any] = json.loads((_PAYLOADS / name / "dashboard.json").read_bytes())
    return body


def test_frozen_pipeline_reproduces_the_v0131_payload(corpus: Corpus) -> None:
    """The frozen copies + the dashboard's inputs give back v0.1.31's committed trend and
    benchmark blocks — so the frozen modules are what v0.1.31 RAN, not just what it held."""
    name, conn, i = corpus
    payload = _payload(name)
    assert to_wire(_frozen_trend(i).model_dump()) == payload["trend"]

    book = ref_build_book(i.bundle, allow_oversell=True)
    flows = ref.ref_build_reporting_flows(i.bundle, i.trend_fx_history, i.reporting,
                                          cash_movements=i.cash_movements,
                                          refused_dividends=book.refused_dividends)
    bench = payload["benchmark"]
    if flows is None:
        assert bench["available"] is False
        assert bench["uncovered_ratio"] is None
        return
    cf = ref.ref_counterfactual(flows, benchmark_closes(conn, i, flows))
    wire = to_wire({"available": cf.available, "reason": cf.reason,
                    "terminal_value": cf.terminal_value, "net_invested": cf.net_invested,
                    "benchmark_return": cf.benchmark_return,
                    "uncovered_markets": list(cf.uncovered_markets),
                    "uncovered_ratio": cf.uncovered_ratio})
    assert wire == {k: bench[k] for k in wire}


def test_the_pin_bites(corpus_template_dir: Path, tmp_path: Path,
                       monkeypatch: pytest.MonkeyPatch) -> None:
    """A live engine that drifts by ONE Decimal exponent from one day on fails the pin, on
    that day — the comparison is not ``==`` (``Decimal("500000") == Decimal("500000.0")``)."""
    db = copy_corpus("golden", corpus_template_dir, tmp_path / "golden.db")
    conn = open_db(db.path)
    try:
        inputs = load_inputs(conn, db.now, db.reporting)
        real = live_cb.build_book

        def drifting(bundle: LedgerBundle, *, allow_oversell: bool = False) -> Any:
            book = real(bundle, allow_oversell=allow_oversell)
            if not bundle.dividends:
                return book
            return book.model_copy(update={"gross_invested": {
                c: v + Decimal("0.0") for c, v in book.gross_invested.items()}})

        monkeypatch.setattr(live_cb, "build_book", drifting)
        with pytest.raises(AssertionError, match="golden 2026-03-01: .*gross_invested"):
            test_books_equal_on_every_event_day(("golden", conn, inputs))
    finally:
        conn.close()


def test_canon_sees_what_eq_misses() -> None:
    assert Decimal("1") == Decimal("1.0")
    assert canon(Decimal("1")) != canon(Decimal("1.0"))
    assert {"a": 1, "b": 2} == {"b": 2, "a": 1}
    assert canon({"a": 1, "b": 2}) != canon({"b": 2, "a": 1})
    on, off = (TrendSeries(points=[], reporting_currency=Currency.TWD,
                           options_value_excluded=flag) for flag in (True, False))
    assert on.model_dump_json() == off.model_dump_json()  # the field is exclude=True
    assert canon(on) != canon(off)
