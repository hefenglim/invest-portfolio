"""Unit tests for the PURE node-state derivation (spec 07 §7.1.1).

``derive_node_states`` is a pure function over a fed :class:`PipelineFacts` bundle — it
reads no connection and imports neither pricing nor api (architecture.md). It mirrors the
runtime gate's notion of "would this run / would it degrade" for the five pipeline nodes
(trigger / input / assemble / exec / output) without re-deriving the gate. The aggregate
``level`` is the max severity; a disabled task is wholly ``idle``.
"""

from decimal import Decimal

from portfolio_dash.llm_insight.pipeline_status import (
    PipelineFacts,
    derive_node_states,
)


def _facts(**over: object) -> PipelineFacts:
    """A baseline all-green per_symbol task; override individual facts per test."""
    base: dict[str, object] = {
        "enabled": True,
        "scope": "per_symbol",
        "scheduled": True,
        "universe_symbols": ["2330", "AAPL"],
        "removed_recently": [],
        "missing_price_symbols": [],
        "stale_price_symbols": [],
        "live_template_count": 2,
        "total_template_count": 2,
        "r1_mismatch": False,
        "unapplied_calibration": False,
        "self_correct": False,
        "master_configured": True,
        "quota_remaining": Decimal("5"),
        "quota_low": Decimal("1"),
        "last_run_status": "ok",
    }
    base.update(over)
    return PipelineFacts(**base)  # type: ignore[arg-type]


# --- aggregate / disabled ------------------------------------------------------


def test_all_green_is_ok() -> None:
    out = derive_node_states(_facts())
    assert out.level == "ok"
    assert {k: v.lv for k, v in out.nodes.items()} == {
        "trigger": "ok", "input": "ok", "assemble": "ok", "exec": "ok", "output": "ok",
    }


def test_disabled_task_is_idle_everywhere() -> None:
    out = derive_node_states(_facts(enabled=False))
    assert out.level == "idle"
    assert all(node.lv == "idle" for node in out.nodes.values())


def test_level_is_max_severity() -> None:
    # universe empty (input fail) dominates a manual-trigger warn.
    out = derive_node_states(_facts(scheduled=False, universe_symbols=[]))
    assert out.level == "fail"


# --- trigger -------------------------------------------------------------------


def test_trigger_manual_unscheduled_warns() -> None:
    out = derive_node_states(_facts(scheduled=False))
    assert out.nodes["trigger"].lv == "warn"


# --- input ---------------------------------------------------------------------


def test_input_empty_universe_fails() -> None:
    out = derive_node_states(_facts(universe_symbols=[]))
    assert out.nodes["input"].lv == "fail"


def test_input_missing_price_warns() -> None:
    # M8: MISSING keeps the warn — it is what the shared gate's R4 fires on (the
    # deterministic zero-LLM anomaly card). Split from the old combined
    # `test_input_missing_or_stale_price_warns`, which pinned one level for both states.
    out = derive_node_states(_facts(missing_price_symbols=["AAPL"]))
    assert out.nodes["input"].lv == "warn"
    assert out.nodes["input"].sub is not None
    assert "AAPL 缺價" in out.nodes["input"].sub


def test_input_stale_only_price_is_info() -> None:
    # M8: a STALE price still produces a real card off an older close — the dry run's R4
    # passes it, so the card must not shout warn at the same fact.
    out = derive_node_states(_facts(stale_price_symbols=["AAPL"]))
    assert out.nodes["input"].lv == "info"
    assert out.nodes["input"].sub == "價格過期（以舊價產生）：AAPL"


def test_input_missing_outranks_stale_but_names_both() -> None:
    out = derive_node_states(
        _facts(missing_price_symbols=["AAPL"], stale_price_symbols=["2330"])
    )
    node = out.nodes["input"]
    assert node.lv == "warn"
    assert node.sub is not None and "AAPL 缺價" in node.sub and "2330 價格過期" in node.sub


def test_input_recent_removal_is_info() -> None:
    out = derive_node_states(_facts(removed_recently=["1155.KL"]))
    assert out.nodes["input"].lv == "info"


def test_input_empty_universe_outranks_removal_info() -> None:
    out = derive_node_states(_facts(universe_symbols=[], removed_recently=["X"]))
    assert out.nodes["input"].lv == "fail"


def test_portfolio_scope_input_ignores_universe() -> None:
    # portfolio scope has no universe lifecycle → empty list is not a fail.
    out = derive_node_states(_facts(scope="portfolio", universe_symbols=[]))
    assert out.nodes["input"].lv == "ok"
    assert out.nodes["input"].text == "全持倉"


# --- M2: the head text is decided by SCOPE, before the branches --------------------
# Measured 2026-09-16: a portfolio task printed 「0 檔標的」 and a per_market task 「3 檔標的」
# (its 3 markets) while both listed 14 symbols underneath — the scope-aware label existed
# only on the ok branch, so every warning task fell back to len(universe_symbols).


def test_portfolio_scope_head_is_all_holdings_on_every_branch() -> None:
    for over in (
        {"missing_price_symbols": ["AAPL"]},
        {"stale_price_symbols": ["AAPL"]},
        {"removed_recently": ["AAPL"]},
    ):
        out = derive_node_states(_facts(scope="portfolio", universe_symbols=[], **over))
        assert out.nodes["input"].text == "全持倉", over


def test_on_alert_scope_head_is_all_holdings() -> None:
    out = derive_node_states(
        _facts(scope="on_alert", universe_symbols=[], missing_price_symbols=["AAPL"])
    )
    assert out.nodes["input"].text == "全持倉"


def test_per_market_scope_head_counts_markets() -> None:
    facts = _facts(scope="per_market", universe_symbols=["TW", "US", "MY"])
    assert derive_node_states(facts).nodes["input"].text == "3 個市場"
    warned = _facts(
        scope="per_market", universe_symbols=["TW", "US", "MY"],
        missing_price_symbols=["AAPL"],
    )
    assert derive_node_states(warned).nodes["input"].text == "3 個市場"


def test_per_symbol_scope_head_counts_symbols_on_every_branch() -> None:
    warned = _facts(missing_price_symbols=["AAPL"])
    assert derive_node_states(warned).nodes["input"].text == "2 檔標的"
    stale = _facts(stale_price_symbols=["AAPL"])
    assert derive_node_states(stale).nodes["input"].text == "2 檔標的"


# --- assemble ------------------------------------------------------------------


def test_assemble_all_templates_off_fails() -> None:
    out = derive_node_states(_facts(live_template_count=0, total_template_count=2))
    assert out.nodes["assemble"].lv == "fail"


def test_assemble_some_templates_off_warns() -> None:
    out = derive_node_states(_facts(live_template_count=1, total_template_count=2))
    assert out.nodes["assemble"].lv == "warn"


def test_assemble_r1_mismatch_warns() -> None:
    out = derive_node_states(_facts(r1_mismatch=True))
    assert out.nodes["assemble"].lv == "warn"


def test_assemble_unapplied_calibration_is_info() -> None:
    out = derive_node_states(_facts(unapplied_calibration=True))
    assert out.nodes["assemble"].lv == "info"


# --- exec ----------------------------------------------------------------------


def test_exec_quota_zero_fails() -> None:
    out = derive_node_states(_facts(quota_remaining=Decimal("0")))
    assert out.nodes["exec"].lv == "fail"


def test_exec_quota_below_threshold_warns() -> None:
    out = derive_node_states(_facts(quota_remaining=Decimal("0.5"), quota_low=Decimal("1")))
    assert out.nodes["exec"].lv == "warn"


def test_exec_master_unset_with_self_correct_warns() -> None:
    out = derive_node_states(_facts(self_correct=True, master_configured=False))
    assert out.nodes["exec"].lv == "warn"


def test_exec_master_unset_without_self_correct_is_ok() -> None:
    out = derive_node_states(_facts(self_correct=False, master_configured=False))
    assert out.nodes["exec"].lv == "ok"


# --- output --------------------------------------------------------------------


def test_output_never_run_is_idle() -> None:
    out = derive_node_states(_facts(last_run_status=None))
    assert out.nodes["output"].lv == "idle"


def test_output_skipped_fails() -> None:
    out = derive_node_states(_facts(last_run_status="skipped"))
    assert out.nodes["output"].lv == "fail"


def test_output_error_fails() -> None:
    out = derive_node_states(_facts(last_run_status="error"))
    assert out.nodes["output"].lv == "fail"


def test_output_partial_warns() -> None:
    out = derive_node_states(_facts(last_run_status="partial"))
    assert out.nodes["output"].lv == "warn"
