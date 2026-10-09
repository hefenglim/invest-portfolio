"""F-10 counter-evidence: every Alert must name a place to go.

``Alert.href`` defaults to ``None``, and the bell renders every row as an ``<a>`` regardless,
so an href-less alert becomes ``href="#"`` — a link with a pointer cursor that navigates to
the top of the page the reader is already on. Four of the sixteen rules were in that state,
measured 2026-08-27: ``sector_weight``, ``fx_drift``, ``portfolio_drawdown`` and
``currency_weight``.

Those four are precisely the PORTFOLIO-LEVEL risks — the ones with no single symbol to open,
and the ones most worth looking into. The per-symbol rules all pointed at their drawer, so
the alerts that were easiest to route were routed and the ones that needed a decision were
not.

Checked with ``ast`` rather than by constructing sixteen ledger states: the rule being
enforced is "no ``Alert(...)`` is built without an href", which is a property of the call
sites, and a state-based test would need a fixture per rule and would still miss the
seventeenth rule someone adds next year.
"""

import ast
from pathlib import Path

_SRC = (Path(__file__).resolve().parents[2]
        / "portfolio_dash" / "strategy" / "alerts.py")


def _alert_calls() -> list[ast.Call]:
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "Alert"]


def test_the_scan_actually_finds_the_alert_constructions() -> None:
    """A guard that matches nothing passes forever."""
    calls = _alert_calls()
    assert len(calls) >= 14, f"expected the full rule set, found {len(calls)}"


def test_no_alert_is_constructed_without_an_href() -> None:
    offenders = []
    for call in _alert_calls():
        keywords = {kw.arg for kw in call.keywords if kw.arg}
        if "href" not in keywords:
            rule = next((ast.literal_eval(kw.value)
                         for kw in call.keywords
                         if kw.arg == "rule" and isinstance(kw.value, ast.Constant)),
                        f"line {call.lineno}")
            offenders.append((rule, call.lineno))
    assert not offenders, (
        "these alerts render as a link to nowhere (href defaults to None -> the bell "
        f"draws href='#'): {offenders}")
