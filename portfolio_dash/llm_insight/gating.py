"""Runtime gate R1–R8 (spec 4.9) — THE single shared gate.

``evaluate_gates`` is a PURE function over a fed :class:`GateContext`. It is the ONE place
the R1–R8 rules live: Loop-1 generation (04b ``generate.run_insight_type``) and the spec-07
preflight both call it, so gate logic is never duplicated. The orchestrator (api service
layer) populates the context from already-computed inputs — this layer reads no connection
and imports neither ``pricing`` nor ``data_ingestion`` (architecture.md).

Verdict:
* ``blocked`` — any HARD block fired (R1 scope mismatch, R2 empty universe, R3 no live
  templates, R6 quota, R7 rule-not-matched). The run does not call the LLM; the caller
  writes ``job_runs(status=skipped, reason)``.
* ``degraded`` — only SOFT issues (R4 missing prices → zero-LLM data-anomaly cards, R5
  unavailable variables). The run proceeds.
* ``clean`` — no issues (info-level notes like R2 removed-symbols / master_missing may
  still be present).

R8 is realized by :attr:`GateResult.target_symbols`: one card per combo (``[None]`` for a
portfolio/on_alert run) or one per resolved symbol (per_symbol). R4's anomaly symbols are
surfaced via :attr:`GateResult.data_anomaly_symbols` for the zero-cost deterministic card.
"""

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

from portfolio_dash.llm_insight import variables as V

Verdict = Literal["blocked", "degraded", "clean"]
GateLevel = Literal["info", "warn", "block"]


class GateFinding(BaseModel):
    """One gate observation: rule id (R1..R8 / master), severity, message, machine reason."""

    id: str
    lv: GateLevel
    msg: str
    reason: str | None = None


class GateContext(BaseModel):
    """Already-computed inputs to the gate (fed by the orchestrator; no conn here)."""

    scope: str  # 'portfolio' | 'per_symbol' | 'on_alert'
    live_strategy_count: int  # enabled + non-archived strategies in the combo (R3)
    budget_remaining: Decimal  # USD remaining for the quota gate (R6)
    insight_type_id: int = 0
    strategy_bodies: list[str] = Field(default_factory=list)  # R1 token scan
    universe_symbols: list[str] = Field(default_factory=list)  # per_symbol resolved universe
    removed_symbols: list[str] = Field(default_factory=list)  # R2 auto-removed (info)
    missing_price_symbols: list[str] = Field(default_factory=list)  # R4 per-symbol
    unavailable_vars: list[str] = Field(default_factory=list)  # R5 degraded vars
    self_correct: bool = False
    master_configured: bool = False  # master role bound (spec 4.3)
    # on_alert (R7): the subscribed rules ('all' or a list) + the fired event.
    alert_rules: str | list[str] | None = None
    fired_rule: str | None = None
    fired_symbol: str | None = None


class GateResult(BaseModel):
    """The gate outcome: verdict + findings + the R8 targets / R4 anomalies / R7 key."""

    verdict: Verdict
    gates: list[GateFinding]
    target_symbols: list[str | None]  # R8: one card per element
    data_anomaly_symbols: list[str]  # R4: zero-LLM deterministic cards
    debounce_key: str | None = None  # R7: (task, rule, symbol)


def _r1_violations(scope: str, bodies: list[str]) -> list[str]:
    """R1 scope×variable mismatch via the single ``validate_tokens`` core (no per_symbol
    variable allowed in a non-per_symbol body)."""
    if scope == "per_symbol":
        return []
    seen: list[str] = []
    for body in bodies:
        for token in V.validate_tokens(body, scope).scope_violations:
            if token not in seen:
                seen.append(token)
    return seen


def _alert_matches(alert_rules: str | list[str] | None, fired_rule: str | None) -> bool:
    """R7 filter: 'all' matches any rule; a list matches when it contains the fired rule."""
    if fired_rule is None:
        return False
    if alert_rules == "all":
        return True
    if isinstance(alert_rules, list):
        return fired_rule in alert_rules
    return False


def _resolve_targets(ctx: GateContext) -> list[str | None]:
    """R8 execution unit: one card per symbol (per_symbol) / per market code
    (per_market — ``universe_symbols`` carries "TW"/"US"/"MY"), else one per combo."""
    if ctx.scope in ("per_symbol", "per_market"):
        return list(ctx.universe_symbols)
    if ctx.scope == "on_alert":
        return [ctx.fired_symbol]
    return [None]


def evaluate_gates(ctx: GateContext) -> GateResult:
    """Run R1–R8 over *ctx* and return the verdict + findings (pure; see module docstring)."""
    gates: list[GateFinding] = []
    blocked = False
    degraded = False

    # R1 — scope × variable mismatch (hard block).
    for token in _r1_violations(ctx.scope, ctx.strategy_bodies):
        blocked = True
        gates.append(GateFinding(
            id="R1", lv="block",
            msg=f"per_symbol 變數 {token} 不可用於 {ctx.scope} 範圍組合",
            reason="R1_scope_mismatch",
        ))

    # R3 — all strategies disabled/archived (hard block; recovers when strategies return).
    if ctx.live_strategy_count <= 0:
        blocked = True
        gates.append(GateFinding(
            id="R3", lv="block", msg="組合的策略段全空（全部停用/封存）",
            reason="R3_no_live_templates",
        ))

    # R2 — per_symbol / per_market universe lifecycle (a market card needs at least
    # one held market; an emptied portfolio blocks instead of producing hollow cards).
    if ctx.scope in ("per_symbol", "per_market"):
        if not ctx.universe_symbols:
            blocked = True
            gates.append(GateFinding(
                id="R2", lv="block", msg="標的宇宙為空（清單已出清）",
                reason="R2_universe_empty",
            ))
        elif ctx.removed_symbols:
            gates.append(GateFinding(
                id="R2", lv="info",
                msg=f"已自動移除出清/移出觀察清單標的：{', '.join(ctx.removed_symbols)}",
                reason="R2_symbols_removed",
            ))

    # R6 — quota exhausted (hard block).
    if ctx.budget_remaining <= 0:
        blocked = True
        gates.append(GateFinding(
            id="R6", lv="block", msg=f"額度耗盡（剩餘 ${ctx.budget_remaining}）",
            reason="R6_quota",
        ))

    # R7 — on_alert filter (hard block when the fired rule is not subscribed).
    debounce_key: str | None = None
    if ctx.scope == "on_alert":
        if not _alert_matches(ctx.alert_rules, ctx.fired_rule):
            blocked = True
            gates.append(GateFinding(
                id="R7", lv="block",
                msg=f"觸發規則 {ctx.fired_rule} 不在此組合的訂閱規則內",
                reason="R7_rule_not_matched",
            ))
        else:
            debounce_key = f"{ctx.insight_type_id}|{ctx.fired_rule}|{ctx.fired_symbol}"

    # R4 — missing price per symbol (soft: emit a deterministic zero-LLM card).
    anomalies = [s for s in ctx.missing_price_symbols if s in ctx.universe_symbols] \
        if ctx.scope == "per_symbol" else list(ctx.missing_price_symbols)
    if anomalies:
        degraded = True
        gates.append(GateFinding(
            id="R4", lv="warn",
            msg=f"缺價標的（產確定性『資料異常』卡，零成本）：{', '.join(anomalies)}",
            reason="R4_missing_price",
        ))

    # R5 — variable unavailable (soft: degrade, proceed).
    if ctx.unavailable_vars:
        degraded = True
        gates.append(GateFinding(
            id="R5", lv="info",
            msg=f"變數資料不可用，降級執行：{', '.join(ctx.unavailable_vars)}",
            reason="R5_var_unavailable",
        ))

    # master_missing — self_correct on but no master role (warn; cards still generate).
    if ctx.self_correct and not ctx.master_configured:
        gates.append(GateFinding(
            id="master", lv="warn",
            msg="已開啟自我校正但未設定 AI 大師模型；校正管線暫停，洞察照常產生",
            reason="master_missing",
        ))

    verdict: Verdict = "blocked" if blocked else ("degraded" if degraded else "clean")
    return GateResult(
        verdict=verdict,
        gates=gates,
        target_symbols=_resolve_targets(ctx),
        data_anomaly_symbols=anomalies,
        debounce_key=debounce_key,
    )


def skip_reasons(result: GateResult) -> list[dict[str, Any]]:
    """The machine-readable block reasons for a ``job_runs`` skip row (block-level only)."""
    return [
        {"id": g.id, "reason": g.reason, "msg": g.msg}
        for g in result.gates
        if g.lv == "block"
    ]
