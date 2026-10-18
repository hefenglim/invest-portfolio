"""``timeseries.value_series`` and ``networth.daily_cash_series(start=…)`` (PERF spec §8.2–§8.3).

The one-pass trend: the per-day valuation loop of ``daily_value_series``, fed by
``iter_books`` instead of a replay per event day, with FX by a moving index and
``net_invested`` from an exactness-guarded running sum. On every corpus it must equal the
FROZEN v0.1.31 ``daily_value_series`` (``tests/reference/trend_v0131.py``) point for point,
compared by :func:`tests.reference.canon.canon` — exponents and the ``exclude=True`` flag
included. The guard's fallback (the reference per-day sum) is forced on every corpus and must
be equal too; ``rich_refusals`` and a crafted USD ledger take it without being forced.

A series starting after its first day carries the flag of the WHOLE trend
(``options_value_excluded`` is the OR over every day, spec §4): the caller states whether a day
before the start held an open option (``options_open_before``), and a ledger that closed its
option before the start shows the argument matters. The book's refusals are an input that
matters too: ``refused_dividends=[]`` (the reviewer's surviving mutant) gives another series on
a ledger with refused cash dividends.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest

from portfolio_dash.portfolio import timeseries as ts
from portfolio_dash.portfolio.cost_basis import build_book, iter_books
from portfolio_dash.portfolio.dashboard_models import TrendSeries
from portfolio_dash.portfolio.networth import daily_cash_series
from portfolio_dash.portfolio.timeseries import (
    _NetInvested,
    daily_value_series,
    holds_open_option,
    trend_book_days,
    trend_start,
    value_series,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.models.ledger import LedgerBundle, Transaction
from tests.reference import trend_v0131 as ref
from tests.reference.canon import canon, first_difference
from tests.reference.corpora import CORPUS_NAMES, TEMPLATE_DIR_NAME, copy_corpus, open_db
from tests.reference.cost_basis_v0131 import ref_books
from tests.reference.dashboard_inputs import DashboardInputs, load_inputs


@pytest.fixture(scope="module", params=CORPUS_NAMES)
def corpus(request: pytest.FixtureRequest,
           tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, DashboardInputs]]:
    name: str = request.param
    base = tmp_path_factory.getbasetemp() / TEMPLATE_DIR_NAME
    db = copy_corpus(name, base, tmp_path_factory.mktemp(f"vs-{name}") / f"{name}.db")
    conn = open_db(db.path)
    try:
        yield name, load_inputs(conn, db.now, db.reporting)
    finally:
        conn.close()


def _reference(i: DashboardInputs) -> TrendSeries:
    return ref.ref_daily_value_series(i.bundle, i.price_history, i.trend_fx_history,
                                      i.reporting, end=i.as_of,
                                      cash_movements=i.cash_movements)


def _open_before(bundle: LedgerBundle, start: date) -> bool:
    """Whether a day from the trend's first day to the day before *start* held an open option
    — what the store read path states from its stored days, here from the FROZEN books."""
    first = trend_start(bundle)
    assert first is not None
    days = sorted(d for d in bundle.counts_on_dates() if first <= d < start)
    return any(holds_open_option(b, bundle.instruments) for b in ref_books(bundle, days))


def _one_pass(i: DashboardInputs, start: date, *, refused: Any = None) -> TrendSeries:
    """The trend as the dashboard builds it: one ``iter_books`` pass, the book's refusals."""
    days = trend_book_days(i.bundle, start=start, end=i.as_of)
    return value_series(i.bundle, i.price_history, i.trend_fx_history, i.reporting,
                        start=start, end=i.as_of, cash_movements=i.cash_movements,
                        refused_dividends=(build_book(i.bundle, allow_oversell=True)
                                           .refused_dividends if refused is None else refused),
                        books=zip(days, iter_books(i.bundle, days), strict=True),
                        options_open_before=_open_before(i.bundle, start))


def _assert_same(label: str, expected: Any, got: Any) -> None:
    a, b = canon(expected), canon(got)
    assert a == b, f"{label}: {first_difference(a, b)}"


def _from(series: TrendSeries, start: date) -> TrendSeries:
    """The reference series with the days before *start* removed — its flag kept: a later
    start carries the whole trend's ``options_value_excluded``."""
    return series.model_copy(update={"points": [p for p in series.points if p.date >= start]})


def test_the_one_pass_trend_equals_v0131(corpus: tuple[str, DashboardInputs]) -> None:
    name, i = corpus
    first = trend_start(i.bundle)
    if first is None:
        pytest.skip(f"{name}: no trend")
    expected = _reference(i)
    assert expected.available or name == "unquoted_ccy_cash"
    _assert_same(name, expected, _one_pass(i, first))


def test_a_later_start_values_the_same_days_the_same(corpus: tuple[str, DashboardInputs]
                                                    ) -> None:
    """A tail starting mid-series — on an event day, on the next day (often a weekend or a
    non-event weekday) and before the first day — reads the same points as the full series."""
    name, i = corpus
    first = trend_start(i.bundle)
    assert first is not None
    expected = _reference(i)
    if not expected.available:
        _assert_same(f"{name} tail", expected, _one_pass(i, first + timedelta(days=40)))
        return
    events = sorted(d for d in i.bundle.counts_on_dates() if first < d < i.as_of)
    middle = events[len(events) // 2]
    for start in (middle, middle + timedelta(days=1), first - timedelta(days=30),
                  i.as_of):
        _assert_same(f"{name} from {start}", _from(expected, start), _one_pass(i, start))


def test_the_forced_fallback_is_equal_and_actually_runs(
    corpus: tuple[str, DashboardInputs], monkeypatch: pytest.MonkeyPatch,
) -> None:
    name, i = corpus
    first = trend_start(i.bundle)
    assert first is not None
    calls = [0]
    real = ts._reference_net_invested

    def counted(converted: Any, day: date) -> Decimal:
        calls[0] += 1
        return real(converted, day)

    monkeypatch.setattr(ts, "_running_sum_is_exact", lambda amounts: False)
    monkeypatch.setattr(ts, "_reference_net_invested", counted)
    expected = _reference(i)
    _assert_same(f"{name} fallback", expected, _one_pass(i, first))
    if expected.available:
        assert calls[0] > 0, f"{name}: the fallback never ran"
        flow_days = {p.date for p in expected.points}
        assert calls[0] < len(flow_days), "the fallback recomputes on flow days only"


def _flows(i: DashboardInputs) -> list[tuple[date, Decimal]]:
    flows = ts.build_reporting_flows(
        i.bundle, i.trend_fx_history, i.reporting, cash_movements=i.cash_movements,
        refused_dividends=build_book(i.bundle, allow_oversell=True).refused_dividends)
    assert flows is not None
    return [(f.on, f.amount) for f in flows]


#: Which corpora take the reference per-day sum by themselves — measured, not assumed.
#: ``usd_reporting``'s one 28-digit amount (1 / 31.9 does not terminate) sits beside two short
#: ones and their sum still fits 28 digits; ``rich_refusals`` (reporting USD over many TWD and
#: MYR flows through 28-digit inverse rates) does not fit, so its trend takes the fallback.
_TAKES_THE_FALLBACK = {"rich_refusals"}


def test_which_corpora_take_the_fallback_by_themselves(corpus: tuple[str, DashboardInputs]
                                                       ) -> None:
    name, i = corpus
    if not _reference(i).available:
        pytest.skip(f"{name}: no flows to sum (the trend is unavailable)")
    assert _NetInvested(_flows(i)).exact is (name not in _TAKES_THE_FALLBACK), name


def test_a_usd_ledger_whose_sum_needs_more_digits_takes_the_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two TWD buys reported in USD through the inverse of 31.9: each amount carries 28
    significant digits and their sum needs 29 — the bound fails by itself, the reference
    per-day sum is used, and the series equals v0.1.31's."""
    usd, twd = Currency.USD, Currency.TWD
    inst = Instrument(symbol="BBB", market=Market.TW, quote_ccy=twd, sector="Semis",
                      name="BBB Corp", board="TWSE")
    bundle = LedgerBundle(
        transactions=[
            Transaction(account_id="tw_broker", symbol="BBB", side=Side.BUY,
                        quantity=Decimal("20000"), price=Decimal("100"), fees=Decimal("0"),
                        tax=Decimal("0"), trade_date=date(2026, 3, 2)),
            Transaction(account_id="tw_broker", symbol="BBB", side=Side.BUY,
                        quantity=Decimal("20000"), price=Decimal("101"), fees=Decimal("0"),
                        tax=Decimal("0"), trade_date=date(2026, 3, 9)),
        ],
        instruments={"BBB": inst})
    prices = {"BBB": [(date(2026, 3, 2), Decimal("100")), (date(2026, 3, 13), Decimal("103"))]}
    fx = {(usd, twd): [(date(2026, 3, 1), Decimal("31.9"))]}
    end = date(2026, 3, 20)
    calls = [0]
    real = ts._reference_net_invested

    def counted(converted: Any, day: date) -> Decimal:
        calls[0] += 1
        return real(converted, day)

    monkeypatch.setattr(ts, "_reference_net_invested", counted)
    expected = ref.ref_daily_value_series(bundle, prices, fx, usd, end=end, cash_movements=[])
    assert max(len(p.net_invested.as_tuple().digits) for p in expected.points) == 28
    days = trend_book_days(bundle, start=date(2026, 3, 2), end=end)
    got = value_series(bundle, prices, fx, usd, start=date(2026, 3, 2), end=end,
                       cash_movements=[], refused_dividends=[],
                       books=zip(days, iter_books(bundle, days), strict=True),
                       options_open_before=False)
    _assert_same("crafted usd", expected, got)
    assert calls[0] == 2  # the first day, and the second buy's day


def test_the_exactness_bound() -> None:
    exact = ts._running_sum_is_exact
    assert exact([Decimal("1.5"), Decimal("-2"), Decimal("100")])
    assert exact([])
    # 28 digits at the smallest exponent is the limit (prec = 28)
    assert exact([Decimal("9" * 27), Decimal("1")])
    assert not exact([Decimal("9" * 28), Decimal("1")])
    assert not exact([Decimal("1E+30"), Decimal("0.1")])
    assert not exact([Decimal("NaN")])


def test_a_book_missing_for_an_event_day_is_refused(corpus: tuple[str, DashboardInputs]
                                                    ) -> None:
    name, i = corpus
    first = trend_start(i.bundle)
    assert first is not None
    days = trend_book_days(i.bundle, start=first, end=i.as_of)
    if len(days) < 3 or not _reference(i).available:
        pytest.skip(f"{name}: nothing to drop")
    kept = days[:1] + days[2:]
    with pytest.raises(ValueError, match="no book for"):
        value_series(i.bundle, i.price_history, i.trend_fx_history, i.reporting,
                     start=first, end=i.as_of, cash_movements=i.cash_movements,
                     refused_dividends=[],
                     books=zip(kept, iter_books(i.bundle, kept), strict=True),
                     options_open_before=False)


def test_trend_book_days() -> None:
    bundle = LedgerBundle()
    assert trend_start(bundle) is None
    assert trend_book_days(bundle, start=date(2026, 1, 1), end=date(2026, 12, 31)) == []


def test_the_cash_series_from_a_start_day_is_the_full_one_cut(
    corpus: tuple[str, DashboardInputs],
) -> None:
    name, i = corpus
    args = (i.cash_movements, i.cash_convs, i.cash_txs, i.cash_divs, i.instruments,
            i.cash_fx_history, i.reporting)
    full = daily_cash_series(*args, end=i.as_of)
    _assert_same(f"{name} cash", ref.ref_daily_cash_series(*args, end=i.as_of), full)
    if not full:
        return
    days = sorted(full)
    for start in (days[0] - timedelta(days=3), days[len(days) // 2], days[-1],
                  days[-1] + timedelta(days=1)):
        cut = {d: v for d, v in full.items() if d >= start}
        _assert_same(f"{name} cash from {start}", cut,
                     daily_cash_series(*args, end=i.as_of, start=start))


# --- the series flag over every day, and the refusals as an input ---------------------------


def test_holds_open_option_is_the_series_flag(corpus: tuple[str, DashboardInputs]) -> None:
    """The per-day test the store keeps, OR-ed over every event day's book, is the frozen
    series' ``options_value_excluded``."""
    name, i = corpus
    expected = _reference(i)
    if not expected.available:
        pytest.skip(f"{name}: no trend")
    assert _open_before(i.bundle, i.as_of + timedelta(days=1)) == (
        expected.options_value_excluded), name


_PUT = "XYZ261218P00050000"


def _closed_option_ledger() -> tuple[LedgerBundle, dict[str, list[tuple[date, Decimal]]]]:
    """A put bought and sold within January, a stock held throughout — the option is open on
    days BEFORE a February start and on none after it."""
    from portfolio_dash.data_ingestion.register import option_contract_draft
    from portfolio_dash.shared.options import parse_option_symbol

    terms = parse_option_symbol(_PUT)
    assert terms is not None
    stock = Instrument(symbol="XYZ", market=Market.US, quote_ccy=Currency.USD, sector="Tech",
                       name="XYZ Corp")
    put = option_contract_draft(terms)

    def tx(symbol: str, side: Side, qty: str, price: str, day: date) -> Transaction:
        return Transaction(account_id="schwab", symbol=symbol, side=side,
                           quantity=Decimal(qty), price=Decimal(price), fees=Decimal("0"),
                           tax=Decimal("0"), trade_date=day)

    bundle = LedgerBundle(
        transactions=[tx("XYZ", Side.BUY, "10", "50", date(2026, 1, 5)),
                      tx(_PUT, Side.BUY, "100", "1.20", date(2026, 1, 12)),
                      tx(_PUT, Side.SELL, "100", "1.50", date(2026, 1, 26))],
        instruments={"XYZ": stock, _PUT: put})
    prices = {"XYZ": [(date(2026, 1, 5), Decimal("50")), (date(2026, 2, 2), Decimal("52"))]}
    return bundle, prices


def test_a_later_start_carries_the_whole_trends_option_flag() -> None:
    bundle, prices = _closed_option_ledger()
    end, start = date(2026, 2, 20), date(2026, 2, 3)
    expected = ref.ref_daily_value_series(bundle, prices, {}, Currency.USD, end=end,
                                          cash_movements=[])
    assert expected.options_value_excluded is True
    days = trend_book_days(bundle, start=start, end=end)

    def tail(open_before: bool) -> TrendSeries:
        return value_series(bundle, prices, {}, Currency.USD, start=start, end=end,
                            cash_movements=[], refused_dividends=[],
                            books=zip(days, iter_books(bundle, days), strict=True),
                            options_open_before=open_before)

    assert _open_before(bundle, start) is True
    _assert_same("closed option", _from(expected, start), tail(True))
    assert tail(False).options_value_excluded is False  # the argument is what carries it


def test_the_option_flag_argument_cannot_be_left_out_or_contradicted() -> None:
    bundle, prices = _closed_option_ledger()
    first = trend_start(bundle)
    assert first is not None
    with pytest.raises(TypeError, match="options_open_before"):
        daily_value_series(bundle, prices, {}, Currency.USD, end=date(2026, 2, 20),
                           cash_movements=[], start=date(2026, 2, 3))
    days = trend_book_days(bundle, start=first, end=date(2026, 2, 20))
    with pytest.raises(ValueError, match="no day precedes"):
        value_series(bundle, prices, {}, Currency.USD, start=first, end=date(2026, 2, 20),
                     cash_movements=[], refused_dividends=[],
                     books=zip(days, iter_books(bundle, days), strict=True),
                     options_open_before=True)


def test_the_books_refusals_are_an_input_that_matters(corpus: tuple[str, DashboardInputs]
                                                      ) -> None:
    """The reviewer's surviving mutant — ``refused_dividends=[]`` handed to the trend — gives
    another series on every corpus whose replay refuses a CASH-type payout."""
    name, i = corpus
    book = build_book(i.bundle, allow_oversell=True)
    cash_refused = [d for d in book.refused_dividends if d.type.value in ("CASH", "NET")]
    first = trend_start(i.bundle)
    if not cash_refused or first is None or not _reference(i).available:
        pytest.skip(f"{name}: no refused cash payout")
    expected = canon(_reference(i))
    assert canon(_one_pass(i, first)) == expected
    assert canon(_one_pass(i, first, refused=[])) != expected, name
