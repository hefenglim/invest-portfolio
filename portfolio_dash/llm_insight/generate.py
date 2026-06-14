"""Generation orchestration (spec 04.0 / 4.9) — run_insight_type, the PURE controller.

``run_insight_type`` is the Loop-1 (自運作) entry point. It:

1. builds the R1–R8 :class:`~llm_insight.gating.GateContext` from the composer tables +
   the FED gate inputs and runs the single shared gate;
2. on a hard block → writes a ``job_runs(status=skipped, reason)`` row, returns, no LLM;
3. otherwise, for each R8 target (one per symbol for per_symbol, else one card):
   - R4 missing price → a deterministic "資料異常" card (zero LLM, zero cost);
   - fingerprint cache hit → reuse the cached card (no LLM);
   - else assemble layers → ``complete_structured`` (default role, ``InsightCard`` schema)
     → store the card; records cost to ``llm_usage`` (via ``shared.llm``);
   - mid-iteration quota exhaustion (R6) → stop remaining targets, mark the run ``partial``,
     keep produced cards.

**LOCKED layering (architecture.md):** this module is PURE — stdlib + pydantic + ``shared``
+ ``llm_insight`` internals. It imports NEITHER ``pricing`` NOR ``data_ingestion``. All
conn-bearing inputs (dashboard data, price history, external snapshots, fx) are FED IN as
per-symbol :class:`~llm_insight.variables.VarContext` objects + a :class:`RunInputs` bundle;
the only seam that reads pricing/portfolio is the api service layer
(``api/insight_service.py``). It writes the shared ``job_runs`` table via SQL (no scheduler
import — sharing a table is not importing a module).
"""

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from portfolio_dash.llm_insight import assemble
from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.llm_insight import insights_store as istore
from portfolio_dash.llm_insight import variables as V
from portfolio_dash.llm_insight.cards import InsightCard
from portfolio_dash.llm_insight.gating import GateContext, GateResult, evaluate_gates, skip_reasons
from portfolio_dash.llm_insight.insights_store import HorizonBasis
from portfolio_dash.shared import llm
from portfolio_dash.shared.llm_config import LLMError

# The agent tag recorded in llm_usage for an insight generation call.
_AGENT = "insight_generate"
_DEFAULT_PROMPT_VERSION = "v1"


class RunInputs(BaseModel):
    """The fed gate/run inputs the service layer assembles from conn-bearing reads.

    Everything here is already computed; ``run_insight_type`` reads no connection for these.
    ``budget_remaining`` drives R6 (and its mid-iteration re-check). The per-symbol maps are
    keyed by the resolved universe symbols (per_symbol scope) or ignored for portfolio.
    """

    model_config = {"arbitrary_types_allowed": True}

    budget_remaining: Decimal
    master_configured: bool = False
    universe_symbols: list[str] = Field(default_factory=list)
    removed_symbols: list[str] = Field(default_factory=list)
    missing_price_symbols: list[str] = Field(default_factory=list)
    unavailable_vars: list[str] = Field(default_factory=list)
    input_snapshots: dict[str, str] = Field(default_factory=dict)  # key: symbol or "" (portfolio)
    prompt_version: str = _DEFAULT_PROMPT_VERSION
    horizon_basis: HorizonBasis = "trading_days"
    is_shadow: bool = False
    # on_alert (R7): the fired event the dispatcher is acting on.
    fired_rule: str | None = None
    fired_symbol: str | None = None


class RunResult(BaseModel):
    """The outcome of a generation run (mirrors a ``job_runs`` row)."""

    status: str  # 'ok' | 'partial' | 'skipped'
    reason: str
    cards_created: int
    cost_usd: Decimal


def _gate_context(
    conn: sqlite3.Connection, it: cs.InsightType, inputs: RunInputs
) -> GateContext:
    """Build the gate context from the composer tables + the fed run inputs."""
    strategies = cs.get_strategies(conn, it.id)
    bodies: list[str] = []
    live = 0
    for ref in strategies:
        sp = cs.get_strategy(conn, ref.id)
        if sp is None or not sp.enabled or sp.archived:
            continue
        live += 1
        bodies.append(sp.body)
    alert_rules = it.alert_rules if isinstance(it.alert_rules, (str, list)) else None
    return GateContext(
        scope=it.scope,
        live_strategy_count=live,
        budget_remaining=inputs.budget_remaining,
        insight_type_id=it.id,
        strategy_bodies=bodies,
        universe_symbols=inputs.universe_symbols,
        removed_symbols=inputs.removed_symbols,
        missing_price_symbols=inputs.missing_price_symbols,
        unavailable_vars=inputs.unavailable_vars,
        self_correct=it.self_correct,
        master_configured=inputs.master_configured,
        alert_rules=alert_rules,
        fired_rule=inputs.fired_rule,
        fired_symbol=inputs.fired_symbol,
    )


def _snapshot_for(inputs: RunInputs, symbol: str | None, ctx: V.VarContext) -> str:
    """The input-snapshot string for a target; fed by the service, else the as_of+symbol.

    The snapshot's content feeds the fingerprint digest, so its DATE makes the fingerprint
    distinct per trading day (spec 04.10 cache semantics).
    """
    key = symbol or ""
    fed = inputs.input_snapshots.get(key)
    if fed is not None:
        return fed
    return f"{ctx.data.as_of}|{symbol or 'portfolio'}"


def _anomaly_card(symbol: str) -> InsightCard:
    """The deterministic zero-LLM "資料異常" card for a missing-price symbol (R4)."""
    return InsightCard(
        title=f"{symbol} 資料異常",
        summary="缺少報價，無法產生洞察。",
        body_md=f"**{symbol}** 目前無可用報價，已略過 AI 解讀；報價恢復後將重新評估。",
        tags=["data_anomaly"],
        symbol=symbol,
    )


def _write_job_run(
    conn: sqlite3.Connection,
    insight_type_id: int,
    *,
    status: str,
    reason: str,
    cost: Decimal,
    now: datetime,
) -> None:
    """Write a completed ``job_runs`` row for an insight run (raw SQL; no scheduler import)."""
    job_id = f"insight:{insight_type_id}"
    conn.execute(
        "INSERT INTO job_runs (job_id, started_at, finished_at, status, detail, payload, "
        "reason, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            job_id,
            now.isoformat(),
            datetime.now(UTC).isoformat(),
            status,
            reason,
            str(insight_type_id),
            reason or None,
            str(cost),
        ),
    )
    conn.commit()


def _block_reason_text(result: GateResult) -> str:
    return "; ".join(r["reason"] for r in skip_reasons(result) if r["reason"])


def run_insight_type(
    conn: sqlite3.Connection,
    insight_type_id: int,
    *,
    var_contexts: dict[str | None, V.VarContext],
    inputs: RunInputs,
    now: datetime,
) -> RunResult:
    """Run one insight_type generation pass (Loop 1). See the module docstring for the flow.

    ``var_contexts`` is keyed by symbol (``None`` for a portfolio/on_alert run); the service
    layer feeds one per resolved target. Returns a :class:`RunResult` and writes a matching
    ``job_runs`` row. Pure controller — no pricing/data_ingestion read here.
    """
    it = cs.get_insight_type(conn, insight_type_id)
    if it is None:
        _write_job_run(
            conn, insight_type_id, status="skipped", reason="unknown_insight_type",
            cost=Decimal("0"), now=now,
        )
        return RunResult(
            status="skipped", reason="unknown_insight_type", cards_created=0,
            cost_usd=Decimal("0"),
        )

    gate = evaluate_gates(_gate_context(conn, it, inputs))
    if gate.verdict == "blocked":
        reason = _block_reason_text(gate)
        _write_job_run(
            conn, insight_type_id, status="skipped", reason=reason, cost=Decimal("0"),
            now=now,
        )
        return RunResult(
            status="skipped", reason=reason, cards_created=0, cost_usd=Decimal("0")
        )

    remaining = inputs.budget_remaining
    total_cost = Decimal("0")
    created = 0
    stopped_early = False
    anomalies = set(gate.data_anomaly_symbols)

    for target in gate.target_symbols:
        # R4: a missing-price symbol gets a deterministic zero-LLM card.
        if target is not None and target in anomalies:
            ctx = var_contexts.get(target)
            snapshot = _snapshot_for(inputs, target, ctx) if ctx is not None else target
            fp = istore.fingerprint(
                insight_type_id, "DATA_ANOMALY",
                istore.snapshot_digest(snapshot), inputs.prompt_version,
            )
            if istore.find_by_fingerprint(conn, fp) is None:
                istore.add_card(
                    conn, insight_type_id=insight_type_id, card=_anomaly_card(target),
                    fingerprint=fp, calibration_version=it.active_calibration_version,
                    horizon_days=it.horizon_days, input_snapshot=snapshot, model="(none)",
                    cost_usd=Decimal("0"), now=now, is_shadow=inputs.is_shadow,
                    horizon_basis=inputs.horizon_basis,
                )
                created += 1
            continue

        # R6 mid-iteration: stop before spending past the cap; keep produced cards.
        if remaining <= 0:
            stopped_early = True
            break

        ctx = var_contexts.get(target)
        if ctx is None:
            continue  # no fed context for this target — skip defensively (never crash)

        assembled = assemble.assemble_layers(conn, insight_type_id, ctx)
        snapshot = _snapshot_for(inputs, target, ctx)
        fp = istore.fingerprint(
            insight_type_id, assembled.prompt,
            istore.snapshot_digest(snapshot), inputs.prompt_version,
        )
        if istore.find_by_fingerprint(conn, fp) is not None:
            continue  # cache hit — same-day identical inputs, no LLM, no duplicate row

        before = remaining
        try:
            card = llm.complete_structured(
                assembled.prompt, InsightCard, agent=_AGENT, conn=conn
            )
        except LLMError:
            # Graceful degradation: a provider/budget/activation failure stops the run as
            # partial (produced cards kept); never crash the scheduler/dashboard.
            stopped_early = True
            break
        # Per-call cost = the delta of cumulative usage just logged by shared.llm.
        spent = _last_usage_cost(conn)
        total_cost += spent
        remaining = before - spent
        if target is not None:
            card = card.model_copy(update={"symbol": target})
        istore.add_card(
            conn, insight_type_id=insight_type_id, card=card, fingerprint=fp,
            calibration_version=it.active_calibration_version, horizon_days=it.horizon_days,
            input_snapshot=snapshot, model=card.symbol or "default", cost_usd=spent,
            now=now, is_shadow=inputs.is_shadow, horizon_basis=inputs.horizon_basis,
        )
        created += 1

    status = "partial" if stopped_early else "ok"
    reason = "budget_exhausted_mid_run" if stopped_early else ""
    _write_job_run(
        conn, insight_type_id, status=status, reason=reason, cost=total_cost, now=now
    )
    return RunResult(
        status=status, reason=reason, cards_created=created, cost_usd=total_cost
    )


def _last_usage_cost(conn: sqlite3.Connection) -> Decimal:
    """The cost of the most-recent ``llm_usage`` row (the call just logged by shared.llm)."""
    row = conn.execute(
        "SELECT cost FROM llm_usage ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return Decimal(row["cost"]) if row is not None else Decimal("0")
