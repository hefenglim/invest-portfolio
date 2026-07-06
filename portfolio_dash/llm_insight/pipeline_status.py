"""Pure node-state derivation for the spec-07 Insight Pipeline Hub (§7.1.1).

The pipeline hub shows each insight task as five nodes — trigger / input / assemble /
exec / output — each carrying a level (``ok|info|warn|fail|idle``). This module is the
SINGLE place that derivation lives, as a PURE function over a fed :class:`PipelineFacts`
bundle: it reads no connection and imports neither ``pricing`` nor ``api`` nor
``data_ingestion`` (architecture.md). The api layer gathers the facts (resolving the
universe, freshness, template counts, quota, last run) and feeds them in; this layer only
applies the §7.1.1 rule table.

This is observability, NOT a second gate: the levels mirror what the runtime gate
(``gating.evaluate_gates``, R1–R6) and the schedule/freshness state imply, but the
authoritative go/no-go decision for an actual run is always the shared gate (spec 07 §7.2
preflight reuses that same function). No money is float here; quota is a ``Decimal``.
"""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

# Node/aggregate severity. ``idle`` is "not applicable / off" and sits below ``ok`` for
# the aggregate (a disabled task is wholly idle; a never-run output is idle).
NodeLevel = Literal["ok", "info", "warn", "fail", "idle"]

# Severity ordering for the aggregate ``level`` (higher = worse). ``idle`` is the floor.
_SEVERITY: dict[NodeLevel, int] = {"idle": 0, "ok": 1, "info": 2, "warn": 3, "fail": 4}

# How recent an R2 auto-removal counts as the input-node info pre-warning (spec §7.1.1).
REMOVAL_INFO_WINDOW_DAYS = 7

NodeName = Literal["trigger", "input", "assemble", "exec", "output"]


class NodeState(BaseModel):
    """One pipeline node's derived state: its level + a short text + an optional sub-line."""

    lv: NodeLevel
    text: str
    sub: str | None = None


class PipelineFacts(BaseModel):
    """The fed facts for one task's node-state derivation (gathered in the api layer).

    Everything here is already resolved against the DB/dashboard; this module computes no
    number of record. ``missing_or_stale_symbols`` is the freshness of THIS task's symbols
    taken from the dashboard's own freshness computation (the locked R4-source decision).
    ``removed_recently`` is the R2 auto-removed list within the last
    :data:`REMOVAL_INFO_WINDOW_DAYS` days. Quota figures are Decimals.
    """

    enabled: bool
    scope: str  # 'per_symbol' | 'portfolio' | 'on_alert'
    scheduled: bool  # a kind=insight schedule_config binding exists (manual when False)
    universe_symbols: list[str]
    removed_recently: list[str]
    missing_or_stale_symbols: list[str]
    live_template_count: int  # enabled + non-archived strategies in the combo (R3)
    total_template_count: int  # all linked strategies (to tell "some off" from "none")
    r1_mismatch: bool  # a scope×per_symbol-variable conflict in existing linked bodies
    unapplied_calibration: bool  # a calibration version exists but is not the active one
    self_correct: bool
    master_configured: bool
    quota_remaining: Decimal
    quota_low: Decimal  # the quota_low alert threshold (USD)
    last_run_status: str | None  # 'ok'|'partial'|'skipped'|'error'|None (never run)


class PipelineNodes(BaseModel):
    """The five derived node states keyed by node name, plus the aggregate level."""

    nodes: dict[str, NodeState]
    level: NodeLevel


def _trigger(f: PipelineFacts) -> NodeState:
    """Trigger node: manual (unscheduled) → warn ("won't auto-run"); scheduled → ok."""
    if not f.scheduled:
        return NodeState(lv="warn", text="未排程（手動）", sub="不會自動執行")
    return NodeState(lv="ok", text="已排程")


def _input(f: PipelineFacts) -> NodeState:
    """Input node: empty universe (R2) → fail; missing/stale price (R4 source) → warn;
    a recent R2 auto-removal → info. Portfolio/on_alert scopes have no universe lifecycle.
    """
    if f.scope == "per_symbol" and not f.universe_symbols:
        return NodeState(lv="fail", text="標的宇宙為空", sub="清單已出清")
    count_text = f"{len(f.universe_symbols)} 檔標的"
    if f.missing_or_stale_symbols:
        joined = ", ".join(f.missing_or_stale_symbols)
        return NodeState(lv="warn", text=count_text, sub=f"{joined} 缺價/過期")
    if f.removed_recently:
        joined = ", ".join(f.removed_recently)
        return NodeState(lv="info", text=count_text, sub=f"近期移除：{joined}")
    text = count_text if f.scope == "per_symbol" else "全持倉"
    return NodeState(lv="ok", text=text)


def _assemble(f: PipelineFacts) -> NodeState:
    """Assemble node: all templates off/archived (R3) → fail; some off OR an R1 mismatch
    on existing data → warn; an unapplied calibration version → info."""
    if f.live_template_count <= 0:
        return NodeState(lv="fail", text="模板全停用", sub="組裝段為空")
    if f.live_template_count < f.total_template_count:
        return NodeState(
            lv="warn",
            text=f"{f.live_template_count}/{f.total_template_count} 模板啟用",
            sub="停用段跳過",
        )
    if f.r1_mismatch:
        return NodeState(lv="warn", text="範圍不相容", sub="既有模板含 per_symbol 變數")
    if f.unapplied_calibration:
        return NodeState(lv="info", text="有未套用校正版本", sub="可手動套用")
    return NodeState(lv="ok", text=f"{f.live_template_count} 模板啟用")


def _usd_display(x: Decimal) -> str:
    """USD for NodeState display text: 2 dp（FM5 fix — the task card printed the raw
    full-precision Decimal「$3.8014615」）. Display-only quantize; the comparisons
    above it stay full precision."""
    return f"${x.quantize(Decimal('0.01'))}"


def _exec(f: PipelineFacts) -> NodeState:
    """Exec node: quota 0 (R6) → fail; quota < quota_low OR (master unset & self_correct)
    → warn; else ok. Master-unset alone (no self_correct) does not degrade exec."""
    if f.quota_remaining <= 0:
        return NodeState(lv="fail", text="額度耗盡", sub=f"餘 {_usd_display(f.quota_remaining)}")
    if f.quota_remaining < f.quota_low:
        return NodeState(lv="warn", text="額度偏低", sub=f"餘 {_usd_display(f.quota_remaining)}")
    if f.self_correct and not f.master_configured:
        return NodeState(lv="warn", text="校正暫停", sub="未設定 AI 大師模型")
    return NodeState(lv="ok", text=f"額度餘 {_usd_display(f.quota_remaining)}")


_OUTPUT_FAIL = {"skipped", "error"}


def _output(f: PipelineFacts) -> NodeState:
    """Output node: never run → idle; last run skipped/error → fail; partial → warn."""
    status = f.last_run_status
    if status is None:
        return NodeState(lv="idle", text="從未執行")
    if status in _OUTPUT_FAIL:
        return NodeState(lv="fail", text="上次未產出", sub=status)
    if status == "partial":
        return NodeState(lv="warn", text="部分產出", sub="額度中斷")
    return NodeState(lv="ok", text="已產出", sub=status)


def derive_node_states(f: PipelineFacts) -> PipelineNodes:
    """Derive the five node states + aggregate level for one task (pure; §7.1.1).

    A disabled task is wholly ``idle`` (every node idle, aggregate idle). Otherwise each
    node is derived independently and the aggregate ``level`` is the max severity across
    the five (fail > warn > info > ok > idle).
    """
    if not f.enabled:
        idle = NodeState(lv="idle", text="已停用")
        names: tuple[NodeName, ...] = ("trigger", "input", "assemble", "exec", "output")
        return PipelineNodes(nodes={n: idle for n in names}, level="idle")

    nodes: dict[str, NodeState] = {
        "trigger": _trigger(f),
        "input": _input(f),
        "assemble": _assemble(f),
        "exec": _exec(f),
        "output": _output(f),
    }
    level = max((n.lv for n in nodes.values()), key=lambda lv: _SEVERITY[lv])
    return PipelineNodes(nodes=nodes, level=level)
