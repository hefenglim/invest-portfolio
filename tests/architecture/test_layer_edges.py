"""The two upward edges that are AUTHORISED, and the property that keeps them safe.

`.claude/rules/architecture.md` states a one-way dependency direction. Two edges run
against it and both are deliberate; this file is what makes "deliberate" checkable.

**Why a test and not a note.** D39 settled the shape for the first such edge
(`scheduler → data_ingestion`): narrow the guard to an allowlist, record the edge in the
diagram, **and assert the allowlisted line is actually present** — because "an exception
nobody uses is an exception nobody notices". The second edge, `data_ingestion → portfolio`,
was added by the corporate-actions W2/W4 packages and reached 2026-08-12 with **no diagram
entry and no guard at all**, which is exactly the F-01 class the spec-conflict audit exists
to catch. Found while resolving the cash-movement importer's own layering question — by an
implementer who declined to copy it, which is the only reason it surfaced.

**The part that is easy to miss.** `data_ingestion → portfolio` plus the existing
`portfolio → data_ingestion` is a **package-level cycle**. It is not an *import-time* cycle
today only because `portfolio/cost_basis.py` and `portfolio/results.py` import nothing above
`shared/`. Nothing enforced that. The moment either grows a `data_ingestion` import, the
interpreter raises on a circular import at startup — a boot failure, not a subtle bug. So the
load-bearing assertion here is not the allowlist; it is
:func:`test_the_leaf_modules_that_keep_the_cycle_dormant_stay_leaves`.
"""

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2] / "portfolio_dash"

# data_ingestion may import EXACTLY these, and only in these modules. The four rejections
# E3 / E22 / E5 / E18 and the corporate-action importer read a replayed `Book`; re-deriving
# the replay inside `data_ingestion` would make it a second owner of the ledger replay,
# which is the duplication §6.0 and `shared/ledger_registry.py` both exist to remove.
_ALLOWED: dict[str, frozenset[str]] = {
    "validate.py": frozenset({
        "portfolio_dash.portfolio.cost_basis", "portfolio_dash.portfolio.results",
    }),
    "corporate_action_import.py": frozenset({
        "portfolio_dash.portfolio.cost_basis", "portfolio_dash.portfolio.results",
    }),
}

# The two modules the allowlist reaches into. They keep the cycle dormant by importing
# nothing above `shared/` — see the module docstring.
_LEAVES = ("portfolio/cost_basis.py", "portfolio/results.py")


def _imported_modules(path: Path) -> set[str]:
    """Every `portfolio_dash.*` module name this file imports, from the AST.

    AST, not a text grep: a grep counts the module's own docstring, which in this codebase
    quotes import lines while explaining them.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("portfolio_dash."):
                found.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("portfolio_dash."):
                    found.add(alias.name)
    return found


def _upward_imports() -> dict[str, set[str]]:
    """`data_ingestion` module -> the `portfolio` modules it imports."""
    out: dict[str, set[str]] = {}
    for path in sorted((_ROOT / "data_ingestion").glob("*.py")):
        hits = {m for m in _imported_modules(path)
                if m.startswith("portfolio_dash.portfolio")}
        if hits:
            out[path.name] = hits
    return out


def test_data_ingestion_imports_portfolio_only_where_authorised() -> None:
    """The allowlist half: no NEW upward import may appear without this file changing."""
    actual = _upward_imports()
    unauthorised = sorted(set(actual) - set(_ALLOWED))
    assert set(actual) <= set(_ALLOWED), (
        f"unauthorised data_ingestion -> portfolio import in {unauthorised}. "
        "Prefer INJECTION (architecture.md — `cash_pool_fn`, `split_factor_fn`); if the edge is "
        "genuinely right, authorise it here AND in the diagram, never in code alone."
    )
    for module, imports in actual.items():
        extra = imports - _ALLOWED[module]
        assert not extra, f"{module} imports un-allowlisted {sorted(extra)}"


def test_the_authorised_imports_are_actually_present() -> None:
    """D39's second half: an exception nobody uses is an exception nobody notices.

    If the replay import is ever removed — say the rejections move behind an injected
    callable, which would be an improvement — this fails and the allowlist gets deleted
    with it, instead of outliving the code and licensing a future edge nobody argued for.
    """
    actual = _upward_imports()
    assert set(actual) == set(_ALLOWED), (
        "an allowlisted module no longer imports portfolio at all — delete its entry "
        f"(stale: {sorted(set(_ALLOWED) - set(actual))})"
    )


@pytest.mark.parametrize("leaf", _LEAVES)
def test_the_leaf_modules_that_keep_the_cycle_dormant_stay_leaves(leaf: str) -> None:
    """**The load-bearing one.** `data_ingestion -> portfolio` and the existing
    `portfolio -> data_ingestion` (`dashboard.py`, `dividends.py`) form a package-level
    cycle. It is not an import-time cycle only because these two modules import nothing but
    `shared/` and each other, so the interpreter never has to resolve the loop.

    Give either of them a `data_ingestion` import and the app fails to boot on a circular
    import. That is the whole safety margin, and until this test it was unwritten.
    """
    imports = _imported_modules(_ROOT / leaf)
    illegal = {m for m in imports
               if not m.startswith(("portfolio_dash.shared", "portfolio_dash.portfolio"))}
    assert not illegal, (
        f"{leaf} may import ONLY `shared` (and its `portfolio` siblings) — it is the leaf that "
        f"keeps the data_ingestion <-> portfolio cycle dormant. Found: {sorted(illegal)}"
    )


def test_the_guard_can_actually_fail() -> None:
    """Detection power. A layering guard nobody has watched go red is a comment.

    Parses a synthetic module rather than mutating the tree, so the assertion under test is
    the extractor itself — the part that would silently pass if it stopped seeing imports.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        bad = Path(tmp) / "sneaky.py"
        bad.write_text(
            '"""A docstring that mentions portfolio_dash.portfolio.twr to bait a grep."""\n'
            "from portfolio_dash.portfolio.twr import twr_index\n",
            encoding="utf-8",
        )
        found = _imported_modules(bad)
    assert found == {"portfolio_dash.portfolio.twr"}, (
        "the extractor missed a real import, or counted the docstring — either way every "
        "assertion in this file would pass vacuously"
    )
