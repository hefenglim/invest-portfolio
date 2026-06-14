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
    _dividend_rows,
    _external_reasons,
    _external_vars,
    _resolve_fx_rates,
)
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
from portfolio_dash.pricing.store import get_price_history
from portfolio_dash.scheduler.jobs import insight_job_id
from portfolio_dash.shared.enums import Currency
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
    ctx.price_points = [
        {"date": p.as_of.isoformat(), "close": decimal_str(p.value)} for p in history
    ]
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
        {k: (decimal_str(v) if isinstance(v, Decimal) else v)
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
    universe = _resolve_universe(it, data) if it.scope == "per_symbol" else []
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
    universe: dict[str, Any] | list[Any] | str | None, data: DashboardData
) -> list[str]:
    """Resolve a per_symbol universe value (mode:all → holdings, mode:custom → listed)."""
    held = sorted({h.symbol for h in data.holdings})
    if isinstance(universe, dict):
        mode = universe.get("mode")
        if mode == "custom":
            syms = universe.get("symbols")
            return list(syms) if isinstance(syms, list) else []
        if mode == "all":
            return held
    return held


def _missing_prices_for(
    data: DashboardData, scope: str, universe_symbols: list[str], conn: sqlite3.Connection
) -> list[str]:
    """The missing-price symbols feeding R4 (same source as ``run_for_id``).

    Dashboard freshness ``missing_prices`` plus, for per_symbol, any universe symbol with
    NO stored price history at all (a custom-list symbol not in the priced holdings).
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
    """True when a symbol has no stored price within the standard history window."""
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
        return _per_symbol_ctx(conn, data, universe_symbols[0], now=now, reporting=reporting)
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
    binding fails ("won't auto-run", one-click create_schedule); on_alert is event-triggered
    (spec 03), so its trigger is never "manual" → ok.
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
            "id": "G1", "name": "觸發來源", "lv": "fail", "msg": "未排程（手動），不會自動執行",
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
    universe = _resolve_universe(it, data) if it.scope == "per_symbol" else []
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
        _resolve_universe_raw(draft.universe, data) if draft.scope == "per_symbol" else []
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
