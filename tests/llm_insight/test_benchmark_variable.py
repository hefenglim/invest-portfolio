"""R4's missing leg: the benchmark counterfactual must be VISIBLE TO THE PROMPT.

R4 shipped the counterfactual — 「同一筆錢、同樣的日期，買指數會是多少？」 — onto the dashboard
and the drawer, and the four-lens review had called it the single highest-value thing in the
programme: without it, "XIRR 15%" has no anchor and every return figure is unmoored.

It shipped without the variable the plan specified (`benchmark_counterfactual_json`, 36 → 37).
So the anchor exists and the narrator cannot see it: no insight card can say the portfolio beat
or lagged the index, which is the one sentence the feature was built to make sayable.

⚠ The degradation is the load-bearing part. `uncovered_ratio > 0` means the headline covers only
PART of the money, and `BenchmarkComparison`'s own docstring forbids printing a bare 「超額報酬」
in that state — the `covered_ratio` (F2) discipline. A prompt variable that hands the model an
`excess` without that context invites exactly the sentence the model must not write.
"""

from portfolio_dash.llm_insight import variables as V


def test_the_variable_is_registered_and_available() -> None:
    spec = V.BY_TOKEN.get("benchmark_counterfactual_json")
    assert spec is not None, "R4's prompt variable was never registered"
    assert spec.available is True
    assert spec.scope == "portfolio"


def test_the_registry_grew_by_exactly_one() -> None:
    """The count the plan named (36 → 37). Pinned so a silent drop is not a silent pass."""
    assert len(V.REGISTRY) == 37
    assert sum(1 for v in V.REGISTRY if v.available) == 37
