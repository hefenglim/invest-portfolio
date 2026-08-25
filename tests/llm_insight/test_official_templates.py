"""Regression tests for the official prompt-template library (P2 batch 3: v2.5 checkup).

The library is the shipped best-default content — versioned so a future update can offer
"official has a newer version" upgrades. These tests pin the health-check strategy's v2.5
advance (library official-v5): it must cite {{rule_signals_json}}, keep the interpret-only
guardrail, frame unheld symbols as 建倉評估, and NOT change the task presets (which
reference strategies BY NAME, so a version bump needs no preset change).
"""

import json
import re

from portfolio_dash.llm_insight import official_templates as ot


def test_library_version_is_official_v15() -> None:
    assert ot.LIBRARY_VERSION == "official-v19 (2026-08-26)"


def test_ai_input_prompt_is_code_owned_here_not_in_library_wire() -> None:
    # FU-D20: the AI-parse prompt is centralized here as a versioned, code-owned constant.
    body = ot.AI_INPUT_PROMPT_BODY
    assert ot.AI_INPUT_PROMPT_VERSION  # a version tag exists
    # the three dynamic placeholders that agents.py fills at call time.
    for placeholder in ("{accounts}", "{today}", "{text}"):
        assert placeholder in body
    # literal JSON braces stay escaped so only those placeholders interpolate.
    assert '{{"rows"' in body
    assert "recent PAST occurrence" in body       # date-anchor rule preserved
    assert "MULTIPLE rows" in body                # the screenshot extension (v6 wording)
    # deliberately NOT exposed in the user-facing library payload.
    wire = ot.library_wire()
    assert "AI_INPUT_PROMPT_BODY" not in wire
    assert body not in str(wire.get("system_prompt", "")) + str(wire.get("strategies", ""))


def test_ai_input_prompt_v6_pins_local_exchange_code_rule() -> None:
    # FU-D41 (owner bug): 「前天聯電買入1張」 on a tw_broker row parsed to the US ADR
    # ticker "UMC" → dead lookup. The prompt must carry the explicit LOCAL-exchange-code
    # rule with the numeric-code examples and the ADR counter-example. v4 (W1 batch-A) adds
    # the parity MY (Bursa) guidance (pinned by test_prompts_v2_carry_my_bursa_guidance);
    # v5 (W3 batch-B) adds the merged multi-market clause + optional ``market`` output field;
    # v6 (W4, AI-D17/D19) turns the door into the three-kind discriminated union.
    assert ot.AI_INPUT_PROMPT_VERSION == "v7"  # R6 added div ex_date
    body = ot.AI_INPUT_PROMPT_BODY
    assert "LOCAL exchange code" in body
    assert "聯電⇒2303" in body and "台積電⇒2330" in body and "鴻海⇒2317" in body
    assert "UMC" in body and "TSM" in body        # the never-an-ADR counter-example
    assert "Bursa" in body                        # MY accounts take the Bursa code
    # v5 merged multi-market guidance + the optional market output field in the schema.
    # ("merged account" is wrap-tolerant: the prompt's line breaks are not the contract.)
    assert "merged" in body and "MULTIPLE markets" in body and "STOCK'S market" in body
    assert '"market"' in body                     # schema carries the optional market field
    assert "field to that market's value" in body
    # v6 (AI-D17): the union's three discriminators + the unparsed confession list.
    assert '"kind":"txn"' in body and '"kind":"div"' in body and '"kind":"cash"' in body
    assert "unparsed" in body
    # v6 (AI-D19): the two flags teach their EXPLICIT-only semantics.
    assert "explicitly says 當沖" in body
    assert "放空／融券／short sell" in body
    # the rule text must survive .format (no stray placeholders were introduced).
    rendered = body.format(accounts="a=b (USD:US＋MYR:MY)", today="2026-07-19", text="x")
    assert "聯電⇒2303" in rendered and "STOCK'S market" in rendered


def test_ai_input_prompt_example_outputs_are_valid_json() -> None:
    """Every ``<example_output>`` must parse as strict JSON — the one-shot example is the
    model's strongest anchor (llm-insight.md), and the completion layer parses STRICTLY.
    The v6 cash example shipped a raw newline INSIDE a string value (a Python string
    concatenation break mid-token), teaching by an example the parser itself would reject.
    Line breaks BETWEEN tokens are fine; inside a string they are not."""
    rendered = ot.AI_INPUT_PROMPT_BODY.format(
        accounts="a=b (TWD)", today="2026-08-18", text="x")
    blocks = re.findall(r"<example_output>(.*?)</example_output>", rendered, re.S)
    assert blocks, "the prompt lost its one-shot examples entirely"
    for blk in blocks:
        json.loads(blk)  # raises on any invalid example


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


def test_checkup_strategy_advances_to_v26_citing_rule_signals() -> None:
    checkup = next(t for t in ot.STRATEGY_TEMPLATES if t["name"] == "個股健檢策略")
    assert checkup["version"] == "v2.8"
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
    # the checkup preset specifically still points at the (now v2.8) 個股健檢策略.
    checkup_preset = next(p for p in ot.TASK_PRESETS if p["preset_key"] == "checkup")
    assert checkup_preset["strategy"] == "個股健檢策略"


def test_library_wire_exposes_v26_checkup() -> None:
    wire = ot.library_wire()
    assert wire["library_version"] == "official-v19 (2026-08-26)"
    strategies = wire["strategies"]
    assert isinstance(strategies, list)
    checkup = next(t for t in strategies if t["name"] == "個股健檢策略")
    assert checkup["version"] == "v2.8"
    assert "{{rule_signals_json}}" in checkup["body"]


# --- W2: the assistant — 持倉建議與提點 + the 提點 on_alert card (AI-D1/D5, 2026-08-16) -------


def test_advice_template_holds_the_product_red_lines() -> None:
    """AI-D8: advice is directional + conditional, never a size or an order. The body must
    forbid position sizing, and AI-D10 leaves prediction FREE (the schema already requires a
    confidence whenever one is present — that guard is in cards.py, not here)."""
    advice = next(t for t in ot.STRATEGY_TEMPLATES if t["name"] == "持倉建議與提點策略")
    assert advice["scope"] == "per_symbol"
    body = advice["body"]
    assert "不給部位大小" in body and "不給買賣金額" in body
    # The two moving-average crosses are named apart (AI-D2): the prompt must tell the model
    # which window is which, or the assistant quotes one as the other.
    assert "ma_cross_short" in body and "20/60" in body
    assert "ma_cross" in body and "50/200" in body
    # AI-D10: prediction is the model's to give or withhold, never forced.
    assert "prediction 由你決定" in body and "confidence" in body


def test_advice_template_v3_carries_fundamentals_with_the_never_average_rule() -> None:
    """W3 (AI-D14/15): the advice body cites {{fundamentals_json}}, names the per-source
    blocks, and carries the red line in its prompt-enforced form — different values across
    sources are reported side by side with their sources, NEVER averaged or reconciled."""
    advice = next(t for t in ot.STRATEGY_TEMPLATES if t["name"] == "持倉建議與提點策略")
    assert advice["version"] == "v3.2"
    body = advice["body"]
    assert "{{fundamentals_json}}" in body
    assert "不得取平均" in body
    assert "標注來源" in body
    # The canonical field names the model will see (the block key is the provenance).
    assert "pe_ratio" in body and "roe_pct" in body
    preset = next(p for p in ot.TASK_PRESETS if p["preset_key"] == "advice")
    assert preset["version"] == "v3.2"


def test_advice_template_v3_cites_the_backtest_and_anchors_confidence() -> None:
    """W7 (AI-D33) + W7.1: the advice body cites all three W6 variables under the citation
    discipline, and anchors confidence on the PRECOMPUTED ceiling."""
    body = next(t for t in ot.STRATEGY_TEMPLATES if t["name"] == "持倉建議與提點策略")["body"]
    assert "{{signal_backtest_json}}" in body
    assert "{{backtest_json}}" in body and "{{calibration_gap_json}}" in body
    # The citation law: verbatim numbers, n<8 cites nothing, sample count + baseline.
    assert "樣本不足" in body and "基線" in body
    # W7.1 — the law now points at ONE number the backend computed. The three-step walk over
    # the bins table is gone: the first live run had 0 of 13 cards execute it.
    assert "confidence_ceiling" in body
    assert "不得超過 confidence_ceiling" in body
    # AI-D39 (2026-08-25): the old assertion pinned 「為 0 時」. With the rolling-gap step
    # removed the ceiling can no longer reach 0 (CEILING_HEADROOM puts the minimum at 5),
    # so that branch became text the model reads and can never act on. The degenerate
    # case is still spelled out — it now keys on a LOW ceiling, which is reachable.
    assert "為 0 時" not in body, "an unreachable branch must not stay in a prompt"
    assert "偏低" in body and "情境與觸發條件" in body
    # And it still forbids the failure mode that clause exists to prevent.
    assert "不要為了讓卡片看起來有用" in body
    # The pre-existing red lines survive the rewrite.
    assert "不給部位大小" in body and "prediction 由你決定" in body


def test_advice_template_states_the_return_unit_and_forbids_substitution() -> None:
    """W7.1 — the two output defects the first live run produced, closed in the prompt.

    The unit existed ONLY in the variable registry's `desc` (UI documentation the model
    never sees), so a fraction was printed as 「0.1336%」 — the true value 100× smaller. And
    a sub-gate cell with no mean had its same-window BASELINE printed as the event return.
    """
    body = next(t for t in ot.STRATEGY_TEMPLATES if t["name"] == "持倉建議與提點策略")["body"]
    assert "0.1336" in body and "+13.36%" in body      # the unit, by worked example
    assert "分數，不是百分比" in body
    assert "不得改用同窗" in body and "baseline" in body  # no substitution
    assert "輸入裡沒有的數字一律不得出現" in body        # no fabrication


def test_checkup_v26_cites_the_event_study() -> None:
    """W7 (AI-D33): the checkup cites the per-symbol event study (no anchoring — its
    confidence law stays the plain 寧可保守 one). W7.1 adds the unit + no-substitution rule,
    because the same 100× defect appeared in the checkup cards too."""
    body = next(t for t in ot.STRATEGY_TEMPLATES if t["name"] == "個股健檢策略")["body"]
    assert "{{signal_backtest_json}}" in body
    assert "尚無回測歷史" in body
    assert "分數不是百分比" in body
    assert "不得改用 baseline 的數字" in body


def test_weekly_template_v22_cites_ai_track_record() -> None:
    """W7 (AI-D34): the weekly report narrates the AI's own calibration — both
    portfolio-scope vars cited; the pure-narrative law (no prediction) is intact."""
    weekly = next(t for t in ot.STRATEGY_TEMPLATES if t["name"] == "持倉週報策略")
    assert weekly["version"] == "v2.4"
    body = weekly["body"]
    assert "{{backtest_json}}" in body and "{{calibration_gap_json}}" in body
    assert "prediction 留空" in body  # the weekly stays narrative-only
    # W7.1 — the live v2.2 card read gap −0.466 as 「低估自身表現」 (the opposite) even though
    # this same section states the convention. The direction now ships as copyable text.
    assert "照抄 calibration_gap_json.reading" in body


def test_advice_and_alert_presets_resolve_and_subscribe() -> None:
    """The two W2 presets reference real strategies, and the 提點 one carries its six-rule
    subscription (AI-D11) — an on_alert task with no alert_rules subscribes to nothing, so the
    subscription has to be pinned here, not assumed."""
    template_names = {t["name"] for t in ot.STRATEGY_TEMPLATES}
    advice = next(p for p in ot.TASK_PRESETS if p["preset_key"] == "advice")
    assert advice["strategy"] in template_names
    assert advice["scope"] == "per_symbol" and advice["self_correct"] is True
    alert = next(p for p in ot.TASK_PRESETS if p["preset_key"] == "alert_advice")
    assert alert["scope"] == "on_alert"
    assert alert["strategy"] == "持倉提點策略"
    assert set(alert["alert_rules"]) == {
        "target_cross", "single_weight", "fx_drift",
        "drawdown_from_peak", "vol_spike", "consensus_change",
    }
    # The wildcard and the data-health rules are deliberately NOT subscribed (AI-D11).
    assert "all" not in alert["alert_rules"]
    assert "missing_price" not in alert["alert_rules"]
    assert "quota_low" not in alert["alert_rules"]
    # The alert card's addendum applies the ≤3-trading-day window; the preset horizon agrees.
    assert alert["horizon_days"] == 3


def test_on_alert_advice_body_is_short_horizon_and_no_sizing() -> None:
    body = next(t for t in ot.STRATEGY_TEMPLATES if t["name"] == "持倉提點策略")["body"]
    assert "≤3 個交易日" in body
    assert "不給部位大小" in body
    assert "ma_cross_short" in body  # the cross disambiguation carries into the alert card too

