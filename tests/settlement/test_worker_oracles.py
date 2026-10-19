"""Independent oracles for what a run stores (package-D review MAJOR 5: the tests must not
compare the code with itself).

- **cash at D** ≡ a direct sum of the ledger rows in the test (movements by their kind's
  direction, both legs of every conversion, every trade's settled 價金 ± fees and tax — the
  settlement rounding transcribed here — and every booked cash dividend), and its reporting
  value at the ``fx_rates`` row on or before D;
- **the non-tax activity lines** (fees and tax, every cash-movement kind, FX conversions,
  openings) ≡ direct SQL sums, each converted at its own date's ``fx_rates`` row;
- **the days before the unconvertible flow of** ``unquoted_ccy_cash`` ≡ the v0.1.31 trend of
  ``golden`` (the same ledger until the rebate) on EVERY day, not only at month ends;
- **the benchmark state** ≡ the v0.1.31 ``benchmark`` block: the uncovered markets and ratio,
  each market's invested amount, and each terminal value as units × the last stored close at
  the stored rate.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any

import pytest

from portfolio_dash.portfolio import settlement_store as st
from portfolio_dash.portfolio.benchmark_counterfactual import BenchState
from portfolio_dash.pricing.benchmarks import benchmark_for_market
from portfolio_dash.shared.enums import Market
from portfolio_dash.shared.wire import to_wire
from tests.reference.corpora import CORPUS_NAMES, CorpusDB, open_db
from tests.settlement.conftest import CorpusFactory
from tests.settlement.test_worker import (
    generation,
    prepare,
    rate_oracle,
    reference,
    start_run,
    wire_day,
    yesterday,
)

#: The cash kinds that credit the pool (``shared/cash_kinds.py``, transcribed).
_CREDIT = {"DEPOSIT", "OPENING", "REBATE", "INTEREST"}
#: The minor unit and the settlement rounding of each currency (data-and-pricing.md).
_SETTLE = {"TWD": (Decimal("1"), ROUND_DOWN), "USD": (Decimal("0.01"), ROUND_HALF_UP),
           "MYR": (Decimal("0.01"), ROUND_HALF_UP)}


def _settled(qty: Decimal, price: Decimal, ccy: str) -> Decimal:
    unit, mode = _SETTLE[ccy]
    return (qty * price).quantize(unit, rounding=mode)


def _run(db: CorpusDB) -> tuple[sqlite3.Connection, int]:
    prepare(db)
    assert start_run(db, 1) == "published"
    conn = open_db(db.path)
    return conn, generation(conn)


def _quote_ccy(conn: sqlite3.Connection) -> dict[str, str]:
    return {str(r[0]): str(r[1]) for r in conn.execute("SELECT symbol, quote_ccy FROM instruments")}


def _cash_oracle(conn: sqlite3.Connection, day: date) -> dict[tuple[str, str], Decimal]:
    ccy_of = _quote_ccy(conn)
    bal: dict[tuple[str, str], Decimal] = defaultdict(lambda: Decimal("0"))
    d = day.isoformat()
    for acct, kind, ccy, amount in conn.execute(
            "SELECT account_id, kind, ccy, amount FROM cash_movements WHERE date <= ?", (d,)):
        sign = 1 if str(kind).upper() in _CREDIT else -1
        bal[(acct, ccy)] += sign * Decimal(amount)
    for acct, fc, fa, tc, ta in conn.execute(
            "SELECT account_id, from_ccy, from_amount, to_ccy, to_amount FROM fx_conversions "
            "WHERE date <= ?", (d,)):
        bal[(acct, fc)] -= Decimal(fa)
        bal[(acct, tc)] += Decimal(ta)
    for acct, sym, side, qty, price, fees, tax in conn.execute(
            "SELECT account_id, symbol, side, quantity, price, fees, tax FROM transactions "
            "WHERE trade_date <= ?", (d,)):
        if sym not in ccy_of:
            continue
        ccy = ccy_of[sym]
        gross = _settled(Decimal(qty), Decimal(price), ccy)
        if side == "BUY":
            bal[(acct, ccy)] -= gross + Decimal(fees) + Decimal(tax)
        else:
            bal[(acct, ccy)] += gross - Decimal(fees) - Decimal(tax)
    for acct, sym, net in conn.execute(
            "SELECT account_id, symbol, net FROM dividends WHERE date <= ? "
            "AND type IN ('CASH', 'NET')", (d,)):
        if sym in ccy_of:
            bal[(acct, ccy_of[sym])] += Decimal(net)
    return dict(bal)


@pytest.mark.parametrize("name", [n for n in CORPUS_NAMES if n != "rich_refusals"])
def test_cash_at_every_close_is_the_ledger_sum(corpus_db: CorpusFactory, name: str) -> None:
    """(``rich_refusals`` refuses payouts, which the oracle cannot tell without a replay.)"""
    db = corpus_db(name)
    conn, g = _run(db)
    try:
        checked = 0
        for close in st.read_periods(conn, g, kind="M"):
            oracle = _cash_oracle(conn, close.end_day)
            cash = st.read_period_cash(conn, g, "M", close.period)
            got = {(c.account_id, c.ccy.value): c for c in cash}
            assert {k for k, v in oracle.items() if v} <= set(got), f"{name} {close.period}"
            for key, c in got.items():
                assert c.balance == oracle.get(key, Decimal("0")), f"{name} {close.period} {key}"
                rate = rate_oracle(conn, key[1], db.reporting.value, close.end_day)
                if key[1] == db.reporting.value:
                    assert c.reporting_value == c.balance
                elif rate is None and c.balance == 0:
                    assert c.reporting_value == 0  # an empty pool needs no rate
                elif rate is None:
                    assert c.reporting_value is None
                    assert f"{key[1]}/{db.reporting.value}" in close.kpi.notes.missing_cash_fx
                else:
                    assert c.reporting_value == c.balance * rate[0], f"{name} {key}"
                    assert c.fx_rate == rate[0] and c.fx_as_of == rate[1]
            checked += 1
        assert checked
    finally:
        conn.close()


def _first_row(conn: sqlite3.Connection) -> date:
    row = conn.execute(
        "SELECT MIN(d) FROM (SELECT MIN(trade_date) AS d FROM transactions"
        " UNION ALL SELECT MIN(build_date) FROM opening_inventory"
        " UNION ALL SELECT MIN(CASE WHEN type = 'STOCK' AND ex_date IS NOT NULL THEN ex_date"
        " ELSE date END) FROM dividends"
        " UNION ALL SELECT MIN(date) FROM cash_movements"
        " UNION ALL SELECT MIN(date) FROM fx_conversions"
        " UNION ALL SELECT MIN(date) FROM corporate_actions)").fetchone()
    return date.fromisoformat(row[0])


def _bounds(conn: sqlite3.Connection, g: int, close: st.SettledPeriod) -> tuple[date, date]:
    """The close's activity window: its calendar period, the FIRST month and year from the
    ledger's first row."""
    first = st.store_state(conn, reporting=close.kpi.reporting_ccy).first_day
    assert first is not None
    end = close.end_day
    start = date(end.year, end.month, 1) if close.kind == "M" else date(end.year, 1, 1)
    first_period = ((end.year, end.month) == (first.year, first.month) if close.kind == "M"
                    else end.year == first.year)
    del g
    return (min(start, _first_row(conn)) if first_period else start), end


def _money(conn: sqlite3.Connection, rep: str, rows: list[tuple[str, Decimal, str]]
           ) -> tuple[Decimal, Decimal | None]:
    """Native Σ and Σ amount × rate(row date), left to right; ``None`` once a rate is
    missing."""
    native, reporting, ok = Decimal("0"), Decimal("0"), True
    for day, amount, ccy in rows:
        native += amount
        rate = rate_oracle(conn, ccy, rep, date.fromisoformat(day))
        if rate is None:
            ok = False
        else:
            reporting += amount * rate[0]
    return native, reporting if ok else None


@pytest.mark.parametrize("name", CORPUS_NAMES)
def test_the_non_tax_activity_lines_are_the_ledger_sums(corpus_db: CorpusFactory,
                                                       name: str) -> None:
    db = corpus_db(name)
    conn, g = _run(db)
    rep = db.reporting.value
    try:
        ccy_of = _quote_ccy(conn)
        compared = 0
        for close in st.read_periods(conn, g):
            lo, hi = _bounds(conn, g, close)
            span = (lo.isoformat(), hi.isoformat())
            act = close.activity
            # fees and tax, by account and currency, in ledger (date, id) order
            fees: dict[tuple[str, str], list[tuple[str, Decimal, str]]] = defaultdict(list)
            taxes: dict[tuple[str, str], list[tuple[str, Decimal, str]]] = defaultdict(list)
            for acct, sym, day, fee, tax in conn.execute(
                    "SELECT account_id, symbol, trade_date, fees, tax FROM transactions "
                    "WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date, id", span):
                ccy = ccy_of[sym]
                fees[(acct, ccy)].append((day, Decimal(fee), ccy))
                taxes[(acct, ccy)].append((day, Decimal(tax), ccy))
            got_fees = {(f.account_id, f.fees.ccy.value): f for f in act.fees}
            assert set(got_fees) == set(fees), f"{name} {close.period}"
            for key, line in got_fees.items():
                assert (line.fees.native, line.fees.reporting) == _money(conn, rep, fees[key])
                assert (line.tax.native, line.tax.reporting) == _money(conn, rep, taxes[key])
            # every cash-movement kind, by account and currency
            moves: dict[tuple[str, str, str], list[tuple[str, Decimal, str]]] = defaultdict(list)
            for acct, day, kind, ccy, amount in conn.execute(
                    "SELECT account_id, date, kind, ccy, amount FROM cash_movements "
                    "WHERE date BETWEEN ? AND ? ORDER BY date, id", span):
                moves[(acct, str(kind).upper(), ccy)].append((day, Decimal(amount), ccy))
            got_moves = {(m.account_id, m.kind, m.amount.ccy.value): m
                         for m in act.cash_movements}
            assert set(got_moves) == set(moves), f"{name} {close.period}"
            for mkey, mline in got_moves.items():
                assert mline.rows == len(moves[mkey])
                assert (mline.amount.native, mline.amount.reporting) == _money(
                    conn, rep, moves[mkey]), f"{name} {close.period} {mkey}"
            # FX conversions, both amounts
            convs: dict[tuple[str, str, str], list[tuple[str, Decimal, Decimal]]] = (
                defaultdict(list))
            for acct, day, fc, fa, tc, ta in conn.execute(
                    "SELECT account_id, date, from_ccy, from_amount, to_ccy, to_amount FROM "
                    "fx_conversions WHERE date BETWEEN ? AND ? ORDER BY date, id", span):
                convs[(acct, fc, tc)].append((day, Decimal(fa), Decimal(ta)))
            got_convs = {(x.account_id, x.from_amount.ccy.value, x.to_amount.ccy.value): x
                         for x in act.fx_conversions}
            assert set(got_convs) == set(convs), f"{name} {close.period}"
            for xkey, xline in got_convs.items():
                rows = convs[xkey]
                assert (xline.from_amount.native, xline.from_amount.reporting) == _money(
                    conn, rep, [(d, a, xkey[1]) for d, a, _b in rows])
                assert (xline.to_amount.native, xline.to_amount.reporting) == _money(
                    conn, rep, [(d, b, xkey[2]) for d, _a, b in rows])
            # opening inventory built in the period
            openings = {(str(r[0]), str(r[1])): (Decimal(r[2]), Decimal(r[3]))
                        for r in conn.execute(
                            "SELECT account_id, symbol, shares, original_cost_total FROM "
                            "opening_inventory WHERE build_date BETWEEN ? AND ?", span)}
            assert {(o.account_id, o.symbol): (o.shares, o.original_cost_total.native)
                    for o in act.openings} == openings, f"{name} {close.period}"
            compared += 1
        assert compared
    finally:
        conn.close()


def test_the_days_before_the_unconvertible_flow_are_goldens_trend(
        corpus_db: CorpusFactory) -> None:
    """``unquoted_ccy_cash`` is ``golden`` plus a MYR rate and a 50 MYR rebate on 2026-03-16;
    before the rebate the two ledgers are the same, so every settled day before it ≡ v0.1.31's
    golden trend point — on EVERY day."""
    db = corpus_db("unquoted_ccy_cash")
    conn, g = _run(db)
    try:
        rebate = date(2026, 3, 16)
        days = {d.day: d for d in st.read_days(conn, g, end=rebate - timedelta(days=1))}
        golden = [p for p in reference("golden", "dashboard.json")["trend"]["points"]
                  if p["date"] < rebate.isoformat()]
        assert len(golden) == len(days) > 60
        assert [wire_day(days[date.fromisoformat(p["date"])]) for p in golden] == golden
    finally:
        conn.close()


def _last_close(conn: sqlite3.Connection, key: str, day: date) -> tuple[Decimal, date] | None:
    row = conn.execute("SELECT close, as_of_date FROM prices WHERE instrument = ? AND "
                       "as_of_date <= ? ORDER BY as_of_date DESC LIMIT 1",
                       (key, day.isoformat())).fetchone()
    return None if row is None else (Decimal(row[0]), date.fromisoformat(row[1]))


@pytest.mark.parametrize("name", CORPUS_NAMES)
def test_the_benchmark_state_is_the_reference_benchmark(corpus_db: CorpusFactory,
                                                       name: str) -> None:
    db = corpus_db(name)
    conn, _g = _run(db)
    try:
        ref: dict[str, Any] = reference(name, "dashboard.json")["benchmark"]
        stored = st.store_state(conn, reporting=db.reporting).bench_state
        if stored is None:
            assert not ref["available"] and "匯率" in (ref["reason"] or ""), name
            return
        state = BenchState.from_json(json.loads(stored))
        assert sorted(state.uncovered) == ref["uncovered_markets"], name
        ratio = (state.uncovered_gross / state.gross_all if state.gross_all > 0
                 else Decimal("0"))
        assert to_wire(ratio) == ref["uncovered_ratio"], name
        legs = {leg["market"]: leg for leg in ref["by_market"]}
        assert {m.value for m in state.units} == set(legs), name
        for market, units in state.units.items():
            leg = legs[market.value]
            assert to_wire(state.invested[market]) == leg["net_invested"], f"{name} {market}"
            bench = benchmark_for_market(Market(market))
            assert bench is not None
            last = _last_close(conn, bench.storage_key, db.now.date())
            assert last is not None
            rate = rate_oracle(conn, bench.quote_ccy.value, db.reporting.value, last[1])
            assert rate is not None
            assert to_wire(units * (last[0] * rate[0])) == leg["terminal_value"], name
        assert yesterday(db) == st.store_state(conn, reporting=db.reporting).settled_through
    finally:
        conn.close()
