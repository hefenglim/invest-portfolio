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
    number of record. ``missing_price_symbols`` / ``stale_price_symbols`` are the freshness of
    THIS task's symbols taken from the dashboard's own freshness computation (the locked
    R4-source decision). ``removed_recently`` is the R2 auto-removed list within the last
    :data:`REMOVAL_INFO_WINDOW_DAYS` days. Quota figures are Decimals.
    """

    enabled: bool
    scope: str  # 'per_symbol' | 'per_market' | 'portfolio' | 'on_alert'
    scheduled: bool  # a kind=insight schedule_config binding exists (manual when False)
    universe_symbols: list[str]
    removed_recently: list[str]
    # M8 (2026-09-16): SPLIT from the old single ``missing_or_stale_symbols`` list. The card
    # flattened "missing OR stale" into one warn while the dry-run preflight's R4 tested
    # MISSING only, so one task read 「⚠ … 8299 缺價/過期」 on its card and 「R4 價格資料 ✓ 通過」
    # in its own dry run. Two fields, two levels, one source (``insight_service.
    # _price_state_for`` feeds both surfaces): missing → warn (the R4 anomaly card),
    # stale-only → info (the card still generates, off an older close).
    missing_price_symbols: list[str]
    stale_price_symbols: list[str]
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
    """Trigger node: scheduled → ok; ``on_alert`` → ok (event-triggered); otherwise manual →
    warn ("won't auto-run").

    DEF-043 (2026-09-24): this read ``scheduled`` alone, so an ``on_alert`` task — which by
    design can never carry a schedule (``PUT …/schedule`` refuses it, spec 03) and runs every
    time a subscribed alert fires — read 「未排程（手動）・不會自動執行」 and was counted under
    需注意 on the pipeline page, while the task's own dry-run G1 said 「由風險預警事件觸發」 ✓
    and ``alert_scan`` was dispatching it. The scope decides first, exactly as G1
    (``insight_service._g0_g1``) does: one task, one answer to "how does this run?".
    """
    if f.scope == "on_alert":
        return NodeState(lv="ok", text="預警觸發", sub="風險預警命中時自動執行")
    if not f.scheduled:
        return NodeState(lv="warn", text="未排程（手動）", sub="不會自動執行")
    return NodeState(lv="ok", text="已排程")


def _input_head(f: PipelineFacts) -> str:
    """The input node's head text — decided by SCOPE, BEFORE any branch (M2, 2026-09-16).

    ``universe_symbols`` is only a symbol list for ``per_symbol``; it is the held MARKET
    codes for ``per_market`` and empty for portfolio/on_alert. The scope-aware label existed
    only on the ok branch, so a warning task fell back to ``len(universe_symbols)`` and
    printed the counts measured on the demo site: 「0 檔標的」 for a portfolio task and
    「3 檔標的」 for a per_market one (its 3 markets) — both above a list of 14 symbols. Deciding
    the head once, before the branches, is what makes the warn/info subs impossible to
    mislabel again.
    """
    if f.scope == "per_symbol":
        return f"{len(f.universe_symbols)} 檔標的"
    if f.scope == "per_market":
        return f"{len(f.universe_symbols)} 個市場"
    return "全持倉"  # portfolio / on_alert: the whole book, no universe of its own


def _input(f: PipelineFacts) -> NodeState:
    """Input node: empty universe (R2) → fail; missing price (R4 source) → warn; a stale
    price or a recent R2 auto-removal → info. Portfolio/on_alert have no universe lifecycle.

    M8: missing and stale are separate levels because the dry-run preflight already treats
    them differently (R4 tests MISSING only). Flattening them into one warn is what made the
    two surfaces contradict each other on the same task; see :class:`PipelineFacts`.
    """
    if f.scope == "per_symbol" and not f.universe_symbols:
        return NodeState(lv="fail", text="標的宇宙為空", sub="清單已出清")
    head = _input_head(f)
    if f.missing_price_symbols:
        sub = f"{', '.join(f.missing_price_symbols)} 缺價"
        if f.stale_price_symbols:
            # Both present: warn wins, but the stale list is still named — dropping it would
            # lose information the dry run reports.
            sub += f"；{', '.join(f.stale_price_symbols)} 價格過期"
        return NodeState(lv="warn", text=head, sub=sub)
    if f.stale_price_symbols:
        joined = ", ".join(f.stale_price_symbols)
        return NodeState(lv="info", text=head, sub=f"價格過期（以舊價產生）：{joined}")
    if f.removed_recently:
        joined = ", ".join(f.removed_recently)
        return NodeState(lv="info", text=head, sub=f"近期移除：{joined}")
    return NodeState(lv="ok", text=head)


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
        return NodeState(lv="warn", text="範圍不相容", sub="既有模板含「單一標的」變數")
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
