"""Regression tests for the official prompt-template library (P2 batch 3: v2.5 checkup).

The library is the shipped best-default content — versioned so a future update can offer
"official has a newer version" upgrades. These tests pin the health-check strategy's v2.5
advance (library official-v5): it must cite {{rule_signals_json}}, keep the interpret-only
guardrail, frame unheld symbols as 建倉評估, and NOT change the task presets (which
reference strategies BY NAME, so a version bump needs no preset change).
"""

from portfolio_dash.llm_insight import official_templates as ot


def test_library_version_is_official_v5() -> None:
    assert ot.LIBRARY_VERSION == "official-v5 (2026-07-11)"


def test_checkup_strategy_advances_to_v25_citing_rule_signals() -> None:
    checkup = next(t for t in ot.STRATEGY_TEMPLATES if t["name"] == "個股健檢策略")
    assert checkup["version"] == "v2.5"
    body = checkup["body"]
    assert "{{rule_signals_json}}" in body                 # the new section cites the var
    assert "TechScore" in body                             # cite TechScore + coverage
    assert "建倉評估" in body                              # unheld-symbol entry framing
    # interpret-only, never recompute (the hard invariant: LLM never computes numbers).
    assert "不重算" in body and "不虛構" in body
    assert "法則訊號資料不足" in body                     # honest degrade instruction


def test_presets_reference_strategies_by_name_no_preset_change() -> None:
    # A strategy version bump must not orphan a preset: presets reference by NAME, and every
    # referenced strategy still resolves in the library (so no preset edit was needed).
    template_names = {t["name"] for t in ot.STRATEGY_TEMPLATES}
    for preset in ot.TASK_PRESETS:
        assert preset["strategy"] in template_names
    # the checkup preset specifically still points at the (now v2.5) 個股健檢策略.
    checkup_preset = next(p for p in ot.TASK_PRESETS if p["preset_key"] == "checkup")
    assert checkup_preset["strategy"] == "個股健檢策略"


def test_library_wire_exposes_v25_checkup() -> None:
    wire = ot.library_wire()
    assert wire["library_version"] == "official-v5 (2026-07-11)"
    strategies = wire["strategies"]
    assert isinstance(strategies, list)
    checkup = next(t for t in strategies if t["name"] == "個股健檢策略")
    assert checkup["version"] == "v2.5"
    assert "{{rule_signals_json}}" in checkup["body"]
