"""W6c (D42) — every SERIES read is re-expressed into ONE denomination.

``price_in`` (W6b) fixes a price that meets a *share count* from another day. This package
fixes a price that meets *another price* from another day: a 52-week high/low, a moving
average, a day-change percentage, a rebased index, a chart. A stored close is as traded on
its own date (``data-and-pricing.md``), so the moment a SPLIT falls inside a window the
points are in two denominations and every comparison across it is wrong by the ratio.

The fixture below is a 7-for-1 — chosen because it is the shape that fires: on the raw
series the pre-split year reads 7x higher, the symbol sits at its 52-week low, and the alert
scan **notifies the owner** about a −86% collapse that never happened.

Each re-express site gets a pair: the corrected reading, and the pre-fix reading proved on
the real code path (``series_in`` neutered to the identity — literally the statement each
call site had before). The last test is the D38-invariant-1 containment landmine.
"""

import sqlite3
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from portfolio_dash.api import (
    alert_inputs,
    digest_service,
    dividend_inbox,
    insight_service,
    signals_service,
)
from portfolio_dash.api.routers import dashboard as dashboard_router
from portfolio_dash.api.routers import instruments as instruments_router
from portfolio_dash.api.routers import performance as performance_router
from portfolio_dash.api.routers import prompts as prompts_router
from portfolio_dash.api.routers import symbol as symbol_router
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.holdings import load_action_index
from portfolio_dash.data_ingestion.store import (
    insert_corporate_action,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.llm_insight import evaluations_store as es
from portfolio_dash.llm_insight.cards import Prediction
from portfolio_dash.portfolio import price_basis
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.portfolio.price_basis import series_in
from portfolio_dash.portfolio.technicals import week52_position
from portfolio_dash.pricing.results import DividendEvent, FxRow, PriceRead, PriceRow
from portfolio_dash.pricing.store import (
    get_price_history,
    upsert_dividend_events,
    upsert_fx,
    upsert_prices,
)
from portfolio_dash.shared.corporate_actions import ActionIndex, CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.strategy.rules.params import default_params
from tests.conftest import init_golden_base

USD = Currency.USD
TWD = Currency.TWD

NOW = datetime(2026, 6, 30, 12, 0)
SPLIT_DATE = date(2026, 6, 1)
RATIO = Decimal("7")

# As traded: 700 before the 7-for-1, 100 after it. Same economic value on both sides —
# which is exactly what a correctly denominated series must show, and what the raw one
# does not.
PRE_PRICE = Decimal("700")
POST_PRICE = Decimal("100")
FIRST_PRICED = date(2026, 4, 1)          # 61 pre-split sessions -> past _DRAWDOWN_MIN_WINDOW
LAST_PRICED = date(2026, 6, 30)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    """The FULL empty schema (``init_golden_base``) + the seeded accounts.

    These tests drive whole API routes, not one helper, so every table the app's startup
    creates must exist — otherwise a missing table masquerades as a W6c failure.
    """
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    init_golden_base(c)
    seed_accounts(c)
    yield c
    c.close()


def _daily(start: date, end: date, close: Decimal, symbol: str) -> list[PriceRow]:
    rows: list[PriceRow] = []
    day = start
    while day <= end:
        rows.append(PriceRow(instrument=symbol, market=Market.US, as_of=day,
                             close=close, source="test"))
        day += timedelta(days=1)
    return rows


def _seed_split(
    conn: sqlite3.Connection,
    symbol: str = "SPLT",
    *,
    with_split: bool = True,
    split_on: date = SPLIT_DATE,
    last_priced: date = LAST_PRICED,
    target_high: Decimal | None = None,
) -> None:
    """10 shares of *symbol* held through a 7-for-1, with a daily AS-TRADED close series.

    ``with_split=False`` seeds the identical series with NO ledger action — the control for
    the containment landmine (the prices themselves are never what triggers the correction).
    ``split_on`` moves the action within the series for the sites whose read window is not
    the full history (the watchlist's month-to-date change, the digest's 14-day lookback).
    """
    upsert_instrument(conn, Instrument(symbol=symbol, market=Market.US, quote_ccy=USD,
                                       sector="Tech", name=symbol,
                                       target_high=target_high))
    insert_transaction(conn, account_id="schwab", symbol=symbol, side=Side.BUY,
                       quantity=Decimal("10"), price=PRE_PRICE, fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 3, 2))
    if with_split:
        insert_corporate_action(conn, account_id="schwab", action_date=split_on,
                                kind=CorporateActionKind.SPLIT, from_symbol=symbol,
                                to_symbol=symbol, ratio_to=RATIO,
                                ratio_from=Decimal("1"))
    rows = _daily(FIRST_PRICED, split_on - timedelta(days=1), PRE_PRICE, symbol)
    if last_priced >= split_on:
        rows += _daily(split_on, last_priced, POST_PRICE, symbol)
    # `factor_of` defaults to the identity, so these store byte-for-byte as written: the
    # fixture supplies as-traded values directly rather than re-deriving W6a's write seam.
    upsert_prices(conn, rows, fetched_at=NOW)
    upsert_fx(conn, [FxRow(base=USD, quote=TWD, as_of=date(2026, 3, 2),
                           rate=Decimal("32"), source="test")], fetched_at=NOW)


def _identity_series(monkeypatch: pytest.MonkeyPatch, module: object) -> None:
    """Neuter ``series_in`` in *module* to the pre-W6c statement (the raw list)."""
    monkeypatch.setattr(
        module, "series_in",
        lambda index, symbol, points, *, valued_on: list(points),
    )


# --- the rule itself ------------------------------------------------------------------


def test_series_in_puts_a_split_window_into_one_denomination(
    conn: sqlite3.Connection,
) -> None:
    """The whole package in one assertion: 700-as-traded and 100-as-traded are the SAME
    level once both are read in the valuation day's share terms."""
    _seed_split(conn)
    raw = get_price_history(conn, "SPLT", FIRST_PRICED, LAST_PRICED)
    assert {p.value for p in raw} == {PRE_PRICE, POST_PRICE}   # two denominations

    fixed = series_in(load_action_index(conn), "SPLT", raw, valued_on=NOW.date())
    assert {p.value for p in fixed} == {POST_PRICE}            # one
    # Dates, volume and provenance ride along untouched; only the close is re-expressed
    # (D39b), and nothing is mutated in place.
    assert [p.as_of for p in fixed] == [p.as_of for p in raw]
    assert [p.value for p in raw] != [p.value for p in fixed]  # the input list is intact


def test_series_in_is_a_no_op_once_a_genuine_post_split_series_exists(
    conn: sqlite3.Connection,
) -> None:
    """Self-cancelling, like ``price_in``: for a point dated ON or after the split the
    window ``(pd, valued_on]`` is empty, so the stored close is used exactly as stored."""
    _seed_split(conn)
    post = get_price_history(conn, "SPLT", SPLIT_DATE, LAST_PRICED)
    fixed = series_in(load_action_index(conn), "SPLT", post, valued_on=NOW.date())
    assert [p.value for p in fixed] == [p.value for p in post]


# --- 1. api/signals_service.py — the rule engine (highest consequence) -----------------


def test_signals_52w_position_is_not_a_phantom_low(conn: sqlite3.Connection) -> None:
    _seed_split(conn)
    closes, _, _ = signals_service._read_series(
        conn, "SPLT", now=NOW, params=default_params(),
        actions=load_action_index(conn),
    )
    w52 = week52_position(closes)
    assert w52["high"] == POST_PRICE and w52["low"] == POST_PRICE
    assert w52["pct_from_high"] == Decimal("0")


def test_signals_52w_position_pre_fix_reads_the_ratio_as_a_collapse(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DETECTION POWER — the raw read on the real path. −85.7% is the 7-for-1, not a move."""
    _seed_split(conn)
    _identity_series(monkeypatch, signals_service)
    closes, _, _ = signals_service._read_series(
        conn, "SPLT", now=NOW, params=default_params(),
        actions=load_action_index(conn),
    )
    w52 = week52_position(closes)
    assert w52["high"] == PRE_PRICE and w52["low"] == POST_PRICE
    assert w52["pct_from_high"] == (POST_PRICE - PRE_PRICE) / PRE_PRICE  # -6/7


# --- 2. api/alert_inputs.py — the alert that would have been SENT ----------------------


def test_a_seven_for_one_does_not_fire_a_52_week_drawdown_alert(
    conn: sqlite3.Connection,
) -> None:
    """THE HEADLINE. A drawdown alert is pushed to the owner; a fabricated one is worse
    than no alert at all, because it trains him to ignore the real ones."""
    _seed_split(conn)
    alerts = alert_inputs.compute_alerts_full(conn, now=NOW, reporting=USD)
    assert not [a for a in alerts if a.rule == "drawdown_from_peak"]

    fed = alert_inputs.assemble(
        conn, build_dashboard(conn, now=NOW, reporting=USD), now=NOW)
    assert fed.symbol_metrics["SPLT"].pct_from_52w_high == Decimal("0")


def test_pre_fix_the_same_ledger_fires_a_risk_drawdown_alert(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_split(conn)
    _identity_series(monkeypatch, alert_inputs)
    alerts = alert_inputs.compute_alerts_full(conn, now=NOW, reporting=USD)
    fired = [a for a in alerts if a.rule == "drawdown_from_peak"]
    assert len(fired) == 1
    assert fired[0].sev == "risk"          # -85.7% is far past the 20% RISK threshold


def test_the_fed_target_price_is_in_the_SAME_denomination_as_the_fed_series(
    conn: sqlite3.Connection,
) -> None:
    """THE MIXING TRAP. ``target_cross`` reads ``get_latest_price`` while every metric
    beside it reads the re-expressed series; a stale pre-split quote against a post-split
    band crosses it by the whole ratio."""
    _seed_split(conn, last_priced=SPLIT_DATE - timedelta(days=1),
                target_high=Decimal("150"))
    fed = alert_inputs.assemble(
        conn, build_dashboard(conn, now=NOW, reporting=USD), now=NOW)
    # Latest STORED close is 700 as traded on 5/31; in 6/30's terms that is 100 — below the
    # 150 band, so no cross. Raw it is 700 and the band is breached 4.7x over.
    assert fed.target_levels["SPLT"].price == POST_PRICE
    alerts = alert_inputs.compute_alerts_full(conn, now=NOW, reporting=USD)
    assert not [a for a in alerts if a.rule == "target_cross"]


def test_pre_fix_the_stale_pre_split_quote_crosses_the_target_band(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The negative assertion above is not vacuous: the SAME ledger fires 突破 when the
    latest quote is left in its own (pre-split) denomination."""
    _seed_split(conn, last_priced=SPLIT_DATE - timedelta(days=1),
                target_high=Decimal("150"))
    monkeypatch.setattr(
        alert_inputs, "price_in",
        lambda index, symbol, price, *, priced_on, valued_on: price,
    )
    alerts = alert_inputs.compute_alerts_full(conn, now=NOW, reporting=USD)
    assert [a for a in alerts if a.rule == "target_cross"]


# --- 3. api/digest_service.py — the daily movers ---------------------------------------


def test_digest_day_change_is_not_the_split_ratio(conn: sqlite3.Connection) -> None:
    """The two closes the pct divides straddle the split: 700 -> 100 is 0%, not −86%."""
    _seed_split(conn, last_priced=SPLIT_DATE)
    now = datetime(2026, 6, 1, 20, 0)
    pct, as_of, meta = digest_service._per_symbol_day_change(conn, ["SPLT"], now=now)
    assert pct["SPLT"] == Decimal("0")
    assert as_of == SPLIT_DATE.isoformat()
    # The tooltip's close comes from the SAME re-expressed row as the pct (no mixing).
    assert meta["SPLT"][2] == "100"


def test_digest_day_change_pre_fix_reports_minus_86_percent(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_split(conn, last_priced=SPLIT_DATE)
    _identity_series(monkeypatch, digest_service)
    now = datetime(2026, 6, 1, 20, 0)
    pct, _as_of, _meta = digest_service._per_symbol_day_change(conn, ["SPLT"], now=now)
    assert pct["SPLT"] == (POST_PRICE - PRE_PRICE) / PRE_PRICE


# --- 4. api/dividend_inbox.py — a DRIP share count, money of record --------------------


def test_drip_reinvest_price_is_expressed_in_the_pay_dates_share_terms(
    conn: sqlite3.Connection,
) -> None:
    """``est_reinvest_shares = net / px`` is BOOKED into the dividend ledger. A pre-split
    close carried forward across the action books one seventh of the shares."""
    _seed_split(conn, last_priced=SPLIT_DATE - timedelta(days=1))
    upsert_dividend_events(conn, [DividendEvent(
        instrument="SPLT", market=Market.US, ex_date=date(2026, 6, 3),
        pay_date=date(2026, 6, 5), cash_amount=Decimal("7"), currency=USD,
        source="test")], fetched_at=NOW)
    pending = dividend_inbox.detect(conn, now=NOW)
    drip = next(p for p in pending if p.symbol == "SPLT" and p.kind == "drip")
    assert drip.est_reinvest_price == POST_PRICE       # 700 as traded 5/31 -> 100 on 6/5
    assert drip.est_reinvest_shares == drip.est_net / POST_PRICE


def test_drip_reinvest_price_pre_fix_is_the_pre_split_close(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_split(conn, last_priced=SPLIT_DATE - timedelta(days=1))
    upsert_dividend_events(conn, [DividendEvent(
        instrument="SPLT", market=Market.US, ex_date=date(2026, 6, 3),
        pay_date=date(2026, 6, 5), cash_amount=Decimal("7"), currency=USD,
        source="test")], fetched_at=NOW)
    _identity_series(monkeypatch, dividend_inbox)
    drip = next(p for p in dividend_inbox.detect(conn, now=NOW)
                if p.symbol == "SPLT" and p.kind == "drip")
    assert drip.est_reinvest_price == PRE_PRICE        # 7x too high -> 1/7 of the shares


# --- 5. api/insight_service.py — the evaluation window + the model's context -----------


def _due(symbol: str, *, created: date, due: date, price_at_create: str | None) -> es.DueInsight:
    return es.DueInsight(
        insight_id=1, insight_type_id=1, symbol=symbol, calibration_version=None,
        is_shadow=False, confidence=60,
        prediction='{"metric":"price_change","direction":"up","horizon_days":30}',
        due_at=datetime.combine(due, datetime.min.time()).isoformat(),
        created_at=datetime.combine(created, datetime.min.time()).isoformat(),
        price_at_create=price_at_create,
    )


PRED = Prediction(metric="price_change", direction="up", horizon_days=30)


def test_a_scored_card_is_not_marked_wrong_by_a_split(conn: sqlite3.Connection) -> None:
    """The card was created pre-split and matures post-split. Raw, it scores a −86%
    collapse — a fabricated miss on the model's permanent record."""
    _seed_split(conn)
    actual = insight_service._measure_actual(
        conn, _due("SPLT", created=date(2026, 5, 1), due=date(2026, 6, 20),
                   price_at_create=None),
        PRED, actions=load_action_index(conn))
    assert actual is not None
    assert actual.price_change_pct == Decimal("0")


def test_price_at_create_is_re_expressed_on_the_SAME_basis_as_the_fetched_leg(
    conn: sqlite3.Connection,
) -> None:
    """MIXING TRAP, the ledger-scalar form: ``price_at_create`` is a stored close from the
    card's own date. Correcting only the end leg would MANUFACTURE the discrepancy."""
    _seed_split(conn)
    actual = insight_service._measure_actual(
        conn, _due("SPLT", created=date(2026, 5, 1), due=date(2026, 6, 20),
                   price_at_create="700"),
        PRED, actions=load_action_index(conn))
    assert actual is not None
    assert actual.price_change_pct == Decimal("0")


def test_a_scored_card_pre_fix_reads_the_ratio_as_a_collapse(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_split(conn)
    _identity_series(monkeypatch, insight_service)
    monkeypatch.setattr(
        insight_service, "price_in",
        lambda index, symbol, price, *, priced_on, valued_on: price,
    )
    actual = insight_service._measure_actual(
        conn, _due("SPLT", created=date(2026, 5, 1), due=date(2026, 6, 20),
                   price_at_create=None),
        PRED, actions=load_action_index(conn))
    assert actual is not None
    assert actual.price_change_pct == (POST_PRICE - PRE_PRICE) / PRE_PRICE


def test_the_model_is_shown_one_denomination(conn: sqlite3.Connection) -> None:
    """``ctx.closes`` feeds the technical signals and ``ctx.price_points`` is the price path
    rendered into the prompt — both must be the series the dashboard's own price agrees
    with."""
    _seed_split(conn)
    data = build_dashboard(conn, now=NOW, reporting=USD)
    ctx = insight_service._per_symbol_ctx(
        conn, data, "SPLT", now=NOW, reporting=USD, actions=load_action_index(conn))
    assert set(ctx.closes or []) == {POST_PRICE}
    assert {p["close"] for p in (ctx.price_points or [])} == {"100"}


# --- 6. api/routers/prompts.py — the preview must match the run path -------------------


def test_the_prompt_preview_renders_the_same_series_the_run_will(
    conn: sqlite3.Connection,
) -> None:
    _seed_split(conn)
    body = prompts_router.PromptBody(body="{{price_history_json}}", scope="per_symbol",
                                     symbol="SPLT")
    ctx = prompts_router._build_context(conn, body, NOW, USD)
    assert set(ctx.closes or []) == {POST_PRICE}
    run_ctx = insight_service._per_symbol_ctx(
        conn, build_dashboard(conn, now=NOW, reporting=USD), "SPLT",
        now=NOW, reporting=USD, actions=load_action_index(conn))
    assert ctx.closes == run_ctx.closes


# --- 7. api/routers/dashboard.py — spark_30d beside the holding's own price ------------


def test_the_sparkline_agrees_with_the_price_printed_next_to_it(
    conn: sqlite3.Connection,
) -> None:
    """MIXING TRAP, the visible form: ``market_price`` already went through W6b's
    ``price_map`` seam. A raw sparkline draws a 7x cliff beside a price that has none."""
    _seed_split(conn)
    payload = dashboard_router.dashboard(trend_days=90, conn=conn, now=NOW, reporting=USD)
    row = next(r for r in payload["holdings"] if r["symbol"] == "SPLT")
    assert set(row["spark_30d"]) == {"100"}
    assert row["spark_30d"][-1] == row["market_price"]


def test_the_sparkline_pre_fix_disagrees_with_that_price_by_the_ratio(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The newest stored close predates the split, so the sparkline's last point is the
    as-traded 700 while the KPI beside it is already 100 — a 7x disagreement on one row."""
    _seed_split(conn, last_priced=SPLIT_DATE - timedelta(days=1))
    _identity_series(monkeypatch, dashboard_router)
    payload = dashboard_router.dashboard(trend_days=90, conn=conn, now=NOW, reporting=USD)
    row = next(r for r in payload["holdings"] if r["symbol"] == "SPLT")
    assert row["spark_30d"][-1] == "700"          # raw
    assert row["market_price"] == "100"           # already corrected at the W6b seam


# --- 8. api/routers/instruments.py — the watchlist day change --------------------------

# The watchlist's `chg_pct` window is MONTH-TO-DATE, so the split has to fall inside that
# month for the defect to be reachable at all — mid-June, with the newest close ON the
# action date (the last session whose predecessor is still pre-split).
_MID = date(2026, 6, 15)
_MID_NOW = datetime(2026, 6, 15, 20, 0)


def test_the_watchlist_row_reports_no_move_across_a_split(
    conn: sqlite3.Connection,
) -> None:
    _seed_split(conn, split_on=_MID, last_priced=_MID)
    element = next(e for e in instruments_router.list_all(conn=conn, now=_MID_NOW)["list"]
                   if e["symbol"] == "SPLT")
    assert element["chg_pct"] == "0"
    assert element["last"] == "100"


def test_the_watchlist_row_pre_fix_reports_minus_86_percent(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_split(conn, split_on=_MID, last_priced=_MID)
    _identity_series(monkeypatch, instruments_router)
    element = next(e for e in instruments_router.list_all(conn=conn, now=_MID_NOW)["list"]
                   if e["symbol"] == "SPLT")
    assert Decimal(element["chg_pct"]) == (POST_PRICE - PRE_PRICE) / PRE_PRICE


# --- 9. api/routers/symbol.py — the drawer chart vs its own cost line ------------------


def test_the_drawer_chart_shares_a_denomination_with_its_cost_line(
    conn: sqlite3.Connection,
) -> None:
    """The chart draws horizontal mark-lines at ``original_avg`` / ``adjusted_avg``, which
    are ``total / shares`` over the replay's ALREADY re-denominated share count. A raw
    series puts the pre-split part of the line a whole ratio away from its own cost line."""
    _seed_split(conn)
    detail = symbol_router.symbol_detail(symbol="SPLT", days=180, conn=conn, now=NOW,
                                         reporting=USD)
    assert {p["close"] for p in detail["price_history"]["points"]} == {"100"}
    assert detail["cost_basis"]["original_avg"] == "100"   # 7,000 / 70 shares


def test_the_drawer_chart_pre_fix_diverges_from_the_cost_line_by_the_ratio(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_split(conn)
    _identity_series(monkeypatch, symbol_router)
    detail = symbol_router.symbol_detail(symbol="SPLT", days=180, conn=conn, now=NOW,
                                         reporting=USD)
    assert "700" in {p["close"] for p in detail["price_history"]["points"]}
    assert detail["cost_basis"]["original_avg"] == "100"   # the cost line does not move


def test_the_drawer_builds_exactly_one_action_index(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trap #21 on the one route that already had an index: the chart REUSES it rather than
    loading a second one. ``_reconcile`` runs 1 + n_accounts times, so a per-call load was
    the original defect here."""
    _seed_split(conn)
    calls: list[int] = []

    def _counting(c: sqlite3.Connection) -> ActionIndex:
        calls.append(1)
        return load_action_index(c)

    monkeypatch.setattr(symbol_router, "load_action_index", _counting)
    symbol_router.symbol_detail(symbol="SPLT", days=180, conn=conn, now=NOW, reporting=USD)
    assert len(calls) == 1


# --- 10. api/routers/performance.py — the benchmark, and its honest limit --------------


def test_the_benchmark_series_is_re_expressed_when_an_action_IS_recorded(
    conn: sqlite3.Connection,
) -> None:
    """The overlay rebases to 100 at the window start, so every point is divided by an
    earlier one — a raw split shows as a single-day index cliff of the whole ratio."""
    _seed_split(conn)                       # the portfolio side
    _seed_split(conn, symbol="0050")        # a benchmark that HAS a recorded action
    out = performance_router.performance_twr(
        benchmark="0050", window="1y", conn=conn, now=NOW, reporting=USD)
    assert out["available"] is True
    benches = {p["benchmark"] for p in out["points"]}
    assert benches == {"100.0000"}           # flat: no phantom cliff


def test_a_benchmark_with_no_recorded_action_is_NOT_re_expressed_documented_limit(
    conn: sqlite3.Connection,
) -> None:
    """THE HONEST LIMITATION, pinned as a test so it is a known state and not a surprise.

    ``ActionIndex`` is built from the owner's RECORDED ledger actions. A benchmark is
    deliberately not a registered instrument — nobody holds it, so nobody records its
    splits — and there is no independent split feed. Inferring one from a price gap would
    be guessing (``domain-ledger.md``), so the cliff below is *left visible* rather than
    silently rewritten.
    """
    _seed_split(conn)
    _seed_split(conn, symbol="0050", with_split=False)   # prices split, ledger silent
    out = performance_router.performance_twr(
        benchmark="0050", window="1y", conn=conn, now=NOW, reporting=USD)
    assert out["available"] is True
    assert len({p["benchmark"] for p in out["points"]}) > 1   # the cliff survives


# --- 11. the reads that stay RAW ------------------------------------------------------


def test_the_existence_checks_stay_raw(conn: sqlite3.Connection) -> None:
    """``_has_no_history`` asks whether a row is THERE. A corporate action changes a
    price's denomination, never its existence — routing this through ``series_in`` would
    add a query and an index for no answer."""
    _seed_split(conn)
    assert insight_service._has_no_history(conn, "NOPE", NOW.date()) is True
    assert insight_service._has_no_history(conn, "SPLT", NOW.date()) is False


def test_the_trend_bulk_load_stays_raw_because_its_consumer_re_expresses_per_day(
    conn: sqlite3.Connection,
) -> None:
    """``portfolio/dashboard.py`` hands its bulk history to ``daily_value_series``, which
    values a DIFFERENT day each iteration and applies §5.1(d) itself at the lookup with
    ``valued_on=day``. Re-expressing at the bulk load too would divide by the ratio twice —
    so the trend is the proof that it does not.

    70 shares x 100 = 7,000 on BOTH sides of the split (the conservation law, §2.1).
    """
    _seed_split(conn)
    data = build_dashboard(conn, now=NOW, reporting=USD)
    assert data.trend.available
    pts = {p.date: p for p in data.trend.points}
    before, after = pts[date(2026, 5, 31)], pts[SPLIT_DATE]
    assert not before.incomplete and not after.incomplete
    assert before.total_value == after.total_value == Decimal("7000")


# --- 12. D38 invariant 1 — structural containment --------------------------------------


def test_an_action_free_ledger_never_executes_the_new_series_code(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HEADLINE ACCEPTANCE, W6c's half (owner requirement 2026-08-10, D38 invariant 1).

    A symbol with no corporate action must behave exactly as it did before W6c existed.
    Proved STRUCTURALLY — a landmine on ``split_factor``, the one function the new
    arithmetic reaches — rather than by equality of results: code that does not run cannot
    drift, while code that happens to compute an equal answer can. ``series_in``
    short-circuits on ``index.splits_on(symbol)`` being empty and returns the caller's own
    rows, so with an action-free ledger the loop below never runs at all.

    Every re-express site is driven, so the containment claim covers the whole package and
    not just the site the author happened to think of.
    """
    def _boom(*a: object, **k: object) -> None:
        raise AssertionError("W6c series code ran on an action-free ledger")

    _seed_split(conn, with_split=False)
    monkeypatch.setattr(price_basis, "split_factor", _boom)

    data = build_dashboard(conn, now=NOW, reporting=USD)
    alert_inputs.assemble(conn, data, now=NOW)
    alert_inputs.compute_alerts_full(conn, now=NOW, reporting=USD)
    digest_service._per_symbol_day_change(conn, ["SPLT"], now=NOW)
    dividend_inbox.detect(conn, now=NOW)
    insight_service._per_symbol_ctx(conn, data, "SPLT", now=NOW, reporting=USD,
                                    actions=load_action_index(conn))
    insight_service._measure_actual(
        conn, _due("SPLT", created=date(2026, 5, 1), due=date(2026, 6, 20),
                   price_at_create="700"),
        PRED, actions=load_action_index(conn))
    signals_service._read_series(conn, "SPLT", now=NOW,
                                 params=default_params(),
                                 actions=load_action_index(conn))
    dashboard_router.dashboard(trend_days=90, conn=conn, now=NOW, reporting=USD)
    instruments_router.list_all(conn=conn, now=NOW)
    symbol_router.symbol_detail(symbol="SPLT", days=180, conn=conn, now=NOW, reporting=USD)
    prompts_router._build_context(
        conn, prompts_router.PromptBody(body="{{price_history_json}}",
                                        scope="per_symbol", symbol="SPLT"),
        NOW, USD)
    _seed_split(conn, symbol="0050", with_split=False)
    performance_router.performance_twr(benchmark="0050", window="1y", conn=conn, now=NOW,
                                       reporting=USD)


def test_series_in_returns_the_callers_own_rows_when_there_is_no_split() -> None:
    """The short-circuit at its own level: identical objects out, not equal copies — the
    cheapest possible proof that nothing was recomputed."""
    index = ActionIndex.build([])
    points = [PriceRead(value=Decimal("123.4"), as_of=date(2026, 6, 1), source="test",
                        stale=False)]
    out = series_in(index, "NOSPLIT", points, valued_on=date(2026, 6, 30))
    assert out[0] is points[0]
