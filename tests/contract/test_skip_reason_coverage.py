"""Every job-run reason code the backend can emit has a Chinese label in the frontend.

``web/pipeline.js`` renders a job run's reason as ``SKIP_REASONS[code] || code`` — so an
unmapped code does not fail loudly, it prints the raw identifier onto the page. That is
what happened to the four ``*_mid_run`` codes: ``generate.py`` has emitted them since the
budget guard shipped, none was ever added to the table, and the owner would have read
``budget_exhausted_mid_run`` in the 管線 view (found 2026-08-05 by the 0->1 sweep, while
checking whether an English string was reachable — it was).

This is the dual-surface drift class: two files must agree, nothing made them. The fix is
the check, not the four labels — those would rot again by the next release.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_PIPELINE_JS = _ROOT / "web" / "pipeline.js"
_LLM_INSIGHT = _ROOT / "portfolio_dash" / "llm_insight"
_LLM_CONFIG = _ROOT / "portfolio_dash" / "shared" / "llm_config.py"


def _frontend_codes() -> set[str]:
    """The keys of ``SKIP_REASONS`` in web/pipeline.js."""
    src = _PIPELINE_JS.read_text(encoding="utf-8")
    block = re.search(r"var SKIP_REASONS = \{(.*?)\n  \};", src, re.S)
    assert block, "SKIP_REASONS table not found in web/pipeline.js"
    return set(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", block.group(1), re.M))


def _backend_codes() -> set[str]:
    """Every literal that can reach ``job_runs.reason``.

    Two shapes: a plain ``reason="R1_scope_mismatch"`` keyword, and the composed
    ``f"{exc.kind}_mid_run"`` — the composed one is expanded from the ``kind`` class
    attribute of every :class:`LLMError` subclass, so a NEW subclass is caught too.
    """
    codes: set[str] = set()
    for path in sorted(_LLM_INSIGHT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg == "reason" and isinstance(kw.value, ast.Constant) \
                        and isinstance(kw.value.value, str) and kw.value.value:
                    codes.add(kw.value.value)
            # the literal branch of the mid-run ternary, e.g. "budget_exhausted_mid_run"
        for node in ast.walk(tree):
            # `.startswith("_")` skips the f-string's own trailing fragment: the literal
            # parts of f"{exc.kind}_mid_run" include the bare suffix "_mid_run", which is
            # a piece of a code, not one.
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and node.value.endswith("_mid_run") and not node.value.startswith("_"):
                codes.add(node.value)

    # `f"{exc.kind}_mid_run"` — one code per LLMError subclass in llm_config.py
    cfg = ast.parse(_LLM_CONFIG.read_text(encoding="utf-8"))
    for node in cfg.body:
        if not isinstance(node, ast.ClassDef):
            continue
        if not any(getattr(b, "id", "") .endswith("Error") or getattr(b, "id", "") == "Exception"
                   or getattr(b, "id", "").startswith("LLM") for b in node.bases):
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant) \
                    and any(getattr(t, "id", None) == "kind" for t in stmt.targets):
                kind = stmt.value.value
                # `ast.Constant.value` is Any — a bytes literal would interpolate as
                # "b'...'" and quietly compare unequal to every frontend key, i.e. the
                # check would pass by never matching. Require a str.
                if isinstance(kind, str):
                    codes.add(f"{kind}_mid_run")
    return codes


def test_every_backend_reason_code_has_a_chinese_label() -> None:
    missing = sorted(_backend_codes() - _frontend_codes())
    # LLMBudgetExceeded is special-cased in generate.py to the clearer
    # "budget_exhausted_mid_run", so its own kind never reaches the wire.
    missing = [c for c in missing if c != "budget_exceeded_mid_run"]
    assert not missing, (
        "these job-run reason codes would render as raw English identifiers — add a label "
        f"to SKIP_REASONS in web/pipeline.js:\n  {missing}"
    )


def test_the_check_sees_both_sides() -> None:
    """Detection power: a set-difference test passes trivially if either side comes back
    empty, so assert both are populated and that the known codes are really present."""
    front, back = _frontend_codes(), _backend_codes()
    assert len(front) >= 10, front
    assert len(back) >= 10, back
    assert "R6_quota" in front and "R6_quota" in back
    assert "budget_exhausted_mid_run" in back, "the composed mid-run code was not extracted"
    assert "llm_unavailable_mid_run" in back, "LLMError subclasses were not expanded"
