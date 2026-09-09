"""The injection convention's obligation (1), checked: a required seam stays required, and
every production caller binds it.

`architecture.md` — "the injected parameter has **no default**": D39 rejected injection from
`api/app.py` because a *missed* registration degrades silently, and a required argument is
the difference — forgetting it is a mypy error and a `TypeError`. The pricing entry points
`refresh_quotes` / `refresh_history` defaulted `factor_of` to the identity until 2026-09-10
(site-architecture map D-05). The default was "safe" only for a symbol with no split in
`(as_of, fetched_at]`; for a held symbol that had split, a caller that forgot the binding
would store the provider's re-stated history as if it were as-traded, and the read path
would divide it again. Nothing caught that except every caller remembering — and two did
not (the benchmark sweeps, D-11).

Two assertions: the signatures carry no default (so a new caller cannot forget), and every
call inside `portfolio_dash/` passes `factor_of=` explicitly (so an omitting call cannot
come back). `upsert_prices` keeps its default on purpose — it is `pricing`-internal, and its
docstring says why.
"""

import ast
import inspect
from pathlib import Path

import pytest

from portfolio_dash.pricing import refresh

_ROOT = Path(__file__).resolve().parents[2] / "portfolio_dash"
_SEAMS = ("refresh_quotes", "refresh_history")


@pytest.mark.parametrize("name", _SEAMS)
def test_the_seam_parameter_has_no_default(name: str) -> None:
    param = inspect.signature(getattr(refresh, name)).parameters["factor_of"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty, (
        f"{name}(factor_of=...) grew a default — architecture.md injection obligation (1)"
    )


def _calls_missing_factor(root: Path) -> list[str]:
    """`file:line` of every `refresh_quotes(...)` / `refresh_history(...)` call under *root*
    that does not pass `factor_of=` — by AST, so a docstring quoting the call does not count."""
    out: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            else:
                continue
            if name in _SEAMS and not any(k.arg == "factor_of" for k in node.keywords):
                out.append(f"{path.relative_to(root).as_posix()}:{node.lineno}")
    return out


def test_every_production_caller_binds_the_factor() -> None:
    assert _calls_missing_factor(_ROOT) == [], (
        "a pricing entry point is called without factor_of= — bind split_factor_fn(conn) "
        "(scheduler / api) or spell the identity out with _no_factor; never rely on a default"
    )


def test_the_walk_can_actually_fail(tmp_path: Path) -> None:
    """Detection power: a docstring quoting a bound call must not mask an unbound one."""
    (tmp_path / "sneaky.py").write_text(
        '"""refresh_history(conn, reg, refs, start, now=now, factor_of=f) — in a docstring."""\n'
        "def job(conn, reg, refs, start, now):\n"
        "    return refresh_history(conn, reg, refs, start, now=now)\n",
        encoding="utf-8",
    )
    assert _calls_missing_factor(tmp_path) == ["sneaky.py:3"]
