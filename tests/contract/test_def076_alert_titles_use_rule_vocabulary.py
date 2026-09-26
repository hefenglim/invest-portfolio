"""DEF-076 (owner ruling ⑤ b, 2026-09-26): an alert's TITLE never names its rule with a
different word than the rule's NAME does.

``quota_low`` was named 「AI 額度偏低」 (``shared/alert_rule_names.py``, the owner's DEF-062
wording) while its event title read 「LLM 額度偏低」 (``strategy/alerts.py``) — so the 預警 bell
listed 「LLM 額度偏低」 under a rule the settings page, the push and the insight card's 觸發 chip
all call 「AI 額度偏低」. Ruling: one word, 「AI」. The owner accepted the one-time same-day
insight-cache miss this causes (the alert title is part of the alert card's prompt).

**The pairs are DERIVED FROM THE CODE, not listed by hand.** Every call anywhere in
``portfolio_dash`` that passes a literal ``title=`` together with a literal ``rule=`` /
``rule_id=`` is a (rule, title) pair; the title's subject placeholder (``{sym}``,
``{sector}`` …) is removed and what remains — the predicate — is compared with the rule's
name. A predicate that contains the name, or is contained in it, uses the rule's own
vocabulary. Anything else must be a REVIEWED difference below, each with its reason; a
reviewed entry that no longer differs (or no longer exists) fails too, so the list cannot rot.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from portfolio_dash.shared.alert_rule_names import rule_name
from portfolio_dash.strategy.alerts import compute_alerts_from
from portfolio_dash.strategy.rules_config import DEFAULT_RULES, RULE_IDS
from tests.strategy.test_alerts import _minimal_data

_PKG = Path(__file__).resolve().parents[2] / "portfolio_dash"

#: Titles that describe the INSTANCE (its subject and the state that fired) in words other
#: than the category name — reviewed 2026-09-26 (DEF-076 class scan), none contradicts the
#: rule's vocabulary. Keyed by (rule id, predicate). Changing any of them is an owner call:
#: the title is fed to the alert card's prompt, so every change is a cache miss.
_REVIEWED: dict[tuple[str, str], str] = {
    ("single_weight", "單一持股權重偏高"): "states the fired condition (weight above the "
        "threshold) of the named holding; 「集中度」 is the category",
    ("sector_weight", "產業權重偏高"): "same shape as single_weight, for a sector",
    ("currency_weight", "幣別權重偏高"): "same shape as single_weight, for a currency",
    ("stale_price", "報價過期"): "「報價」 is the quote the check reads; candidate for "
        "「價格過期」 if the owner wants the category word — not ruled",
    ("missing_price", "無報價"): "states the fact (no quote); candidate for 「缺價」 if the "
        "owner wants the category word — not ruled",
    ("fx_drift", "匯率偏離成本"): "names what drifted from what (spot vs the pool's cost "
        "rate) for the named account",
    ("rebalance_drift", "偏離目標配置"): "names what the holding drifted from",
    ("target_cross", "跌破目標價"): "one DIRECTION of the rule 「目標價穿越」",
    ("target_cross", "突破目標價"): "the other direction of 「目標價穿越」",
    ("portfolio_drawdown", "組合自高點回撤"): "names the reference (the portfolio's own "
        "peak); keeps 「組合」, so it can never be read as the per-symbol 「高點回撤」",
}


@dataclass(frozen=True)
class _Pair:
    where: str
    rule: str
    predicate: str


def _literal(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _predicate(node: ast.expr) -> str | None:
    """The title with its formatted placeholders removed, whitespace-trimmed."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.strip()
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value for v in node.values
            if isinstance(v, ast.Constant) and isinstance(v.value, str)
        ).strip()
    return None


def _pairs() -> list[_Pair]:
    out: list[_Pair] = []
    for path in sorted(_PKG.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            kw = {k.arg: k.value for k in node.keywords if k.arg}
            rule = next((r for r in (_literal(kw[k]) for k in ("rule", "rule_id") if k in kw)
                         if r), None)
            if rule is None or "title" not in kw:
                continue
            pred = _predicate(kw["title"])
            if pred is None:
                continue
            out.append(_Pair(f"{path.relative_to(_PKG.parent)}:{node.lineno}", rule, pred))
    return out


def _same_vocabulary(pair: _Pair) -> bool:
    name = rule_name(pair.rule)
    return name in pair.predicate or pair.predicate in name


def test_the_scan_sees_every_registered_rule() -> None:
    """The derivation cannot silently miss the producers: every rule has a title site."""
    found = {p.rule for p in _pairs()}
    missing = [rid for rid in RULE_IDS if rid not in found]
    assert not missing, f"no literal title found for {missing} — the AST scan went blind"


def test_no_title_names_its_rule_in_other_words() -> None:
    bad = [
        f"{p.where}: rule {p.rule} 「{rule_name(p.rule)}」 vs title predicate 「{p.predicate}」"
        for p in _pairs()
        if not _same_vocabulary(p) and (p.rule, p.predicate) not in _REVIEWED
    ]
    assert not bad, "alert title differs from its rule's name:\n" + "\n".join(bad)


def test_every_reviewed_difference_still_exists_and_still_differs() -> None:
    live = {(p.rule, p.predicate) for p in _pairs() if not _same_vocabulary(p)}
    stale = sorted(k for k in _REVIEWED if k not in live)
    assert not stale, f"reviewed entries no longer match the code — remove them: {stale}"


def test_no_alert_title_says_llm() -> None:
    """The owner's word for the model is 「AI」 (ruling ⑤ b) — on every alert title."""
    hits = [f"{p.where}: 「{p.predicate}」" for p in _pairs() if "LLM" in p.predicate.upper()]
    assert not hits, hits


def test_the_quota_alert_carries_the_rule_name_as_its_title() -> None:
    """Through the engine: the title the bell lists, the alert scan records into
    ``alert_events`` and the alert card's prompt reads is the rule's own name."""
    alerts = compute_alerts_from(
        _minimal_data(fx=None, calendar=[]), DEFAULT_RULES,
        quota_remaining=Decimal("0.5"), quota_threshold=Decimal("1"))
    quota = next(a for a in alerts if a.rule == "quota_low")
    assert quota.title == rule_name("quota_low") == "AI 額度偏低"
