"""Unit tests for the runtime gate R1–R8 (spec 4.9) — the single shared gate.

``evaluate_gates`` is a PURE function over a fed :class:`GateContext`; both Loop-1
generation (04b) and the spec-07 preflight reuse it (no gate logic duplicated elsewhere).
Verdict is ``blocked`` (any R1/R2/R3/R6 hard block), ``degraded`` (only soft issues like
R5 unavailable vars or R4 missing prices), or ``clean``.
"""

from decimal import Decimal

from portfolio_dash.llm_insight.gating import GateContext, evaluate_gates


def _reasons(result: object) -> set[str]:
    return {g.reason for g in result.gates if g.reason is not None}  # type: ignore[attr-defined]


# --- clean baseline -----------------------------------------------------------


def test_clean_portfolio_combo() -> None:
    ctx = GateContext(
        scope="portfolio", live_strategy_count=2, budget_remaining=Decimal("5"),
    )
    result = evaluate_gates(ctx)
    assert result.verdict == "clean"
    assert result.gates == [] or all(g.lv == "info" for g in result.gates)


# --- R1 scope × variable mismatch ---------------------------------------------


def test_r1_scope_mismatch_blocks() -> None:
    # a portfolio-scope combo referencing a per_symbol variable in a strategy body
    ctx = GateContext(
        scope="portfolio", live_strategy_count=1, budget_remaining=Decimal("5"),
        strategy_bodies=["watch {{symbol_detail_json}}"],
    )
    result = evaluate_gates(ctx)
    assert result.verdict == "blocked"
    assert "R1_scope_mismatch" in _reasons(result)


def test_r1_per_symbol_scope_allows_per_symbol_var() -> None:
    ctx = GateContext(
        scope="per_symbol", live_strategy_count=1, budget_remaining=Decimal("5"),
        strategy_bodies=["{{symbol_detail_json}}"], universe_symbols=["2330"],
    )
    result = evaluate_gates(ctx)
    assert "R1_scope_mismatch" not in _reasons(result)


# --- R2 universe lifecycle (per_symbol) ---------------------------------------


def test_r2_empty_universe_blocks() -> None:
    ctx = GateContext(
        scope="per_symbol", live_strategy_count=1, budget_remaining=Decimal("5"),
        universe_symbols=[],
    )
    result = evaluate_gates(ctx)
    assert result.verdict == "blocked"
    assert "R2_universe_empty" in _reasons(result)


def test_r2_removed_symbols_are_info_not_block() -> None:
    ctx = GateContext(
        scope="per_symbol", live_strategy_count=1, budget_remaining=Decimal("5"),
        universe_symbols=["2330"], removed_symbols=["OLD"],
    )
    result = evaluate_gates(ctx)
    assert "R2_universe_empty" not in _reasons(result)
    info = [g for g in result.gates if g.id == "R2"]
    assert info and info[0].lv == "info"


# --- R3 all strategies disabled/archived --------------------------------------


def test_r3_no_live_templates_blocks() -> None:
    ctx = GateContext(
        scope="portfolio", live_strategy_count=0, budget_remaining=Decimal("5"),
    )
    result = evaluate_gates(ctx)
    assert result.verdict == "blocked"
    assert "R3_no_live_templates" in _reasons(result)


# --- R4 missing price per symbol ----------------------------------------------


def test_r4_missing_price_flags_degraded_not_blocked() -> None:
    ctx = GateContext(
        scope="per_symbol", live_strategy_count=1, budget_remaining=Decimal("5"),
        universe_symbols=["2330", "AAPL"], missing_price_symbols=["AAPL"],
    )
    result = evaluate_gates(ctx)
    assert result.verdict == "degraded"
    r4 = [g for g in result.gates if g.id == "R4"]
    assert r4 and r4[0].lv == "warn"
    # the gate exposes the data-anomaly symbols so the orchestrator emits a zero-LLM card.
    assert result.data_anomaly_symbols == ["AAPL"]


# --- R5 variable unavailable --------------------------------------------------


def test_r5_unavailable_vars_degrade_but_proceed() -> None:
    ctx = GateContext(
        scope="portfolio", live_strategy_count=1, budget_remaining=Decimal("5"),
        unavailable_vars=["institutional_json"],
    )
    result = evaluate_gates(ctx)
    assert result.verdict == "degraded"
    r5 = [g for g in result.gates if g.id == "R5"]
    assert r5 and r5[0].lv == "info"


# --- R6 budget exhausted ------------------------------------------------------


def test_r6_budget_exhausted_blocks() -> None:
    ctx = GateContext(
        scope="portfolio", live_strategy_count=1, budget_remaining=Decimal("0"),
    )
    result = evaluate_gates(ctx)
    assert result.verdict == "blocked"
    assert "R6_quota" in _reasons(result)


def test_r6_negative_budget_blocks() -> None:
    ctx = GateContext(
        scope="portfolio", live_strategy_count=1, budget_remaining=Decimal("-0.5"),
    )
    assert evaluate_gates(ctx).verdict == "blocked"


# --- R7 on_alert filter + debounce key ----------------------------------------


def test_r7_alert_rule_not_matched_blocks() -> None:
    ctx = GateContext(
        scope="on_alert", live_strategy_count=1, budget_remaining=Decimal("5"),
        alert_rules=["fx_drift"], fired_rule="single_weight", fired_symbol="2330",
        universe_symbols=["2330"],
    )
    result = evaluate_gates(ctx)
    assert result.verdict == "blocked"
    assert "R7_rule_not_matched" in _reasons(result)


def test_r7_alert_rule_matched_passes() -> None:
    ctx = GateContext(
        scope="on_alert", live_strategy_count=1, budget_remaining=Decimal("5"),
        alert_rules=["fx_drift"], fired_rule="fx_drift", fired_symbol="schwab",
    )
    result = evaluate_gates(ctx)
    assert "R7_rule_not_matched" not in _reasons(result)
    assert result.verdict == "clean"


def test_r7_alert_rules_all_matches_any_rule() -> None:
    ctx = GateContext(
        scope="on_alert", live_strategy_count=1, budget_remaining=Decimal("5"),
        alert_rules="all", fired_rule="single_weight", fired_symbol="2330",
    )
    assert "R7_rule_not_matched" not in _reasons(evaluate_gates(ctx))


def test_r7_all_wildcard_excludes_signal_rules() -> None:
    # deep review 2026-07-10 F4: 'all' means "all RISK alerts" — a signal_* transition rule
    # is NOT matched by the wildcard (it would fire an unsubscribed narrative card storm).
    blocked = GateContext(
        scope="on_alert", live_strategy_count=1, budget_remaining=Decimal("5"),
        alert_rules="all", fired_rule="signal_trend", fired_symbol="2330",
    )
    assert "R7_rule_not_matched" in _reasons(evaluate_gates(blocked))


def test_r7_explicit_signal_rule_still_subscribes() -> None:
    # ...but explicitly listing signal_trend DOES subscribe (opt-in).
    listed = GateContext(
        scope="on_alert", live_strategy_count=1, budget_remaining=Decimal("5"),
        alert_rules=["signal_trend"], fired_rule="signal_trend", fired_symbol="2330",
    )
    assert "R7_rule_not_matched" not in _reasons(evaluate_gates(listed))


def test_r7_debounce_key_is_task_rule_symbol() -> None:
    ctx = GateContext(
        scope="on_alert", live_strategy_count=1, budget_remaining=Decimal("5"),
        insight_type_id=7, alert_rules="all", fired_rule="fx_drift",
        fired_symbol="schwab",
    )
    result = evaluate_gates(ctx)
    assert result.debounce_key == "7|fx_drift|schwab"


# --- master_missing (self_correct + no master role) ---------------------------


def test_master_missing_warns_but_does_not_block() -> None:
    ctx = GateContext(
        scope="portfolio", live_strategy_count=1, budget_remaining=Decimal("5"),
        self_correct=True, master_configured=False,
    )
    result = evaluate_gates(ctx)
    assert result.verdict == "clean"  # cards still generate (spec 4.3)
    m = [g for g in result.gates if g.reason == "master_missing"]
    assert m and m[0].lv == "warn"


def test_master_present_no_warning() -> None:
    ctx = GateContext(
        scope="portfolio", live_strategy_count=1, budget_remaining=Decimal("5"),
        self_correct=True, master_configured=True,
    )
    assert "master_missing" not in _reasons(evaluate_gates(ctx))


# --- R8 execution unit (one card per combo / per symbol) ----------------------


def test_r8_portfolio_one_card() -> None:
    ctx = GateContext(
        scope="portfolio", live_strategy_count=1, budget_remaining=Decimal("5"),
    )
    result = evaluate_gates(ctx)
    assert result.target_symbols == [None]  # one card for the whole portfolio


def test_r8_per_symbol_one_card_per_symbol() -> None:
    ctx = GateContext(
        scope="per_symbol", live_strategy_count=1, budget_remaining=Decimal("5"),
        universe_symbols=["2330", "AAPL"],
    )
    result = evaluate_gates(ctx)
    assert result.target_symbols == ["2330", "AAPL"]


# --- precedence: a hard block wins over soft issues ---------------------------


def test_block_precedence_over_degrade() -> None:
    ctx = GateContext(
        scope="per_symbol", live_strategy_count=0, budget_remaining=Decimal("5"),
        universe_symbols=["2330"], unavailable_vars=["x_json"],
    )
    # R3 (no live templates) is a hard block; the R5 soft issue does not soften it.
    assert evaluate_gates(ctx).verdict == "blocked"
