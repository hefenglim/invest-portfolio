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
import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal

from portfolio_dash.api.routers.prompts import (
    _dividend_rows,
    _external_reasons,
    _external_vars,
    _resolve_fx_rates,
)
from portfolio_dash.llm_insight import alerts_bridge, master, promote, scoring
from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.llm_insight import evaluations_store as es
from portfolio_dash.llm_insight import variables as V
from portfolio_dash.llm_insight.cards import Prediction
from portfolio_dash.llm_insight.generate import RunInputs, RunResult, run_insight_type
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.portfolio.dashboard_models import DashboardData
from portfolio_dash.pricing.store import get_price_history
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.llm_config import (
    LLMError,
    LLMRole,
    budget_remaining,
    get_role_model_id,
)

logger = logging.getLogger(__name__)

_HISTORY_DAYS = 180

# A pure-narrative card (or a quant card whose narrative is the deciding signal) is a "miss"
# when the master narrative score is below this threshold (spec 4.4 / decide_miss).
_NARRATIVE_MISS_THRESHOLD = 60
# How far back to look for the create-time / due-time price closes when building the actual.
_EVAL_LOOKBACK_DAYS = 14


def _resolve_universe(it: cs.InsightType, data: DashboardData) -> list[str]:
    """The per_symbol universe: custom list, or all current holdings for ``mode:all``."""
    held = sorted({h.symbol for h in data.holdings})
    universe = it.universe
    if isinstance(universe, dict):
        mode = universe.get("mode")
        if mode == "custom":
            syms = universe.get("symbols")
            return list(syms) if isinstance(syms, list) else []
        if mode == "all":
            return held
    return held  # default: follow holdings


def _per_symbol_ctx(
    conn: sqlite3.Connection,
    data: DashboardData,
    symbol: str,
    *,
    now: datetime,
    reporting: Currency,
) -> V.VarContext:
    """Build a per-symbol VarContext (dashboard + history + external snapshots + fx)."""
    external_vars = _external_vars(conn, symbol)
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
    history = get_price_history(conn, symbol, as_of - timedelta(days=_HISTORY_DAYS), as_of)
    ctx.closes = [p.value for p in history]
    ctx.price_points = [{"date": p.as_of.isoformat(), "close": str(p.value)} for p in history]
    return ctx


def _portfolio_ctx(
    conn: sqlite3.Connection, data: DashboardData, *, now: datetime, reporting: Currency
) -> V.VarContext:
    """Build the portfolio-scope VarContext (no per-symbol detail)."""
    external_vars = _external_vars(conn, None)
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
    """
    it = cs.get_insight_type(conn, insight_type_id)
    if it is None:
        return run_insight_type(
            conn, insight_type_id, var_contexts={}, inputs=RunInputs(
                budget_remaining=budget_remaining(conn)
            ), now=now, run_id=run_id,
        )

    data = build_dashboard(conn, now=now, reporting=reporting)
    master_configured = get_role_model_id(conn, LLMRole.MASTER) is not None
    missing_prices = list(data.freshness.missing_prices)

    var_contexts: dict[str | None, V.VarContext] = {}
    universe_symbols: list[str] = []

    if it.scope == "per_symbol":
        universe_symbols = _resolve_universe(it, data)
        for sym in universe_symbols:
            ctx = _per_symbol_ctx(conn, data, sym, now=now, reporting=reporting)
            var_contexts[sym] = ctx
            # R4: a universe symbol with no price history at all (e.g. a custom-list symbol
            # not in the holdings/prices) is a missing-price anomaly → zero-LLM card.
            if not ctx.closes and sym not in missing_prices:
                missing_prices.append(sym)
    elif it.scope == "on_alert":
        target = fired_symbol
        if target is not None:
            var_contexts[target] = _per_symbol_ctx(
                conn, data, target, now=now, reporting=reporting
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


def _price_on_or_after(conn: sqlite3.Connection, symbol: str, on: date) -> Decimal | None:
    """The first stored close on/after *on* within the lookback window, or None."""
    series = get_price_history(conn, symbol, on, on + timedelta(days=_EVAL_LOOKBACK_DAYS))
    return series[0].value if series else None


def _price_on_or_before(conn: sqlite3.Connection, symbol: str, on: date) -> Decimal | None:
    """The last stored close on/before *on* within the lookback window, or None."""
    series = get_price_history(conn, symbol, on - timedelta(days=_EVAL_LOOKBACK_DAYS), on)
    return series[-1].value if series else None


def _measure_actual(
    conn: sqlite3.Connection, due: es.DueInsight, prediction: Prediction
) -> scoring.ActualMeasurement | None:
    """Build the objective measurement for a due insight, or None when unavailable.

    A None return (or all-None measurement fields) signals the actual value is unavailable
    (missing/halted price) → the caller defers as pending_data (anti-poison). Only
    ``price_change`` is fully measured in v1 (the close at create vs the close at due);
    ``volatility``/``relative`` degrade to None when their inputs are absent.
    """
    symbol = due.symbol
    if symbol is None:
        return None  # portfolio-scope quant cards are v1 narrative-only (spec 04.10)
    created = datetime.fromisoformat(due.created_at).date()
    due_date = datetime.fromisoformat(due.due_at).date() if due.due_at is not None else None
    if due_date is None:
        return None
    start_px = _price_on_or_after(conn, symbol, created)
    end_px = _price_on_or_before(conn, symbol, due_date)
    if start_px is None or end_px is None or start_px == Decimal("0"):
        return None  # price unavailable/halted → pending_data
    change = (end_px - start_px) / start_px
    if prediction.metric == "relative":
        # No benchmark series wired in v1 → benchmark unavailable → score_quant returns None.
        return scoring.ActualMeasurement(symbol_return_pct=change, benchmark_return_pct=None)
    if prediction.metric == "volatility":
        # Realized-vol change is not yet derived in v1 → unavailable → None verdict.
        return scoring.ActualMeasurement(vol_change_pct=None)
    return scoring.ActualMeasurement(price_change_pct=change)


def _score_one(
    conn: sqlite3.Connection, due: es.DueInsight, *, master_configured: bool, now: datetime
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
        actual = _measure_actual(conn, due, prediction)
        quant_hit = scoring.score_quant(prediction, actual)
        if quant_hit is None:
            _defer_or_undetermined(conn, due)
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
        _defer_or_undetermined(conn, due)
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


def _defer_or_undetermined(conn: sqlite3.Connection, due: es.DueInsight) -> None:
    """Bump the defer counter; past ``defer_limit_days`` → terminal undetermined (never miss)."""
    cfg = cs.get_evolution_config(conn)
    limit = int(cfg["defer_limit_days"])
    latest = es.latest_for_insight(conn, due.insight_id)
    prior = latest.defer_count if latest is not None else 0
    if prior + 1 > limit:
        es.mark_undetermined(
            conn, insight_id=due.insight_id, insight_type_id=due.insight_type_id
        )
    else:
        es.bump_defer(
            conn, insight_id=due.insight_id, insight_type_id=due.insight_type_id
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
        {k: (str(v) if isinstance(v, Decimal) else v)
         for k, v in actual.model_dump().items() if v is not None},
        ensure_ascii=False,
    )


def _actual_value(actual: scoring.ActualMeasurement | None) -> Decimal | None:
    """The single representative actual figure stored on the evaluation row (the move pct)."""
    if actual is None:
        return None
    return actual.price_change_pct or actual.symbol_return_pct or actual.vol_change_pct


def evaluate_due(conn: sqlite3.Connection, *, now: datetime) -> int:
    """Score every due insight (Loop 2). Returns the count evaluated/deferred.

    The registered Loop-2 runner. Reads prices to build each actual measurement, feeds it
    into the pure quant scorer, runs master narrative scoring (skipped/degraded when the
    master role is unset or over budget), and writes ``insight_evaluations`` rows. One bad
    insight never aborts the rest (degrade, never crash the daily job).
    """
    es.ensure_tables(conn)
    master_configured = get_role_model_id(conn, LLMRole.MASTER) is not None
    processed = 0
    for due in es.due_insights(conn, now=now):
        try:
            _score_one(conn, due, master_configured=master_configured, now=now)
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
