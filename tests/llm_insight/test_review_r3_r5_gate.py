"""R3/⑭ counter-evidence: the R5 degradation gate could never fire.

``RunInputs.unavailable_vars`` → ``GateContext.unavailable_vars`` → R5 was wired end to end and
the field was never POPULATED: ``insight_service`` builds ``RunInputs`` at four sites and sets
it at none of them. So a run whose every external variable came back
``{"unavailable": true}`` was recorded as clean, and the one signal that says "this batch was
synthesised from missing inputs" never reached ``job_runs``.

The producers already emit the shape the gate needs — ``variables.py`` documents
``{"unavailable": true}`` as the contract — so the fix is a read, not a new mechanism.
"""

from typing import Any

from portfolio_dash.llm_insight.gating import GateContext, evaluate_gates
from portfolio_dash.llm_insight.variables import unavailable_tokens


def _ctx(**kw: Any) -> GateContext:
    base: dict[str, Any] = {
        "scope": "per_symbol",
        "live_strategy_count": 1,
        "budget_remaining": __import__("decimal").Decimal("10"),
        "master_configured": True,
    }
    base.update(kw)
    return GateContext(**base)


# --- the read that was missing ------------------------------------------------------------

def test_unavailable_tokens_reads_the_documented_shape() -> None:
    ext: dict[str, Any] = {
        "news_json": {"unavailable": True},
        "consensus_json": {"symbol": "AAPL", "unavailable": True},
        "fundamentals_json": {"yfinance": {"pe": "20"}},
        "signal_backtest_json": {"unavailable": False},
    }
    assert unavailable_tokens(ext) == ["consensus_json", "news_json"]


def test_unavailable_tokens_is_sorted_so_the_gate_message_is_deterministic() -> None:
    """R5's message joins these into user-visible text; set order would churn job_runs."""
    ext: dict[str, Any] = {t: {"unavailable": True} for t in ("z_json", "a_json", "m_json")}
    assert unavailable_tokens(ext) == ["a_json", "m_json", "z_json"]


def test_a_non_dict_value_is_never_mistaken_for_unavailable() -> None:
    """Producers may return lists or scalars; only the documented dict shape counts."""
    ext: dict[str, Any] = {"a": [], "b": None, "c": "unavailable", "d": 0}
    assert unavailable_tokens(ext) == []


# --- the gate that the read feeds ---------------------------------------------------------

def test_a_run_with_every_external_variable_missing_is_recorded_as_DEGRADED() -> None:
    """The whole point: 'clean' must mean the inputs were there."""
    ext: dict[str, Any] = {t: {"unavailable": True}
                           for t in ("news_json", "consensus_json", "fundamentals_json")}
    result = evaluate_gates(_ctx(unavailable_vars=unavailable_tokens(ext)))
    r5 = [g for g in result.gates if g.id == "R5"]
    assert r5 and r5[0].reason == "R5_var_unavailable"
    assert "news_json" in r5[0].msg


def test_a_run_with_all_inputs_present_stays_clean() -> None:
    """The gate must stay silent on a healthy run — an alarm that always fires is no alarm."""
    ext: dict[str, Any] = {"news_json": {"items": []}}
    result = evaluate_gates(_ctx(unavailable_vars=unavailable_tokens(ext)))
    assert not [g for g in result.gates if g.id == "R5"]
