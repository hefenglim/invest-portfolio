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
        "missing_or_stale_symbols": [],
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


def test_input_missing_or_stale_price_warns() -> None:
    out = derive_node_states(_facts(missing_or_stale_symbols=["AAPL"]))
    assert out.nodes["input"].lv == "warn"


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
