"""Regression tests for the official prompt-template library (P2 batch 3: v2.5 checkup).

The library is the shipped best-default content — versioned so a future update can offer
"official has a newer version" upgrades. These tests pin the health-check strategy's v2.5
advance (library official-v5): it must cite {{rule_signals_json}}, keep the interpret-only
guardrail, frame unheld symbols as 建倉評估, and NOT change the task presets (which
reference strategies BY NAME, so a version bump needs no preset change).
"""

from portfolio_dash.llm_insight import official_templates as ot


def test_library_version_is_official_v10() -> None:
    assert ot.LIBRARY_VERSION == "official-v10 (2026-07-21)"


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


def test_ai_input_prompt_v4_pins_local_exchange_code_rule() -> None:
    # FU-D41 (owner bug): 「前天聯電買入1張」 on a tw_broker row parsed to the US ADR
    # ticker "UMC" → dead lookup. The prompt must carry the explicit LOCAL-exchange-code
    # rule with the numeric-code examples and the ADR counter-example. v4 (W1 batch-A) adds
    # the parity MY (Bursa) guidance (pinned by test_prompts_v2_carry_my_bursa_guidance).
    assert ot.AI_INPUT_PROMPT_VERSION == "v4"
    body = ot.AI_INPUT_PROMPT_BODY
    assert "LOCAL exchange code" in body
    assert "聯電⇒2303" in body and "台積電⇒2330" in body and "鴻海⇒2317" in body
    assert "UMC" in body and "TSM" in body        # the never-an-ADR counter-example
    assert "Bursa" in body                        # MY accounts take the Bursa code
    # the rule text must survive .format (no stray placeholders were introduced).
    rendered = body.format(accounts="a=b (TWD)", today="2026-07-19", text="x")
    assert "聯電⇒2303" in rendered


def test_ai_instrument_resolve_prompt_is_registered_and_versioned() -> None:
    # R6-B: the UNIFIED 「AI 標的判讀」 prompt SUPERSEDES the former ai_sector + ai_symbol_resolve
    # prompts. It lives in the registry (code-owned), carries the local-exchange-code rules +
    # the embedded GICS sector vocabulary + all reply-schema fields, and states that the real
    # lookup re-verifies (so the model claims no authority).
    assert ot.AI_INSTRUMENT_RESOLVE_PROMPT_VERSION == "v2"
    body = ot.AI_INSTRUMENT_RESOLVE_PROMPT
    for placeholder in ("{query}", "{market}"):
        assert placeholder in body
    assert "聯電⇒2303" in body and "UMC" in body   # local-code rule + ADR counter-example
    assert "真實報價覆核" in body                    # verification stays with the lookup
    # the single-reply schema fields (symbol/name resolution + GICS classify + candidates).
    for field in ("gics_sector", "gics_industry", "confidence", "candidates", "not_found"):
        assert field in body
    rendered = body.format(query="聯電", market="TW")
    assert "聯電" in rendered and '{{"symbol"' not in rendered  # braces unescaped by format
    entry = next(e for e in ot.PROMPT_REGISTRY if e["key"] == "ai_instrument_resolve")
    assert entry["tier"] == "code-owned"
    assert entry["default_constant"] == "AI_INSTRUMENT_RESOLVE_PROMPT"
    assert entry["agent"] == "ai_instrument_resolve"
    # the two former single-purpose prompts (and their keys) are GONE.
    assert not hasattr(ot, "AI_SECTOR_PROMPT")
    assert not hasattr(ot, "AI_SYMBOL_RESOLVE_PROMPT")
    assert not any(e["key"] in ("ai_sector", "ai_symbol_resolve") for e in ot.PROMPT_REGISTRY)


def test_prompts_v2_carry_my_bursa_guidance() -> None:
    """W1 batch-A: the MY (Bursa) clause is raised to TW parity in BOTH the unified resolve
    prompt and the AI-input prompt — verified name⇒code exemplars, the ACE-market leading-zero
    rule, and the brand/mall→listed-parent rule. Guards against silent regression to the old
    one-line MY clause (「MY（馬股，Bursa）：4 位數字（如 5225）」)."""
    resolve = ot.AI_INSTRUMENT_RESOLVE_PROMPT
    # verified name⇒code exemplars (each confirmed against the fetched Bursa directory).
    for pair in ("Maybank／馬銀行⇒1155", "Public Bank／大眾銀行⇒1295",
                 "Tenaga Nasional／國家能源⇒5347", "CIMB⇒1023", "Inari Amertron⇒0166",
                 "IOI Corporation⇒1961", "IOI Properties⇒5249"):
        assert pair in resolve, pair
    # leading-zero rule (ACE codes keep the zero — 0166, never 166).
    assert "保留前導零" in resolve and "絕不可寫成 166" in resolve
    # brand/mall/subsidiary → LISTED parent, else not_found (never fabricate).
    assert "上市母公司" in resolve and "IOI Mall" in resolve
    # the AI-input prompt mirrors the same MY guidance (condensed).
    body = ot.AI_INPUT_PROMPT_BODY
    assert "Maybank⇒1155" in body and "Inari⇒0166" in body
    assert "0166, never 166" in body and "IOI Mall⇒IOI Properties 5249" in body
    # both still .format cleanly (the MY expansion introduced no stray placeholder).
    assert "0166" in resolve.format(query="Inari", market="MY")
    assert "0166" in body.format(accounts="a=b", today="2026-07-21", text="x")


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
    assert wire["library_version"] == "official-v10 (2026-07-21)"
    strategies = wire["strategies"]
    assert isinstance(strategies, list)
    checkup = next(t for t in strategies if t["name"] == "個股健檢策略")
    assert checkup["version"] == "v2.5"
    assert "{{rule_signals_json}}" in checkup["body"]
