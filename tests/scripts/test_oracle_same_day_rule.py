"""The stress-audit oracle transcribes the same-day rule of 2026-09-23 (DEF-012).

The oracle keeps its OWN ``EventPriority`` on purpose (spec §7.4 — an oracle that imports
the implementation's ordering cannot detect an error in it), which is exactly why a rule
change has to reach it by hand and why this test exists: two independent copies of one
rule are only a check when both say the same thing. Read by AST, not imported —
``scripts/stress_audit`` is outside the mypy gate and outside the app's import graph, and
this file must not pull it into either.
"""

import ast
from pathlib import Path

_ORACLE = Path(__file__).resolve().parents[2] / "scripts" / "stress_audit" / "oracle.py"


def _event_priority_members() -> dict[str, int]:
    tree = ast.parse(_ORACLE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "EventPriority":
            out: dict[str, int] = {}
            for stmt in node.body:
                if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant):
                    (target,) = stmt.targets
                    assert isinstance(target, ast.Name)
                    assert isinstance(stmt.value.value, int)
                    out[target.id] = stmt.value.value
            return out
    raise AssertionError("oracle.py has no EventPriority class")


def test_the_oracle_ranks_a_days_trades_as_one_priority() -> None:
    members = _event_priority_members()
    assert members == {"OPENING": 0, "CORPORATE_ACTION": 10, "TRADE": 20, "DIVIDEND": 40}
    assert "BUY" not in members and "SELL" not in members, (
        "the oracle re-introduced a side rank; the rule is ledger-id order within TRADE")


def test_both_replays_sort_trades_by_id_under_the_one_rank() -> None:
    src = _ORACLE.read_text(encoding="utf-8")
    assert "EventPriority.BUY" not in src and "EventPriority.SELL" not in src
    assert src.count('events.append((t.trade_date, EventPriority.TRADE, t.id, "tx", t))') == 2
    assert "events.sort(key=lambda e: (e[0], e[1], e[2]))" in src
