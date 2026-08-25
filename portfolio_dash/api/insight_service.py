"""Insight generation service (spec 04b) — the conn-bearing seam over pure llm_insight.

This is the ONLY place that reads ``pricing`` / ``portfolio`` to feed an insight run, so
``llm_insight.generate`` stays pure (architecture.md; same precedent as 06a's
``api/routers/prompts.py`` ``_build_context``). It:

1. resolves an insight_type's universe (per_symbol: ``mode:all`` → current holdings,
   ``mode:custom`` → the listed symbols; portfolio/on_alert → a single target);
2. builds one :class:`~llm_insight.variables.VarContext` per target from the REAL computed
   dashboard + per-symbol price history + external snapshots + fx (reusing the 06a
   per-variable assembly helpers);
3. computes the fed gate inputs (budget remaining, master-role configured, per-symbol
   missing prices, removed symbols);
4. delegates to the pure ``generate.run_insight_type``.

``run_for_id`` is the function the scheduler's insight runner and the manual-run endpoint
call (wired via ``scheduler.register_insight_runner`` at app startup — no scheduler→api
import).
"""

import json
import logging
import math
import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from portfolio_dash.api.routers.prompts import (
    _HISTORY_DAYS,
    _TECHNICAL_HISTORY_DAYS,
    _dividend_rows,
    _external_reasons,
    _external_vars,
    _resolve_fx_rates,
)
from portfolio_dash.data_ingestion.holdings import load_action_index
from portfolio_dash.data_ingestion.store import list_instruments
from portfolio_dash.llm_insight import (
    alerts_bridge,
    assemble,
    gating,
    generate,
    master,
    promote,
    scoring,
)
from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.llm_insight import evaluations_store as es
from portfolio_dash.llm_insight import insights_store as istore
from portfolio_dash.llm_insight import pipeline_status as ps
from portfolio_dash.llm_insight import variables as V
from portfolio_dash.llm_insight.cards import Prediction
from portfolio_dash.llm_insight.gating import GateContext, GateResult
from portfolio_dash.llm_insight.generate import RunInputs, RunResult, run_insight_type
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.portfolio.dashboard_models import DashboardData, FreshnessReport
from portfolio_dash.portfolio.price_basis import price_in, series_in
from portfolio_dash.portfolio.technicals import annualized_volatility
from portfolio_dash.portfolio.twr import twr_index
from portfolio_dash.pricing.benchmarks import Benchmark, benchmark_for_market
from portfolio_dash.pricing.store import get_price_history
from portfolio_dash.scheduler.jobs import insight_job_id
from portfolio_dash.shared.corporate_actions import ActionIndex
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.llm_config import (
    LLMError,
    LLMRole,
    budget_remaining,
    get_alert_threshold,
    get_role_model_id,
)
from portfolio_dash.shared.llm_config import get_model as llm_config_get_model
from portfolio_dash.shared.wire import decimal_str

logger = logging.getLogger(__name__)

# _HISTORY_DAYS / _TECHNICAL_HISTORY_DAYS are imported from the prompts router (L3 fix):
# ONE definition, so the 06 preview and the 04 run path can never drift apart again.

# A pure-narrative card (or a quant card whose narrative is the deciding signal) is a "miss"
# when the master narrative score is below this threshold (spec 4.4 / decide_miss).
_NARRATIVE_MISS_THRESHOLD = 60
# How far back to look for the create-time / due-time price closes when building the actual.
_EVAL_LOOKBACK_DAYS = 14
# AI-D24: the volatility metric's fixed measurement window (trading days) — the same
# estimator length the alert inputs use (``api/alert_inputs.py`` vol_30d). Fixed, NOT the
# prediction's horizon: a 3-day on_alert horizon would otherwise be a 3-return noise
# reading. The lookback covers the create-side window's 31 closes: 30 trading days ≈ 42
# calendar days + long-holiday cushion.
_VOL_WINDOW = 30
_VOL_LOOKBACK_DAYS = 75


def calibration_gap(conn: sqlite3.Connection) -> Decimal | None:
    """Portfolio-wide AI calibration error in PERCENTAGE POINTS, or None below the gate.

    The SINGLE source for the ``calib_gap`` alert rule (spec 03/04 I1): collects the active
    scored ``(confidence, hit)`` pairs, gates on the GLOBAL ``min_samples`` (portfolio-wide,
    NOT a per-combo ``resolved_sample_count``) so a small sample never fires, and returns
    ``scoring.calibration_error`` (pp) when the gate passes. Returns None below the gate so
    the dashboard / alerts degrade silently. Note: ``evolution_config['gap_alert_pp']`` is
    the SEPARATE spec-04c calibration-regression EVENT threshold — only ``min_samples`` is
    read here; the calib_gap rule threshold lives in ``AlertRules.calib_gap.value``.
    """
    rows = es.scored_confidence_hits(conn)
    min_samples = int(cs.get_evolution_config(conn)["min_samples"])
    if len(rows) < min_samples:
        return None
    return scoring.calibration_error(rows)


def _resolve_markets(data: DashboardData) -> list[str]:
    """The per_market universe: the sorted market codes among current holdings.

    Holdings carry their market directly (HoldingRow.market); an emptied market simply
    stops appearing — its card is not produced (spec: 自動跟隨持有市場).
    """
    return sorted({h.market.value for h in data.holdings})


def _all_registered_symbols(conn: sqlite3.Connection) -> list[str]:
    """Every registered instrument symbol (held + watchlist) — the opt-in ``all_registered``
    universe (P2 batch 3). Watch symbols are entry candidates, so a checkup task MAY analyse
    them — but only when the user explicitly opts in (each is LLM cost; default stays holds)."""
    return sorted({i.symbol for i in list_instruments(conn)})


def _resolve_universe(
    conn: sqlite3.Connection, it: cs.InsightType, data: DashboardData
) -> list[str]:
    """The per_symbol universe: custom list, all current holdings (``mode:all``, the
    default), or holdings + watchlist (``mode:all_registered`` — explicit opt-in)."""
    held = sorted({h.symbol for h in data.holdings})
    universe = it.universe
    if isinstance(universe, dict):
        mode = universe.get("mode")
        if mode == "custom":
            syms = universe.get("symbols")
            return list(syms) if isinstance(syms, list) else []
        if mode == "all_registered":
            return _all_registered_symbols(conn)
        if mode == "all":
            return held
    return held  # default: follow holdings



def _unavailable_across(var_contexts: dict[Any, Any]) -> list[str]:
    """Union of the injected variables that came back "no data", over every context in a run.

    R5 is a per-RUN gate while unavailability is per-symbol-per-token, so a union is the only
    honest reduction: if ANY symbol in the batch was synthesised without its news / consensus /
    fundamentals, the run was degraded. Intersecting would report a clean run whenever a single
    symbol happened to have complete data.

    This is the read that was missing (2026-08-25). ``RunInputs.unavailable_vars`` was threaded
    all the way to the gate and set at NONE of the four construction sites, so R5 had never
    fired: a run built entirely from absent inputs was recorded as clean.
    """
    seen: set[str] = set()
    for ctx in var_contexts.values():
        seen.update(V.unavailable_tokens(getattr(ctx, "external_vars", {}) or {}))
    return sorted(seen)


def _per_symbol_ctx(
    conn: sqlite3.Connection,
    data: DashboardData,
    symbol: str,
    *,
    now: datetime,
    reporting: Currency,
    actions: ActionIndex,
) -> V.VarContext:
    """Build a per-symbol VarContext (dashboard + history + external snapshots + fx).

    ``actions`` is built ONCE by the caller and threaded in (trap #21) — this function runs
    once per universe symbol, so building the index here would re-read the action ledger
    once per card.
    """
    external_vars = _external_vars(conn, symbol, now=now, actions=actions)
    ctx = V.VarContext(
        data=data,
        symbol=symbol,
        now=now,
        fx_rates=_resolve_fx_rates(conn, data, now, reporting),
        dividend_rows=_dividend_rows(conn),
        external_vars=external_vars,
        external_reasons=_external_reasons(conn, external_vars),
    )
    as_of = now.date()
    # SR fix (2026-07-06): the technical signals (52-week position, MA120) need up to ~252
    # trading sessions, but 180 CALENDAR days ≈ 123 sessions — so fetch a longer close
    # series (the backfill already stores 365d) for ctx.closes, while price_history_json
    # keeps only the recent 180d window (then downsampled) to stay token-bounded.
    # §5.1(d) / W6c: every consumer of this series compares points ACROSS dates — the
    # 52-week position and MA120 read the whole window, and `price_points` is handed to the
    # model as a price path. A split inside it puts the pre-split points in a different
    # denomination, so the model is shown a phantom −86% cliff (7-for-1) and the technical
    # signals fire on it. Re-expressed into `as_of`, which is also the day `data`'s holding
    # prices were re-expressed into at the `price_map` seam (W6b) — one denomination for the
    # whole context. ⚠ `volume` is NOT re-expressed (D39b: it has no raw column).
    long_hist = series_in(
        actions, symbol,
        get_price_history(
            conn, symbol, as_of - timedelta(days=_TECHNICAL_HISTORY_DAYS), as_of
        ),
        valued_on=as_of,
    )
    ctx.closes = [p.value for p in long_hist]
    # Volumes aligned 1:1 with closes; fed only when at least one session has volume
    # (probe gate), so the technical volume signal stays honestly absent pre-backfill.
    vols = [p.volume for p in long_hist]
    ctx.volumes = vols if any(v is not None for v in vols) else None
    recent = [p for p in long_hist if p.as_of >= as_of - timedelta(days=_HISTORY_DAYS)]
    ctx.price_points = [
        {"date": p.as_of.isoformat(), "close": decimal_str(p.value)} for p in recent
    ]
    return ctx


def _portfolio_ctx(
    conn: sqlite3.Connection, data: DashboardData, *, now: datetime, reporting: Currency
) -> V.VarContext:
    """Build the portfolio-scope VarContext (no per-symbol detail)."""
    external_vars = _external_vars(conn, None, actions=None)
    return V.VarContext(
        data=data,
        now=now,
        fx_rates=_resolve_fx_rates(conn, data, now, reporting),
        dividend_rows=_dividend_rows(conn),
        external_vars=external_vars,
        external_reasons=_external_reasons(conn, external_vars),
    )


def run_for_id(
    conn: sqlite3.Connection,
    insight_type_id: int,
    *,
    now: datetime,
    reporting: Currency = Currency.TWD,
    fired_rule: str | None = None,
    fired_symbol: str | None = None,
    is_shadow: bool = False,
    run_id: int | None = None,
) -> RunResult:
    """Load conn-bearing inputs and run one insight_type generation (the api seam).

    Builds the per-target VarContexts + the fed gate inputs, then delegates to the pure
    ``generate.run_insight_type``. ``fired_rule``/``fired_symbol`` are set for an on_alert
    dispatch (R7). ``run_id`` finalizes a pre-inserted running row (async manual run).
    Returns the run result.

    H2 fix (decision Q2a): a DISABLED or ARCHIVED task is enforced HERE, at the execution
    seam every trigger path (cron dispatch, manual run, on_alert) flows through — the run
    is skipped and recorded as a ``job_runs`` skip (reason ``task_disabled`` /
    ``task_archived``), never generated or billed. Preflight G0's "won't execute" promise
    and the runtime now agree.
    """
    it = cs.get_insight_type(conn, insight_type_id)
    if it is None:
        return run_insight_type(
            conn, insight_type_id, var_contexts={}, inputs=RunInputs(
                budget_remaining=budget_remaining(conn)
            ), now=now, run_id=run_id,
        )
    if it.archived or not it.enabled:
        reason = "task_archived" if it.archived else "task_disabled"
        detail = "任務已刪除，未執行" if it.archived else "任務已停用，未執行"
        generate._write_job_run(
            conn, insight_type_id, status="skipped", reason=reason, detail=detail,
            cost=Decimal("0"), now=now, run_id=run_id, is_shadow=is_shadow,
        )
        return RunResult(
            status="skipped", reason=reason, cards_created=0, cost_usd=Decimal("0")
        )

    data = build_dashboard(conn, now=now, reporting=reporting)
    master_configured = get_role_model_id(conn, LLMRole.MASTER) is not None
    missing_prices = list(data.freshness.missing_prices)

    var_contexts: dict[str | None, V.VarContext] = {}
    universe_symbols: list[str] = []
    # ONE ActionIndex for the whole run (trap #21), built here and threaded into every
    # per-symbol context below.
    actions = load_action_index(conn)

    if it.scope == "per_symbol":
        universe_symbols = _resolve_universe(conn, it, data)
        for sym in universe_symbols:
            ctx = _per_symbol_ctx(conn, data, sym, now=now, reporting=reporting,
                                  actions=actions)
            var_contexts[sym] = ctx
            # R4: a universe symbol with no price history at all (e.g. a custom-list symbol
            # not in the holdings/prices) is a missing-price anomaly → zero-LLM card.
            if not ctx.closes and sym not in missing_prices:
                missing_prices.append(sym)
    elif it.scope == "per_market":
        # One card per HELD market (2026-07-05 spec): the market code is the R8 target
        # key AND the card's symbol column; the VarContext.market filter guarantees the
        # card's inputs contain only that market's slice.
        universe_symbols = _resolve_markets(data)
        for mk in universe_symbols:
            ctx = _portfolio_ctx(conn, data, now=now, reporting=reporting)
            ctx.market = mk
            var_contexts[mk] = ctx
    elif it.scope == "on_alert":
        target = fired_symbol
        if target is not None:
            var_contexts[target] = _per_symbol_ctx(
                conn, data, target, now=now, reporting=reporting, actions=actions
            )
        else:
            var_contexts[None] = _portfolio_ctx(conn, data, now=now, reporting=reporting)
    else:  # portfolio
        var_contexts[None] = _portfolio_ctx(conn, data, now=now, reporting=reporting)

    inputs = RunInputs(
        budget_remaining=budget_remaining(conn),
        master_configured=master_configured,
        universe_symbols=universe_symbols,
        missing_price_symbols=missing_prices,
        # R5 (2026-08-25): the field the gate reads, finally populated. See
        # _unavailable_across for why a union rather than an intersection.
        unavailable_vars=_unavailable_across(var_contexts),
        is_shadow=is_shadow,
        fired_rule=fired_rule,
        fired_symbol=fired_symbol,
    )
    result = run_insight_type(
        conn, insight_type_id, var_contexts=var_contexts, inputs=inputs, now=now,
        run_id=run_id,
    )
    # Loop 4 (spec 4.6): if a shadow calibration version exists, also produce the hidden
    # shadow cards in the same batch (unless this run is itself a shadow / on_alert opt-out).
    if not is_shadow:
        _maybe_run_shadow(
            conn, it, var_contexts=var_contexts, base_inputs=inputs, now=now,
        )
    return result


def _shadow_card_count(conn: sqlite3.Connection, insight_type_id: int) -> int:
    """Current number of stored shadow cards for an insight_type (the max_shadows cap)."""
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM insights WHERE insight_type_id = ? AND is_shadow = 1",
        (insight_type_id,),
    ).fetchone()
    return int(row["c"]) if row is not None else 0


def _maybe_run_shadow(
    conn: sqlite3.Connection,
    it: cs.InsightType,
    *,
    var_contexts: dict[str | None, V.VarContext],
    base_inputs: RunInputs,
    now: datetime,
) -> None:
    """Generate the SHADOW cards alongside the active run when a shadow version exists.

    No shadow when: the active version is the latest (no shadow); the combo is on_alert and
    ``shadow_on_alert`` is off; or the max_shadows cap is reached (queued — skip this run).
    """
    if not it.self_correct:
        return
    cfg = cs.get_evolution_config(conn)
    if it.scope == "on_alert" and not bool(cfg["shadow_on_alert"]):
        return
    versions = cs.list_calibrations(conn, it.id)
    latest = versions[-1].version if versions else None
    shadow_v = promote.shadow_version(
        active_version=it.active_calibration_version, latest_version=latest
    )
    if shadow_v is None:
        return
    if _shadow_card_count(conn, it.id) >= int(str(cfg["max_shadows"])):
        return  # cap reached → queue (skip this batch)
    shadow_inputs = base_inputs.model_copy(
        update={
            "is_shadow": True,
            "calibration_version_override": shadow_v,
            "budget_remaining": budget_remaining(conn),
        }
    )
    run_insight_type(
        conn, it.id, var_contexts=var_contexts, inputs=shadow_inputs, now=now,
    )


# --- Loop 2: evaluate due insights (spec 04.4) --------------------------------
# The conn-bearing PRICE reads for quant verification live HERE (api MAY import pricing);
# the actual measurement is fed INTO the pure ``scoring.score_quant``. Master narrative
# scoring goes through the pure ``llm_insight.master``. This is the registered evaluate
# runner (``scheduler.register_evaluation_runner`` at startup — no scheduler→api import).


def _price_on_or_after(
    conn: sqlite3.Connection, symbol: str, on: date, *,
    actions: ActionIndex, valued_on: date,
) -> Decimal | None:
    """The first stored close on/after *on*, expressed in ``valued_on``'s share terms."""
    series = series_in(
        actions, symbol,
        get_price_history(conn, symbol, on, on + timedelta(days=_EVAL_LOOKBACK_DAYS)),
        valued_on=valued_on,
    )
    return series[0].value if series else None


def _price_on_or_before(
    conn: sqlite3.Connection, symbol: str, on: date, *,
    actions: ActionIndex, valued_on: date,
) -> Decimal | None:
    """The last stored close on/before *on*, expressed in ``valued_on``'s share terms."""
    series = series_in(
        actions, symbol,
        get_price_history(conn, symbol, on - timedelta(days=_EVAL_LOOKBACK_DAYS), on),
        valued_on=valued_on,
    )
    return series[-1].value if series else None


def _benchmark_for_symbol(conn: sqlite3.Connection, symbol: str) -> Benchmark | None:
    """The fixed benchmark for the symbol's market (AI-D22), or ``None`` when unscorable.

    Reads the ``instruments`` table (a ``data_ingestion`` table) directly — the established
    cross-layer SQL convention (architecture.md): one shared connection, the coupling named
    here, and a missing table/row degrades to ``None`` rather than raising. ``None`` covers
    both an unregistered/removed symbol and a market with no wired benchmark (MY) — the
    caller lands both as honest ``pending_data``, never a guessed proxy (an MYR stock vs a
    USD index is noise, per the ruling).
    """
    try:
        row = conn.execute(
            "SELECT market FROM instruments WHERE symbol = ?", (symbol,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None  # no instruments table → no market → honestly unscorable
    if row is None:
        return None
    return benchmark_for_market(Market(row[0]))


def _window_return(
    closes: list[tuple[date, Decimal]], start: date, end: date
) -> Decimal | None:
    """Fractional return ``start``→``end`` over an ASCENDING stored close series, or None.

    The start leg is the first close ON/AFTER ``start`` (within ``+_EVAL_LOOKBACK_DAYS``),
    the end leg the last close ON/BEFORE ``end`` — the same tolerance the price_change arms
    use, so a benchmark weekend/holiday never fabricates a measurement gap. A zero start
    leg → ``None``. Pure: the caller fetches the series (AI-D22/23 — used for the
    benchmark leg of a ``relative`` prediction; both legs stay in their local currency,
    per-market identical by construction).
    """
    lo = [c for d, c in closes
          if start <= d <= start + timedelta(days=_EVAL_LOOKBACK_DAYS)]
    hi = [c for d, c in closes
          if end - timedelta(days=_EVAL_LOOKBACK_DAYS) <= d <= end]
    if not lo or not hi or lo[0] == Decimal("0"):
        return None
    return (hi[-1] - lo[0]) / lo[0]


def _portfolio_return(
    conn: sqlite3.Connection, created: date, due_date: date,
    *, now: datetime, reporting: Currency,
) -> Decimal | None:
    """The portfolio's create→due return from the chain-linked TWR index (W7, AI-D35).

    Flow-adjusted by construction: a mid-window deposit/withdrawal moves
    ``net_invested``, not the return (the day's flow is removed from the day's value
    change), so injected cash never reads as a gain. The series is the SAME
    ``daily_value_series`` the dashboard trend card plots (the performance router's
    ``twr_index`` feed, `routers/performance.py:93`), in the reporting currency. There is
    no stored portfolio baseline analogous to ``price_at_create`` — the start leg is the
    first index point ON/AFTER ``created`` (the honest on-or-after basis, the same
    tolerance as the per-symbol legs). A thin/unavailable trend → ``None``
    (pending_data, never a fabricated miss). Built per card, deliberately not hoisted:
    a daily pass matures at most a handful of portfolio-scope cards (trap #21 is about
    per-symbol loops).
    """
    data = build_dashboard(conn, now=now, reporting=reporting)
    if not data.trend.available:
        return None
    index = twr_index(data.trend.points)
    if not index:
        return None
    # The index ratio IS the window's TWR (chain-linked returns compound); the legs ride
    # the same ±_EVAL_LOOKBACK_DAYS tolerance as the per-symbol arms.
    return _window_return([(p.date, p.value) for p in index], created, due_date)


#: How many of the symbol's own one-sigma horizon moves count as 「持平」 (review ⑩).
#: 0.5 puts the three directions at roughly 38 / 31 / 31 on a normal move, against the
#: 6 / 51 / 51 the fixed ±0.5% band produced over a 14-day horizon at 30% vol.
_FLAT_BAND_SIGMAS = Decimal("0.5")
#: Trading days per year — the same annualiser ``annualized_volatility`` uses, so the
#: de-annualisation below is in its own units. Calendar days would be a units error.
_TRADING_DAYS_PER_YEAR = Decimal("252")


def _flat_band(
    closes: list[tuple[date, Decimal]], created: date, due: date
) -> Decimal | None:
    """The 「持平」 band for THIS card: half the symbol's own one-sigma move over the horizon.

    A fixed ±0.5% band is not a statement about the market, it is a statement about a number
    someone picked — and over any real horizon it made 「持平」 nearly unhittable while up/down
    stayed near even (review ⑩; AI-D25 already fixed the identical bias for volatility).

    The horizon is counted in TRADING DAYS from the price series itself rather than from the
    calendar: ``annualized_volatility`` annualises with 252, so a calendar-day horizon would
    silently mix two day-counts. Counting the closes is also exact through holidays.

    ``None`` when the baseline volatility is unavailable or zero, or the window contains no
    trading days — the caller then falls back to the fixed band rather than to a guess.
    """
    vol = annualized_volatility([c for d, c in closes if d <= created], window=_VOL_WINDOW)
    if vol is None or vol <= Decimal("0"):
        return None
    sessions = sum(1 for d, _ in closes if created < d <= due)
    if sessions <= 0:
        return None
    horizon_sigma = vol * (Decimal(sessions) / _TRADING_DAYS_PER_YEAR).sqrt()
    return _FLAT_BAND_SIGMAS * horizon_sigma


def _vol_change_pct(
    closes: list[tuple[date, Decimal]], created: date, due: date
) -> Decimal | None:
    """Fractional change in realized volatility, create→due (AI-D24), or ``None``.

    Two fixed ``_VOL_WINDOW``-trading-day windows of ``annualized_volatility`` — one ending
    at the create date (the baseline the model saw), one at the due date. The series MUST
    already be re-expressed into one share basis: a split inside the window would read as a
    vol spike, not a split (the caller passes it through ``series_in``). A zero or
    insufficient-history baseline → ``None`` (pending_data, never a fabricated miss).
    """
    base = annualized_volatility([c for d, c in closes if d <= created], window=_VOL_WINDOW)
    after = annualized_volatility([c for d, c in closes if d <= due], window=_VOL_WINDOW)
    if base is None or after is None or base == Decimal("0"):
        return None
    return (after - base) / base


def _measure_actual(
    conn: sqlite3.Connection, due: es.DueInsight, prediction: Prediction,
    *, actions: ActionIndex, now: datetime, reporting: Currency,
) -> scoring.ActualMeasurement | None:
    """Build the objective measurement for a due insight, or None when unavailable.

    A None return (or all-None measurement fields) signals the actual value is unavailable
    (missing/halted price) → the caller defers as pending_data (anti-poison). All three
    metrics are measured at PER-SYMBOL scope (W5, AI-D22..D26): ``price_change`` from the
    symbol's own legs; ``relative`` adds the fixed market benchmark's create→due return
    (``None`` for an MY symbol, an unregistered symbol, or a missing benchmark series →
    honest pending_data); ``volatility`` compares two fixed 30-day realized-vol windows.
    At PORTFOLIO scope (symbol=None, W7 AI-D35) only ``price_change`` is measurable — via
    the flow-adjusted TWR index (``_portfolio_return``); the other two stay honest None.

    Baseline (M4 fix, decision Q1c): the card's stored ``price_at_create`` — the last
    close the model actually saw — is the preferred start price, so the score measures
    the move from the model's own reference point. LEGACY cards without it fall back to
    the old basis (the first stored close ON/AFTER the create date), so old cards keep
    scoring on the basis they were created under.

    §5.1(d) / W6c — **both legs are expressed in the DUE DATE's share terms.** ``change``
    divides one date's close by another's, and a split between them is the whole ratio: a
    7-for-1 scores every open card as a −86% collapse, i.e. a fabricated miss on the model's
    permanent record. ``price_at_create`` gets the same treatment as a fetched start price
    (``priced_on=created``, the closest date the stored scalar carries) — re-expressing one
    leg and not the other would MANUFACTURE the discrepancy this fixes. The volatility arm's
    whole series is re-expressed the same way (a split inside a vol window is a vol spike,
    not a split), and the benchmark leg rides the same seam as the performance router's
    read — a structural no-op while the ledger records no action on the benchmark key, but
    a HELD 0050 with a recorded SPLIT would otherwise fabricate a −86% "benchmark return".
    """
    symbol = due.symbol
    created = datetime.fromisoformat(due.created_at).date()
    due_date = datetime.fromisoformat(due.due_at).date() if due.due_at is not None else None
    if due_date is None:
        return None
    if symbol is None:
        # Portfolio scope (W7, AI-D35 — closes AI-D26's deferred question): only
        # ``price_change`` is measured, via the flow-adjusted TWR over create→due (a
        # mid-window deposit never reads as profit). ``relative``/``volatility`` stay
        # per-symbol — a blended three-market benchmark is a separate ruling. The shared
        # ±0.5% flat band applies downstream in ``score_quant`` (AI-D25 untouched).
        if prediction.metric != "price_change":
            return None
        change = _portfolio_return(conn, created, due_date, now=now, reporting=reporting)
        if change is None:
            return None  # thin/unavailable trend → pending_data (anti-poison)
        return scoring.ActualMeasurement(price_change_pct=change)
    start_px = (
        price_in(actions, symbol, Decimal(due.price_at_create),
                 priced_on=created, valued_on=due_date)
        if due.price_at_create
        else _price_on_or_after(conn, symbol, created,
                                actions=actions, valued_on=due_date)
    )
    end_px = _price_on_or_before(conn, symbol, due_date,
                                 actions=actions, valued_on=due_date)
    if start_px is None or end_px is None or start_px == Decimal("0"):
        return None  # price unavailable/halted → pending_data
    change = (end_px - start_px) / start_px
    if prediction.metric == "relative":
        bench = _benchmark_for_symbol(conn, symbol)
        bench_ret: Decimal | None = None
        if bench is not None:
            bench_ret = _window_return(
                [
                    (p.as_of, p.value)
                    for p in series_in(
                        actions, bench.storage_key,
                        get_price_history(
                            conn, bench.storage_key, created,
                            due_date + timedelta(days=_EVAL_LOOKBACK_DAYS),
                        ),
                        valued_on=due_date,
                    )
                ],
                created, due_date,
            )
        return scoring.ActualMeasurement(
            symbol_return_pct=change, benchmark_return_pct=bench_ret
        )
    if prediction.metric == "volatility":
        series = series_in(
            actions, symbol,
            get_price_history(
                conn, symbol,
                created - timedelta(days=_VOL_LOOKBACK_DAYS), due_date,
            ),
            valued_on=due_date,
        )
        return scoring.ActualMeasurement(
            vol_change_pct=_vol_change_pct(
                [(p.as_of, p.value) for p in series], created, due_date
            )
        )
    # Review ⑩: the 「持平」 band is scaled to THIS symbol's own volatility over the real
    # horizon. Reuses the same long window the volatility metric fetches, because the baseline
    # must be the vol the model could have seen AT CREATE — not the post-hoc one.
    band_series = series_in(
        actions, symbol,
        get_price_history(
            conn, symbol, created - timedelta(days=_VOL_LOOKBACK_DAYS), due_date,
        ),
        valued_on=due_date,
    )
    return scoring.ActualMeasurement(
        price_change_pct=change,
        flat_band=_flat_band(
            [(p.as_of, p.value) for p in band_series], created, due_date),
    )


def _score_one(
    conn: sqlite3.Connection, due: es.DueInsight, *, master_configured: bool, now: datetime,
    actions: ActionIndex, reporting: Currency,
) -> None:
    """Evaluate one due insight: quant → (master narrative) → miss → write the row.

    A prediction card with an unavailable actual defers as pending_data (or, past the
    defer cap, becomes undetermined — never a miss). Pure-narrative cards (no prediction)
    are scored on narrative alone when master is configured, else left pending.
    """
    prediction = (
        Prediction.model_validate_json(due.prediction) if due.prediction is not None else None
    )
    quant_hit: bool | None = None
    actual: scoring.ActualMeasurement | None = None
    if prediction is not None:
        actual = _measure_actual(
            conn, due, prediction, actions=actions, now=now, reporting=reporting
        )
        quant_hit = scoring.score_quant(prediction, actual)
        if quant_hit is None:
            _defer_or_undetermined(conn, due, now=now)
            return

    narrative_score: int | None = None
    note: str | None = None
    if master_configured:
        try:
            scored = master.score_narrative(
                card_text=_card_text(conn, due), snapshot_then=_snapshot_then(conn, due),
                actual_now=_actual_text(actual), eval_prompt=_eval_prompt(conn, due),
                conn=conn,
            )
            narrative_score = int(scored["narrative_score"])
            note = str(scored.get("note") or "")
        except LLMError:
            # Master unavailable/budget → degrade to quant-only (cards still scored).
            narrative_score = None

    if prediction is None and narrative_score is None:
        # Pure-narrative card with no master signal → cannot judge yet → defer.
        _defer_or_undetermined(conn, due, now=now)
        return

    miss = scoring.decide_miss(
        quant_hit=quant_hit, narrative_score=narrative_score,
        threshold=_NARRATIVE_MISS_THRESHOLD,
    )
    es.add_evaluation(
        conn, insight_id=due.insight_id, insight_type_id=due.insight_type_id,
        calibration_version=due.calibration_version, is_shadow=due.is_shadow,
        status="scored", quant_hit=quant_hit, narrative_score=narrative_score, miss=miss,
        actual_value=_actual_value(actual), confidence=due.confidence, now=now, notes=note,
    )


def _defer_or_undetermined(
    conn: sqlite3.Connection, due: es.DueInsight, *, now: datetime
) -> None:
    """Bump the defer counter; past ``defer_limit_days`` → terminal undetermined (never miss).

    ``now`` is the evaluate pass's injected clock (L7 fix — no wall-clock reads here).
    """
    cfg = cs.get_evolution_config(conn)
    limit = int(cfg["defer_limit_days"])
    latest = es.latest_for_insight(conn, due.insight_id)
    prior = latest.defer_count if latest is not None else 0
    if prior + 1 > limit:
        es.mark_undetermined(
            conn, insight_id=due.insight_id, insight_type_id=due.insight_type_id, now=now
        )
    else:
        es.bump_defer(
            conn, insight_id=due.insight_id, insight_type_id=due.insight_type_id, now=now
        )


def _card_text(conn: sqlite3.Connection, due: es.DueInsight) -> str:
    row = conn.execute(
        "SELECT title, summary, body_md FROM insights WHERE id = ?", (due.insight_id,)
    ).fetchone()
    if row is None:
        return ""
    return f"{row['title']}\n{row['summary']}\n{row['body_md']}"


def _snapshot_then(conn: sqlite3.Connection, due: es.DueInsight) -> str:
    row = conn.execute(
        "SELECT input_snapshot FROM insights WHERE id = ?", (due.insight_id,)
    ).fetchone()
    return str(row["input_snapshot"]) if row is not None else ""


def _eval_prompt(conn: sqlite3.Connection, due: es.DueInsight) -> str | None:
    it = cs.get_insight_type(conn, due.insight_type_id)
    return it.eval_prompt if it is not None else None


def _actual_text(actual: scoring.ActualMeasurement | None) -> str:
    if actual is None:
        return "（無實際數據）"
    return json.dumps(
        {k: (decimal_str(v) if isinstance(v, Decimal) else v)
         for k, v in actual.model_dump().items() if v is not None},
        ensure_ascii=False,
    )


def _actual_value(actual: scoring.ActualMeasurement | None) -> Decimal | None:
    """The single representative actual figure stored on the evaluation row (the move pct)."""
    if actual is None:
        return None
    return actual.price_change_pct or actual.symbol_return_pct or actual.vol_change_pct


def evaluate_due(
    conn: sqlite3.Connection, *, now: datetime, reporting: Currency = Currency.TWD
) -> int:
    """Score every due insight (Loop 2). Returns the count evaluated/deferred.

    The registered Loop-2 runner. Reads prices to build each actual measurement, feeds it
    into the pure quant scorer, runs master narrative scoring (skipped/degraded when the
    master role is unset or over budget), and writes ``insight_evaluations`` rows. One bad
    insight never aborts the rest (degrade, never crash the daily job). Archived tasks'
    cards are excluded (L2 fix) — their matured predictions must not keep consuming
    scoring (incl. master narrative cost) after the task was deleted. ``reporting`` sets
    the currency the portfolio-scope TWR measurement runs in (AI-D35; the cards are
    narrated against the TWD dashboard, so TWD is the default — the `run_for_id`
    precedent).
    """
    es.ensure_tables(conn)
    istore.ensure_tables(conn)  # runs the price_at_create migration for legacy DBs (M4)
    master_configured = get_role_model_id(conn, LLMRole.MASTER) is not None
    processed = 0
    # ONE index for the whole pass (trap #21) — the loop is per due insight.
    actions = load_action_index(conn)
    for due in es.due_insights(conn, now=now, exclude_type_ids=cs.archived_type_ids(conn)):
        try:
            _score_one(conn, due, master_configured=master_configured, now=now,
                       actions=actions, reporting=reporting)
            processed += 1
        except Exception:  # noqa: BLE001 — one insight failing must not abort the pass
            logger.exception("evaluate_due failed for insight %s", due.insight_id)
    # After scoring, run the Loop-4 promote + regression pass (spec 4.6) over the fresh
    # accumulated scores. Isolated so an evaluate failure never blocks the promote step.
    try:
        promote_and_check(conn, now=now)
    except Exception:  # noqa: BLE001 — the promote step must not crash the evaluate job
        logger.exception("promote_and_check failed during evaluate_due")
    return processed


# --- Loop 3: generate calibration versions (spec 04.5 / 4.8) ------------------
# Deterministic trigger (scoring.should_calibrate) + min_samples gate; the master writes the
# new body (master.generate_calibration), the validator gates it (master.validate_calibration),
# and only a valid body is appended (append-only). Master unset → pipeline pauses (no crash).


def _generate_one(
    conn: sqlite3.Connection, it: cs.InsightType, *, now: datetime, cfg: dict[str, object]
) -> bool:
    """Evaluate the triggers + min_samples gate for one combo; generate a version if due.

    Returns True when a new (valid) calibration version was appended. Master unset / over
    budget / a validator rejection → no version, no crash (the pipeline pauses).
    """
    resolved = es.resolved_sample_count(conn, it.id)
    miss_count = es.combo_score(conn, it.id)["miss_count"]
    streak = es.consecutive_misses(conn, it.id)
    gap = Decimal(str(cfg["gap_alert_pp"]))
    if not scoring.should_calibrate(
        resolved_samples=resolved, min_samples=int(str(cfg["min_samples"])),
        consecutive_misses=streak, miss_count=miss_count, gap_alert_pp=gap,
    ):
        return False
    active = cs.list_calibrations(conn, it.id)
    active_body = active[-1].body if active else ""
    active_version = active[-1].version if active else 1
    samples = es.miss_samples_for_version(
        conn, insight_type_id=it.id, version=active_version
    )
    bins = es.calibration_bins(conn, it.id)
    try:
        out = master.generate_calibration(
            active_body=active_body, miss_samples=samples, bins=bins, conn=conn
        )
        ok, _reasons = master.validate_calibration(out["body"], conn=conn)
    except LLMError:
        return False  # master unset / budget → pause (cards still generate)
    if not ok:
        logger.info("calibration for insight_type %s rejected by validator", it.id)
        return False
    cs.create_calibration(conn, it.id, body=out["body"], cause=out["cause"], now=now)
    return True


def generate_calibrations_for_all(conn: sqlite3.Connection, *, now: datetime) -> int:
    """Run the Loop-3 calibration pass over every self_correct combo. Returns versions made.

    The registered Loop-3 runner. Per spec 4.5: only self_correct, non-archived combos with
    resolved samples ≥ min_samples AND a trigger get a new version. One combo failing never
    aborts the rest (degrade, never crash the weekly job).
    """
    es.ensure_tables(conn)
    cfg = cs.get_evolution_config(conn)
    made = 0
    for it in cs.list_insight_types(conn):
        if not it.self_correct:
            continue
        try:
            if _generate_one(conn, it, now=now, cfg=cfg):
                made += 1
        except Exception:  # noqa: BLE001 — one combo failing must not abort the pass
            logger.exception("generate_calibrations failed for insight_type %s", it.id)
    return made


# --- Loop 4: shadow promote + regression check (spec 04.6) --------------------
# Deterministic (promote.py); the LLM never decides win/loss (spec 4.8). Auto-promote
# switches the active version; otherwise the win is flagged for the UI. A worsening active
# rolling score emits a ``calibration_regression`` info alert via alerts_bridge.

# Recent/baseline rolling windows for the regression check (n>=8 split into halves).
_REGRESSION_WINDOW = 8


def _active_eval_rows(conn: sqlite3.Connection, insight_type_id: int) -> list[sqlite3.Row]:
    """Active (non-shadow) scored eval rows for a combo, newest first."""
    return conn.execute(
        "SELECT miss FROM insight_evaluations WHERE insight_type_id = ? AND is_shadow = 0 "
        "AND status = 'scored' ORDER BY id DESC",
        (insight_type_id,),
    ).fetchall()


def _check_regression(conn: sqlite3.Connection, it: cs.InsightType, *, now: datetime) -> None:
    """Emit ``calibration_regression`` when the active rolling score worsens (n≥8)."""
    rows = _active_eval_rows(conn, it.id)
    if len(rows) < _REGRESSION_WINDOW:
        return
    half = _REGRESSION_WINDOW // 2
    recent = rows[:half]  # newest
    baseline = rows[half:_REGRESSION_WINDOW]  # the prior window
    if promote.is_regressing(
        recent_miss=sum(1 for r in recent if r["miss"]), recent_n=len(recent),
        baseline_miss=sum(1 for r in baseline if r["miss"]), baseline_n=len(baseline),
    ):
        alerts_bridge.ensure_tables(conn)
        alerts_bridge.record_event(
            conn, rule_id="calibration_regression", symbol=str(it.id), now=now
        )


def promote_and_check(conn: sqlite3.Connection, *, now: datetime) -> list[int]:
    """Loop-4 promote + regression pass over self_correct combos. Returns promoted ids.

    For each combo with a shadow version: compute active vs shadow scores; on a promotion
    verdict, switch the active version when ``auto_promote`` else leave it (the win is
    surfaced via ai-score for a manual switch). Always run the regression check. One combo
    failing never aborts the rest.
    """
    es.ensure_tables(conn)
    cfg = cs.get_evolution_config(conn)
    auto = bool(cfg["auto_promote"])
    promoted: list[int] = []
    for it in cs.list_insight_types(conn):
        if not it.self_correct:
            continue
        try:
            _check_regression(conn, it, now=now)
            versions = cs.list_calibrations(conn, it.id)
            latest = versions[-1].version if versions else None
            shadow_v = promote.shadow_version(
                active_version=it.active_calibration_version, latest_version=latest
            )
            if shadow_v is None:
                continue
            active_score = es.combo_score(conn, it.id, is_shadow=False)
            shadow_score = es.combo_score(conn, it.id, is_shadow=True)
            if promote.decide_promotion(active_score, shadow_score, cfg) == "promote":
                if auto:
                    cs.set_active_calibration(conn, it.id, shadow_v)
                promoted.append(it.id)
        except Exception:  # noqa: BLE001 — one combo failing must not abort the pass
            logger.exception("promote_and_check failed for insight_type %s", it.id)
    return promoted


# --- spec 07 §7.1: pipeline-hub task status (read-only fact gathering) ---------
# The api layer reads pricing/portfolio/composer/scheduler to GATHER the facts, then feeds
# them into the PURE ``pipeline_status.derive_node_states``. No business logic, no LLM, no
# write. The freshness for a task's symbols REUSES the dashboard's own freshness computation
# (the locked R4-source decision) — no new freshness path.


def _is_scheduled(conn: sqlite3.Connection, insight_type_id: int) -> bool:
    """True when a kind=insight ``schedule_config`` binding exists for the task."""
    row = conn.execute(
        "SELECT 1 FROM schedule_config WHERE job_id = ?",
        (insight_job_id(insight_type_id),),
    ).fetchone()
    return row is not None


def _template_counts(conn: sqlite3.Connection, insight_type_id: int) -> tuple[int, int]:
    """(live, total) strategy counts for a task: live = enabled + non-archived (R3)."""
    refs = cs.get_strategies(conn, insight_type_id)
    live = 0
    for ref in refs:
        sp = cs.get_strategy(conn, ref.id)
        if sp is not None and sp.enabled and not sp.archived:
            live += 1
    return live, len(refs)


def _r1_mismatch(conn: sqlite3.Connection, it: cs.InsightType) -> bool:
    """True when a non-per_symbol task's linked bodies use a per_symbol variable (R1).

    Reuses the single ``variables.validate_tokens`` core (same as the gate / composer CRUD),
    so this is observability over the SAME rule, never a re-implementation.
    """
    if it.scope == "per_symbol":
        return False
    for ref in cs.get_strategies(conn, it.id):
        sp = cs.get_strategy(conn, ref.id)
        if sp is None or not sp.enabled or sp.archived:
            continue
        if V.validate_tokens(sp.body, it.scope).scope_violations:
            return True
    return False


def _unapplied_calibration(conn: sqlite3.Connection, it: cs.InsightType) -> bool:
    """True when a non-archived calibration version exists that is not the active one."""
    versions = cs.list_calibrations(conn, it.id)
    if not versions:
        return False
    latest = versions[-1].version
    return it.active_calibration_version != latest


def _freshness_affected(freshness: FreshnessReport, symbols: list[str]) -> list[str]:
    """The given symbols whose dashboard price is missing OR stale (the R4-source view).

    Reuses the dashboard's own freshness (same snapshot): a symbol is affected when it is in
    ``missing_prices`` or its ``PriceFreshness.stale`` flag is set. Preserves *symbols* order.
    """
    missing = set(freshness.missing_prices)
    stale = {p.symbol for p in freshness.prices if p.stale}
    affected = missing | stale
    return [s for s in symbols if s in affected]


def _last_run_for(conn: sqlite3.Connection, insight_type_id: int) -> dict[str, Any] | None:
    """The task's most recent FINISHED non-shadow run (shadow excluded, spec 04 fix #3)."""
    row = conn.execute(
        "SELECT started_at, finished_at, status, detail, reason FROM job_runs "
        "WHERE job_id = ? AND is_shadow = 0 AND finished_at IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        (insight_job_id(insight_type_id),),
    ).fetchone()
    if row is None:
        return None
    notes = [row["reason"]] if row["reason"] else []
    return {
        "at": row["finished_at"],
        "status": row["status"],
        "summary": row["detail"],
        "notes": notes,
    }


def _gather_facts(
    conn: sqlite3.Connection,
    it: cs.InsightType,
    data: DashboardData,
    *,
    quota_remaining: Decimal,
    quota_low: Decimal,
    master_configured: bool,
) -> ps.PipelineFacts:
    """Assemble the fed :class:`PipelineFacts` for one task (no derivation here)."""
    universe = (
        _resolve_universe(conn, it, data) if it.scope == "per_symbol"
        else _resolve_markets(data) if it.scope == "per_market"
        else []
    )
    affected_scope = universe if it.scope == "per_symbol" else [h.symbol for h in data.holdings]
    live, total = _template_counts(conn, it.id)
    last_run = _last_run_for(conn, it.id)
    return ps.PipelineFacts(
        enabled=it.enabled,
        scope=it.scope,
        scheduled=_is_scheduled(conn, it.id),
        universe_symbols=universe,
        removed_recently=[],  # R2 removal events are surfaced by the gate; v1 status: none
        missing_or_stale_symbols=_freshness_affected(data.freshness, affected_scope),
        live_template_count=live,
        total_template_count=total,
        r1_mismatch=_r1_mismatch(conn, it),
        unapplied_calibration=_unapplied_calibration(conn, it),
        self_correct=it.self_correct,
        master_configured=master_configured,
        quota_remaining=quota_remaining,
        quota_low=quota_low,
        last_run_status=last_run["status"] if last_run is not None else None,
    )


def _last_batch(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """The most recent FINISHED non-shadow insight batch: ``{at, cards, cost_usd}`` or None.

    ``cards`` counts the non-shadow insight rows created in that batch (their ``created_at``
    equals the run's ``started_at`` — both stamped from the same injected ``now``).
    """
    row = conn.execute(
        "SELECT started_at, finished_at, cost_usd FROM job_runs "
        "WHERE job_id LIKE 'insight:%' AND is_shadow = 0 AND finished_at IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
    ).fetchone()
    if row is None:
        return None
    cards_row = conn.execute(
        "SELECT COUNT(*) AS c FROM insights WHERE is_shadow = 0 AND created_at = ?",
        (row["started_at"],),
    ).fetchone()
    return {
        "at": row["finished_at"],
        "cards": int(cards_row["c"]) if cards_row is not None else 0,
        "cost_usd": row["cost_usd"] if row["cost_usd"] is not None else "0",
    }


def build_status(
    conn: sqlite3.Connection, *, now: datetime, reporting: Currency = Currency.TWD
) -> dict[str, Any]:
    """Build the spec-07 §7.1 task-status payload (health bar + per-task pipeline cards).

    Read-only: gathers facts (schedule/universe/freshness/templates/quota/last-run) and
    feeds the PURE node-state derivation. Empty DB → ``tasks: []`` + an AI-off health bar.
    Money/quota are Decimal STRINGS on the wire (the frontend never computes).
    """
    cs.ensure_seeded(conn)
    istore.ensure_tables(conn)
    quota = budget_remaining(conn)
    quota_low = get_alert_threshold(conn)
    master_configured = get_role_model_id(conn, LLMRole.MASTER) is not None
    data = build_dashboard(conn, now=now, reporting=reporting)

    tasks: list[dict[str, Any]] = []
    for it in cs.list_insight_types(conn):
        facts = _gather_facts(
            conn, it, data, quota_remaining=quota, quota_low=quota_low,
            master_configured=master_configured,
        )
        derived = ps.derive_node_states(facts)
        tasks.append({
            "id": it.id,
            "name": it.name,
            "scope": it.scope,
            "enabled": it.enabled,
            "level": derived.level,
            "nodes": {k: v.model_dump() for k, v in derived.nodes.items()},
            "last_run": _last_run_for(conn, it.id),
        })

    return {
        "as_of": now.isoformat(),
        "health": {
            "master_ok": master_configured,
            "quota_remaining": decimal_str(quota),
            "last_batch": _last_batch(conn),
        },
        "tasks": tasks,
    }


# --- spec 07 §7.2/7.3: preflight (shared 04 gate) + diagnose -------------------
# HARD RULE (§7.2): preflight calls the SAME runtime gate as execution
# (``gating.evaluate_gates``, via the SAME ``generate._gate_context`` builder for a saved
# task), so a "preflight passed, run failed" double-truth is impossible. Preflight NEVER
# calls the LLM and NEVER writes a job_runs row — it is purely a dry run.


class PreflightDraft(BaseModel):
    """A transient (unsaved) task spec for the wizard's "check before create" (§7.2).

    The same editable fields as the composer's ``InsightTypeIn``; preflight validates +
    assembles a transient task from these WITHOUT persisting anything.
    """

    name: str = "(draft)"
    scope: str = "portfolio"  # 'per_symbol' | 'portfolio' | 'on_alert'
    strategy_ids: list[int] = Field(default_factory=list)
    use_system_prompt: bool = True
    self_correct: bool = False
    universe: dict[str, Any] | list[Any] | str | None = None
    alert_rules: dict[str, Any] | list[Any] | str | None = None
    enabled: bool = True


def _resolve_universe_raw(
    conn: sqlite3.Connection,
    universe: dict[str, Any] | list[Any] | str | None,
    data: DashboardData,
) -> list[str]:
    """Resolve a per_symbol universe value (mode:all → holdings, mode:custom → listed,
    mode:all_registered → holdings + watchlist). The draft-preflight twin of
    ``_resolve_universe`` (spec 07 §7.2 dry run)."""
    held = sorted({h.symbol for h in data.holdings})
    if isinstance(universe, dict):
        mode = universe.get("mode")
        if mode == "custom":
            syms = universe.get("symbols")
            return list(syms) if isinstance(syms, list) else []
        if mode == "all_registered":
            return _all_registered_symbols(conn)
        if mode == "all":
            return held
    return held


def _missing_prices_for(
    data: DashboardData, scope: str, universe_symbols: list[str], conn: sqlite3.Connection
) -> list[str]:
    """The missing-price symbols feeding R4 (same source as ``run_for_id``).

    Dashboard freshness ``missing_prices`` plus, for per_symbol, any universe symbol with
    NO stored price history at all (a custom-list symbol not in the priced holdings).

    RAW read, deliberately (W6c): this is an EXISTENCE test — no close is read, compared or
    displayed — and a corporate action changes the denomination of a price, never whether a
    row is there. Do not "fix" it by routing it through ``series_in``.
    """
    missing = list(data.freshness.missing_prices)
    if scope == "per_symbol":
        for sym in universe_symbols:
            if sym in missing:
                continue
            if not get_price_history(conn, sym, data.as_of.date(), data.as_of.date()) \
                    and _has_no_history(conn, sym, data.as_of.date()):
                missing.append(sym)
    return missing


def _has_no_history(conn: sqlite3.Connection, symbol: str, as_of: date) -> bool:
    """True when a symbol has no stored price within the standard history window.

    RAW read (W6c): emptiness only — see :func:`_missing_prices_for`.
    """
    history = get_price_history(conn, symbol, as_of - timedelta(days=_HISTORY_DAYS), as_of)
    return not history


def _draft_gate_context(
    conn: sqlite3.Connection,
    draft: PreflightDraft,
    *,
    universe_symbols: list[str],
    inputs: RunInputs,
) -> GateContext:
    """Build the GateContext for an UNSAVED draft (same fields as the saved-task builder).

    Reads the (already-saved) referenced strategies for the R1 token scan + R3 live count,
    then feeds the SAME :class:`GateContext` the gate consumes for an executed run.
    """
    bodies: list[str] = []
    live = 0
    for sid in draft.strategy_ids:
        sp = cs.get_strategy(conn, sid)
        if sp is None or not sp.enabled or sp.archived:
            continue
        live += 1
        bodies.append(sp.body)
    alert_rules = draft.alert_rules if isinstance(draft.alert_rules, (str, list)) else None
    return GateContext(
        scope=draft.scope,
        live_strategy_count=live,
        budget_remaining=inputs.budget_remaining,
        strategy_bodies=bodies,
        universe_symbols=universe_symbols,
        missing_price_symbols=inputs.missing_price_symbols,
        self_correct=draft.self_correct,
        master_configured=inputs.master_configured,
        alert_rules=alert_rules,
    )


def _preview_var_context(
    conn: sqlite3.Connection,
    data: DashboardData,
    *,
    scope: str,
    universe_symbols: list[str],
    now: datetime,
    reporting: Currency,
) -> V.VarContext:
    """One representative VarContext for the assembled preview (first symbol / portfolio)."""
    if scope == "per_symbol" and universe_symbols:
        # ONE symbol, so one index — built here rather than threaded from the caller, which
        # has no other use for it (trap #21 is about a per-symbol build inside a loop).
        return _per_symbol_ctx(conn, data, universe_symbols[0], now=now, reporting=reporting,
                               actions=load_action_index(conn))
    return _portfolio_ctx(conn, data, now=now, reporting=reporting)


def _default_input_price(conn: sqlite3.Connection) -> Decimal:
    """The default-role model's input price per Mtok (USD), or 0 when unset (zero-cost)."""
    model_id = get_role_model_id(conn, LLMRole.DEFAULT)
    if model_id is None:
        return Decimal("0")
    model = llm_config_get_model(conn, model_id)
    return model.input_price_per_mtok if model is not None else Decimal("0")


def _est_tokens(prompt: str) -> int:
    """Heuristic token estimate (no tokenizer dep): ~4 chars per token, ceil (spec 06)."""
    return math.ceil(len(prompt) / 4)


# --- gate-finding → display-gate mapping --------------------------------------

# The fixed §7.2 display order. R1..R6 mirror the runtime gate; G0/G1/G7 wrap it.
_RULE_SLOTS = ("R1", "R2", "R3", "R4", "R5", "R6")
_RULE_NAMES: dict[str, str] = {
    "R1": "範圍相容", "R2": "標的宇宙", "R3": "模板啟用",
    "R4": "價格資料", "R5": "變數可用性", "R6": "LLM 額度",
}
# The one-key fix per rule slot (§7.2 fix.kind enum). R6 (LLM quota) has NO one-click
# fix — a budget top-up is not in the §7.2 enum (senior-review fix: it must not emit
# create_schedule, which belongs to G1 only).
_RULE_FIX: dict[str, str] = {
    "R2": "edit_universe", "R3": "enable_template", "R4": "edit_universe",
    "R5": "edit_templates",
}


def _finding_for(result: GateResult, rule_id: str) -> gating.GateFinding | None:
    """The gate finding for a rule id (R1..R6), or None when the rule did not fire."""
    return next((g for g in result.gates if g.id == rule_id), None)


def _lv_of(finding: gating.GateFinding | None) -> str:
    """Map a gate finding's level (block/warn/info) to the display level; None → ok."""
    if finding is None:
        return "ok"
    return "fail" if finding.lv == "block" else finding.lv


def _rule_gates(result: GateResult, *, disabled_template_id: int | None) -> list[dict[str, Any]]:
    """The R1..R6 display gates (in order), each mapped from the shared gate's findings."""
    gates: list[dict[str, Any]] = []
    for rule_id in _RULE_SLOTS:
        finding = _finding_for(result, rule_id)
        lv = _lv_of(finding)
        msg = finding.msg if finding is not None else "通過"
        fix: dict[str, Any] | None = None
        if lv != "ok":
            kind = _RULE_FIX.get(rule_id)
            if kind is not None:
                fix = {"kind": kind}
                if rule_id == "R3" and disabled_template_id is not None:
                    fix["id"] = disabled_template_id
        gates.append(
            {"id": rule_id, "name": _RULE_NAMES[rule_id], "lv": lv, "msg": msg, "fix": fix}
        )
    return gates


def _disabled_template_id(conn: sqlite3.Connection, strategy_ids: list[int]) -> int | None:
    """The first linked template that is disabled/archived (drives the R3 one-click fix)."""
    for sid in strategy_ids:
        sp = cs.get_strategy(conn, sid)
        if sp is not None and (not sp.enabled or sp.archived):
            return sid
    return None


def _g0_g1(
    *, enabled: bool, scope: str, scheduled: bool
) -> tuple[dict[str, Any], dict[str, Any]]:
    """G0 (task enabled) + G1 (trigger source) display gates (§7.2).

    G0: a disabled task fails (one-click enable). G1: a non-on_alert task with no schedule
    binding WARNS ("won't auto-run", one-click create_schedule) — manual triggering is a
    legitimate mode, so a healthy task must not read as blocked (softened from fail,
    human sign-off 2026-07-05; §7.2 originally hard-failed it). on_alert is
    event-triggered (spec 03), so its trigger is never "manual" → ok.
    """
    g0: dict[str, Any] = (
        {"id": "G0", "name": "任務啟用", "lv": "ok", "msg": "任務已啟用", "fix": None}
        if enabled
        else {
            "id": "G0", "name": "任務啟用", "lv": "fail", "msg": "任務已停用，不會執行",
            "fix": {"kind": "enable_task"},
        }
    )
    if scope == "on_alert":
        g1: dict[str, Any] = {
            "id": "G1", "name": "觸發來源", "lv": "ok", "msg": "由風險預警事件觸發", "fix": None,
        }
    elif scheduled:
        g1 = {"id": "G1", "name": "觸發來源", "lv": "ok", "msg": "已排程", "fix": None}
    else:
        g1 = {
            "id": "G1", "name": "觸發來源", "lv": "warn", "msg": "未排程（手動），不會自動執行",
            "fix": {"kind": "create_schedule"},
        }
    return g0, g1


def _g7(
    conn: sqlite3.Connection, *, self_correct: bool, master_configured: bool,
    unapplied_calibration: bool,
) -> dict[str, Any]:
    """G7 (calibration pipeline): master unset (with self_correct) → warn; an unapplied
    calibration version → info; else ok (§7.2)."""
    if self_correct and not master_configured:
        return {
            "id": "G7", "name": "校正管線", "lv": "warn",
            "msg": "已開啟自我校正但未設定 AI 大師模型；校正管線暫停",
            "fix": None,
        }
    if unapplied_calibration:
        return {
            "id": "G7", "name": "校正管線", "lv": "info", "msg": "有未套用的校正版本",
            "fix": {"kind": "set_active_calibration"},
        }
    return {"id": "G7", "name": "校正管線", "lv": "ok", "msg": "校正管線正常", "fix": None}


def _verdict(gates: list[dict[str, Any]]) -> str:
    """blocked when any gate fails; degraded when any warns; else clean (§7.2)."""
    levels = {g["lv"] for g in gates}
    if "fail" in levels:
        return "blocked"
    if "warn" in levels:
        return "degraded"
    return "clean"


def _assembled_preview(
    conn: sqlite3.Connection,
    *,
    insight_type_id: int,
    ctx: V.VarContext,
    draft: PreflightDraft | None,
) -> dict[str, Any]:
    """The §7.2 assembled preview (reuses the 06 assemble path): layers + est tokens + est cost.

    For a saved task the 06 ``assemble.assemble_layers`` is reused verbatim. For a draft
    (unsaved) the same per-layer render is applied to the draft's referenced strategy bodies
    (system + enabled templates; a draft has no calibration chain). Est cost =
    est_tokens × the default model's input price (no spend — preflight is zero-cost).
    """
    if draft is None:
        assembly = assemble.assemble_layers(conn, insight_type_id, ctx)
        layers = [
            {"kind": lyr.kind, "name": lyr.name, "rendered": lyr.rendered}
            for lyr in assembly.layers
        ]
        prompt = assembly.prompt
    else:
        layers, prompt = _draft_layers(conn, draft, ctx)
    est_tokens = _est_tokens(prompt)
    est_cost = Decimal(est_tokens) * _default_input_price(conn) / Decimal("1000000")
    return {
        "layers": layers,
        "est_tokens": est_tokens,
        "est_cost_usd": decimal_str(est_cost),
    }


def _draft_layers(
    conn: sqlite3.Connection, draft: PreflightDraft, ctx: V.VarContext
) -> tuple[list[dict[str, Any]], str]:
    """Render a draft's layers transiently (system + enabled templates) — no persistence."""
    from portfolio_dash.llm_insight.system_prompt import get_system_prompt

    layers: list[dict[str, Any]] = []
    rendered_parts: list[str] = []
    if draft.use_system_prompt:
        body = get_system_prompt(conn)["body"]
        rendered, _ = V.render_prompt(body, ctx)
        layers.append({"kind": "system", "name": "system", "rendered": rendered})
        rendered_parts.append(rendered)
    for sid in draft.strategy_ids:
        sp = cs.get_strategy(conn, sid)
        if sp is None or not sp.enabled or sp.archived:
            continue
        rendered, _ = V.render_prompt(sp.body, ctx)
        layers.append({"kind": "template", "name": sp.name, "rendered": rendered})
        rendered_parts.append(rendered)
    return layers, "\n\n".join(rendered_parts)


def build_preflight(
    conn: sqlite3.Connection,
    insight_type_id: int,
    *,
    now: datetime,
    reporting: Currency = Currency.TWD,
    draft: PreflightDraft | None = None,
    include_preview: bool = True,
) -> dict[str, Any] | None:
    """Run the spec-07 §7.2 dry-run preflight for a task (or an unsaved draft).

    Builds the SAME GateContext execution builds (``generate._gate_context`` for a saved
    task; an equivalent transient context for a draft) and runs the SAME shared gate
    (``gating.evaluate_gates``). Wraps the R1..R6 findings with G0/G1/G7 in the fixed order,
    computes the verdict, and (unless ``include_preview`` is False, for diagnose) attaches
    the 06 assembled preview. NEVER calls the LLM, NEVER writes a job_runs row.

    Returns the payload, or ``None`` when a saved task id is unknown and no draft is given
    (the router maps that to 404).
    """
    cs.ensure_seeded(conn)
    data = build_dashboard(conn, now=now, reporting=reporting)
    quota = budget_remaining(conn)
    master_configured = get_role_model_id(conn, LLMRole.MASTER) is not None

    if draft is not None:
        return _preflight_draft(
            conn, draft, data, now=now, reporting=reporting, quota=quota,
            master_configured=master_configured, include_preview=include_preview,
        )

    it = cs.get_insight_type(conn, insight_type_id)
    if it is None:
        return None
    return _preflight_saved(
        conn, it, data, now=now, reporting=reporting, quota=quota,
        master_configured=master_configured, include_preview=include_preview,
    )


def _preflight_saved(
    conn: sqlite3.Connection,
    it: cs.InsightType,
    data: DashboardData,
    *,
    now: datetime,
    reporting: Currency,
    quota: Decimal,
    master_configured: bool,
    include_preview: bool,
) -> dict[str, Any]:
    """Preflight a SAVED task: share ``generate._gate_context`` + ``gating.evaluate_gates``."""
    universe = (
        _resolve_universe(conn, it, data) if it.scope == "per_symbol"
        else _resolve_markets(data) if it.scope == "per_market"
        else []
    )
    missing = _missing_prices_for(data, it.scope, universe, conn)
    inputs = RunInputs(
        budget_remaining=quota,
        master_configured=master_configured,
        universe_symbols=universe,
        missing_price_symbols=missing,
    )
    ctx = generate._gate_context(conn, it, inputs)
    result = gating.evaluate_gates(ctx)
    strategy_ids = [ref.id for ref in cs.get_strategies(conn, it.id)]
    gates = _compose_gates(
        conn, result,
        enabled=it.enabled, scope=it.scope, scheduled=_is_scheduled(conn, it.id),
        self_correct=it.self_correct, master_configured=master_configured,
        unapplied_calibration=_unapplied_calibration(conn, it),
        strategy_ids=strategy_ids,
    )
    payload: dict[str, Any] = {"gates": gates, "verdict": _verdict(gates)}
    if include_preview:
        preview_ctx = _preview_var_context(
            conn, data, scope=it.scope, universe_symbols=universe, now=now, reporting=reporting
        )
        payload["assembled_preview"] = _assembled_preview(
            conn, insight_type_id=it.id, ctx=preview_ctx, draft=None
        )
    return payload


def _preflight_draft(
    conn: sqlite3.Connection,
    draft: PreflightDraft,
    data: DashboardData,
    *,
    now: datetime,
    reporting: Currency,
    quota: Decimal,
    master_configured: bool,
    include_preview: bool,
) -> dict[str, Any]:
    """Preflight an UNSAVED draft: same shared gate, transient context, nothing persisted."""
    universe = (
        _resolve_universe_raw(conn, draft.universe, data) if draft.scope == "per_symbol"
        else _resolve_markets(data) if draft.scope == "per_market"
        else []
    )
    missing = _missing_prices_for(data, draft.scope, universe, conn)
    inputs = RunInputs(
        budget_remaining=quota,
        master_configured=master_configured,
        universe_symbols=universe,
        missing_price_symbols=missing,
    )
    ctx = _draft_gate_context(conn, draft, universe_symbols=universe, inputs=inputs)
    result = gating.evaluate_gates(ctx)
    gates = _compose_gates(
        conn, result,
        enabled=draft.enabled, scope=draft.scope, scheduled=False,  # a draft has no schedule
        self_correct=draft.self_correct, master_configured=master_configured,
        unapplied_calibration=False,  # a draft has no calibration chain
        strategy_ids=draft.strategy_ids,
    )
    payload: dict[str, Any] = {"gates": gates, "verdict": _verdict(gates)}
    if include_preview:
        preview_ctx = _preview_var_context(
            conn, data, scope=draft.scope, universe_symbols=universe, now=now,
            reporting=reporting,
        )
        payload["assembled_preview"] = _assembled_preview(
            conn, insight_type_id=0, ctx=preview_ctx, draft=draft
        )
    return payload


def _compose_gates(
    conn: sqlite3.Connection,
    result: GateResult,
    *,
    enabled: bool,
    scope: str,
    scheduled: bool,
    self_correct: bool,
    master_configured: bool,
    unapplied_calibration: bool,
    strategy_ids: list[int],
) -> list[dict[str, Any]]:
    """Assemble the fixed §7.2 gate list: G0, G1, R1..R6 (shared gate), G7."""
    g0, g1 = _g0_g1(enabled=enabled, scope=scope, scheduled=scheduled)
    rule_gates = _rule_gates(
        result, disabled_template_id=_disabled_template_id(conn, strategy_ids)
    )
    g7 = _g7(
        conn, self_correct=self_correct, master_configured=master_configured,
        unapplied_calibration=unapplied_calibration,
    )
    return [g0, g1, *rule_gates, g7]


# --- spec 07 §7.3: diagnose ("why didn't it run") -----------------------------
# Diagnose = the read-only preflight gates (no preview needed) + the first blocking gate
# id + the recent skip rows. No new state: it REUSES the same shared-gate preflight build
# and the existing job_runs query.

_RECENT_SKIPS_LIMIT = 5


def _first_blocker(gates: list[dict[str, Any]]) -> str | None:
    """The id of the first gate that FAILED (the chain's first hard blocker), or None."""
    for g in gates:
        if g["lv"] == "fail":
            return str(g["id"])
    return None


def _recent_skips(conn: sqlite3.Connection, insight_type_id: int) -> list[dict[str, Any]]:
    """The last 5 SKIPPED non-shadow runs as ``[{at, reason}]`` (reason = the 04b enum)."""
    rows = conn.execute(
        "SELECT finished_at, started_at, reason FROM job_runs WHERE job_id = ? "
        "AND is_shadow = 0 AND status = 'skipped' ORDER BY id DESC LIMIT ?",
        (insight_job_id(insight_type_id), _RECENT_SKIPS_LIMIT),
    ).fetchall()
    return [
        {"at": r["finished_at"] or r["started_at"], "reason": r["reason"]}
        for r in rows
    ]


def build_diagnose(
    conn: sqlite3.Connection,
    insight_type_id: int,
    *,
    now: datetime,
    reporting: Currency = Currency.TWD,
) -> dict[str, Any] | None:
    """Build the spec-07 §7.3 diagnose payload for a SAVED task ("why didn't it run").

    Read-only: the SAME shared-gate preflight gates (without the assembled preview) +
    ``first_blocker`` (the first failing gate id, or null) + ``recent_skips`` (the last 5
    skipped runs, each with the single 04b reason enum). Returns ``None`` for an unknown id
    (the router maps that to 404). Never calls the LLM, never writes a job_runs row.
    """
    payload = build_preflight(
        conn, insight_type_id, now=now, reporting=reporting, include_preview=False,
    )
    if payload is None:
        return None
    gates = payload["gates"]
    return {
        "gates": gates,
        "verdict": payload["verdict"],
        "first_blocker": _first_blocker(gates),
        "recent_skips": _recent_skips(conn, insight_type_id),
    }
