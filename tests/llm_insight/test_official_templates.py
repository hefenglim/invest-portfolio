"""Regression tests for the official prompt-template library (P2 batch 3: v2.5 checkup).

The library is the shipped best-default content — versioned so a future update can offer
"official has a newer version" upgrades. These tests pin the health-check strategy's v2.5
advance (library official-v5): it must cite {{rule_signals_json}}, keep the interpret-only
guardrail, frame unheld symbols as 建倉評估, and NOT change the task presets (which
reference strategies BY NAME, so a version bump needs no preset change).
"""

from portfolio_dash.llm_insight import official_templates as ot


def test_library_version_is_official_v8() -> None:
    assert ot.LIBRARY_VERSION == "official-v8 (2026-07-19)"


def test_ai_input_prompt_is_code_owned_here_not_in_library_wire() -> None:
    # FU-D20: the AI-parse prompt is centralized here as a versioned, code-owned constant.
    body = ot.AI_INPUT_PROMPT_BODY
    assert ot.AI_INPUT_PROMPT_VERSION  # a version tag exists
    # the three dynamic placeholders that agents.py fills at call time.
    for placeholder in ("{accounts}", "{today}", "{text}"):
        assert placeholder in body
    # literal JSON braces stay escaped so only those placeholders interpolate.
    assert '{{"drafts"' in body
    assert "recent PAST occurrence" in body       # date-anchor rule preserved
    assert "MULTIPLE transactions" in body        # the screenshot extension
    # deliberately NOT exposed in the user-facing library payload.
    wire = ot.library_wire()
    assert "AI_INPUT_PROMPT_BODY" not in wire
    assert body not in str(wire.get("system_prompt", "")) + str(wire.get("strategies", ""))


def test_ai_input_prompt_v3_pins_local_exchange_code_rule() -> None:
    # FU-D41 (owner bug): 「前天聯電買入1張」 on a tw_broker row parsed to the US ADR
    # ticker "UMC" → dead lookup. The v3 prompt must carry the explicit LOCAL-exchange-code
    # rule with the numeric-code examples and the ADR counter-example.
    assert ot.AI_INPUT_PROMPT_VERSION == "v3"
    body = ot.AI_INPUT_PROMPT_BODY
    assert "LOCAL exchange code" in body
    assert "聯電⇒2303" in body and "台積電⇒2330" in body and "鴻海⇒2317" in body
    assert "UMC" in body and "TSM" in body        # the never-an-ADR counter-example
    assert "Bursa" in body                        # MY accounts take the Bursa code
    # the rule text must survive .format (no stray placeholders were introduced).
    rendered = body.format(accounts="a=b (TWD)", today="2026-07-19", text="x")
    assert "聯電⇒2303" in rendered


def test_ai_symbol_resolve_prompt_is_registered_and_versioned() -> None:
    # FU-D42c: the 「AI 判讀代號」 fallback prompt lives in the registry (code-owned) and
    # instructs the same local-exchange-code rules; the reply is suggestion-only (the real
    # lookup re-verifies — stated in the prompt so the model never claims authority).
    assert ot.AI_SYMBOL_RESOLVE_PROMPT_VERSION == "v1"
    body = ot.AI_SYMBOL_RESOLVE_PROMPT
    for placeholder in ("{query}", "{market}"):
        assert placeholder in body
    assert "聯電⇒2303" in body and "UMC" in body   # local-code rule + ADR counter-example
    assert "真實報價查核" in body                    # verification stays with the lookup
    rendered = body.format(query="聯電", market="TW")
    assert "聯電" in rendered and '{{"symbol"' not in rendered  # braces unescaped by format
    entry = next(e for e in ot.PROMPT_REGISTRY if e["key"] == "ai_symbol_resolve")
    assert entry["tier"] == "code-owned"
    assert entry["default_constant"] == "AI_SYMBOL_RESOLVE_PROMPT"
    assert entry["agent"] == "ai_symbol_resolve"


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
    assert wire["library_version"] == "official-v8 (2026-07-19)"
    strategies = wire["strategies"]
    assert isinstance(strategies, list)
    checkup = next(t for t in strategies if t["name"] == "個股健檢策略")
    assert checkup["version"] == "v2.5"
    assert "{{rule_signals_json}}" in checkup["body"]
