"""The three upward edges that are AUTHORISED, and the property that keeps them safe.

`.claude/rules/architecture.md` states a one-way dependency direction. Three edges run
against it and all are deliberate; this file is what makes "deliberate" checkable.

**Why a test and not a note.** D39 settled the shape for the first such edge
(`scheduler → data_ingestion`): narrow the guard to an allowlist, record the edge in the
diagram, **and assert the allowlisted line is actually present** — because "an exception
nobody uses is an exception nobody notices". The second edge, `data_ingestion → portfolio`,
was added by the corporate-actions W2/W4 packages and reached 2026-08-12 with **no diagram
entry and no guard at all**, which is exactly the F-01 class the spec-conflict audit exists
to catch. Found while resolving the cash-movement importer's own layering question — by an
implementer who declined to copy it, which is the only reason it surfaced.

The third, `data_ingestion → llm_insight`, is older than the second (FU-D20, 2026-07-17 —
every shipped prompt has one home, so the AI door reads its code-owned prompt from
`llm_insight/official_templates.py`) and went just as unrecorded until the 2026-09-09 site
architecture map listed it as its first finding (D-01). Same shape, same fix.

**The part that is easy to miss.** Each of those two edges closes a **package-level cycle**:
`data_ingestion → portfolio` against the existing `portfolio → data_ingestion`, and
`data_ingestion → llm_insight` against `llm_insight → portfolio → data_ingestion`. Neither is
an *import-time* cycle today only because the modules the allowlists reach into —
`portfolio/cost_basis.py`, `portfolio/results.py`, `llm_insight/official_templates.py` and
`llm_insight/__init__.py` — import nothing above `shared/`. Nothing enforced that. The moment
one of them grows the wrong import, the interpreter raises on a circular import at startup —
a boot failure, not a subtle bug. So the load-bearing assertion here is not the allowlist; it
is :func:`test_the_leaf_modules_that_keep_the_cycles_dormant_stay_leaves`.
"""

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2] / "portfolio_dash"

# data_ingestion may import EXACTLY these portfolio modules, and only in these modules. The
# four rejections E3 / E22 / E5 / E18 and the corporate-action importer read a replayed
# `Book`; re-deriving the replay inside `data_ingestion` would make it a second owner of the
# ledger replay, which is the duplication §6.0 and `shared/ledger_registry.py` both exist to
# remove.
_ALLOWED_PORTFOLIO: dict[str, frozenset[str]] = {
    "validate.py": frozenset({
        "portfolio_dash.portfolio.cost_basis", "portfolio_dash.portfolio.results",
    }),
    "corporate_action_import.py": frozenset({
        "portfolio_dash.portfolio.cost_basis", "portfolio_dash.portfolio.results",
    }),
}

# data_ingestion may import EXACTLY this from llm_insight: the code-owned AI-input prompt
# body. FU-D20 gave every shipped prompt ONE home in `official_templates.py`, and FU-D30's
# registry test (`tests/llm_insight/test_prompt_registry.py`) traces every LLM call site back
# to that module — `agents.py` is a call site, so it reads its prompt from there instead of
# owning a second copy. Rejected: injecting the string from the AI-door router (a prompt body
# is a constant, not a computation with an ownership problem, and 33 call sites would carry a
# string for no structural gain); moving the constant to `shared/` (breaks the one-home rule
# the registry test enforces); duplicating the text (FU-D20 existed to end that drift).
_ALLOWED_LLM: dict[str, frozenset[str]] = {
    "agents.py": frozenset({"portfolio_dash.llm_insight.official_templates"}),
}

# edge label -> (the target package prefix, the allowlist for that edge)
_EDGES: dict[str, tuple[str, dict[str, frozenset[str]]]] = {
    "data_ingestion -> portfolio": ("portfolio_dash.portfolio", _ALLOWED_PORTFOLIO),
    "data_ingestion -> llm_insight": ("portfolio_dash.llm_insight", _ALLOWED_LLM),
}

# The modules the allowlists reach into, and the ONLY prefixes each may import. They keep
# the cycles dormant — see the module docstring. The portfolio leaves may import their own
# siblings (those are leaves too, or become one by this test); the llm_insight leaves may
# not even do that, because an `llm_insight` sibling reaches `portfolio` and the loop closes.
_LEAVES: dict[str, tuple[str, ...]] = {
    "portfolio/cost_basis.py": ("portfolio_dash.shared", "portfolio_dash.portfolio"),
    "portfolio/results.py": ("portfolio_dash.shared", "portfolio_dash.portfolio"),
    "llm_insight/official_templates.py": ("portfolio_dash.shared",),
    "llm_insight/__init__.py": ("portfolio_dash.shared",),
}


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


def _upward_imports(target: str, *, root: Path = _ROOT) -> dict[str, set[str]]:
    """`data_ingestion` module -> the modules under *target* it imports."""
    out: dict[str, set[str]] = {}
    for path in sorted((root / "data_ingestion").glob("*.py")):
        hits = {m for m in _imported_modules(path) if m.startswith(target)}
        if hits:
            out[path.name] = hits
    return out


@pytest.mark.parametrize("edge", sorted(_EDGES))
def test_data_ingestion_upward_imports_only_where_authorised(edge: str) -> None:
    """The allowlist half: no NEW upward import may appear without this file changing."""
    target, allowed = _EDGES[edge]
    actual = _upward_imports(target)
    unauthorised = sorted(set(actual) - set(allowed))
    assert set(actual) <= set(allowed), (
        f"unauthorised {edge} import in {unauthorised}. "
        "Prefer INJECTION (architecture.md — `cash_pool_fn`, `split_factor_fn`); if the edge is "
        "genuinely right, authorise it here AND in the diagram, never in code alone."
    )
    for module, imports in actual.items():
        extra = imports - allowed[module]
        assert not extra, f"{edge}: {module} imports un-allowlisted {sorted(extra)}"


@pytest.mark.parametrize("edge", sorted(_EDGES))
def test_the_authorised_imports_are_actually_present(edge: str) -> None:
    """D39's second half: an exception nobody uses is an exception nobody notices.

    If an allowlisted import is ever removed — say the rejections move behind an injected
    callable, which would be an improvement — this fails and the allowlist gets deleted
    with it, instead of outliving the code and licensing a future edge nobody argued for.
    """
    target, allowed = _EDGES[edge]
    actual = _upward_imports(target)
    assert set(actual) == set(allowed), (
        f"{edge}: an allowlisted module no longer imports the target at all — delete its "
        f"entry (stale: {sorted(set(allowed) - set(actual))})"
    )


@pytest.mark.parametrize("leaf", sorted(_LEAVES))
def test_the_leaf_modules_that_keep_the_cycles_dormant_stay_leaves(leaf: str) -> None:
    """**The load-bearing one.** Each authorised upward edge, together with the downward
    edges that already exist (`portfolio/dashboard.py` and `portfolio/dividends.py` import
    `data_ingestion`; `llm_insight` imports `portfolio`), forms a package-level cycle. It is
    not an import-time cycle only because these modules import nothing but what is listed
    for them, so the interpreter never has to resolve the loop.

    Give one of them the wrong import and the app fails to boot on a circular import. That
    is the whole safety margin, and until this test it was unwritten.
    """
    allowed = _LEAVES[leaf]
    imports = _imported_modules(_ROOT / leaf)
    illegal = {m for m in imports if not m.startswith(allowed)}
    assert not illegal, (
        f"{leaf} may import ONLY {allowed} — it is a leaf that keeps a package-level cycle "
        f"dormant. Found: {sorted(illegal)}"
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


def test_the_allowlist_can_actually_fail() -> None:
    """Detection power for the second half: a synthetic `data_ingestion/` tree with one
    module reaching into `llm_insight` past the allowlist must be reported by name.

    Built in a temp dir, so the real tree is never mutated; the walk under test is the same
    `_upward_imports` the authorised-edge tests run against `portfolio_dash/`.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        pkg = Path(tmp) / "data_ingestion"
        pkg.mkdir()
        (pkg / "sneaky.py").write_text(
            "from portfolio_dash.llm_insight.generate import run\n", encoding="utf-8"
        )
        (pkg / "agents.py").write_text(
            "from portfolio_dash.llm_insight.official_templates import AI_INPUT_PROMPT_BODY\n",
            encoding="utf-8",
        )
        actual = _upward_imports("portfolio_dash.llm_insight", root=Path(tmp))
    assert actual == {
        "sneaky.py": {"portfolio_dash.llm_insight.generate"},
        "agents.py": {"portfolio_dash.llm_insight.official_templates"},
    }
    assert set(actual) - set(_ALLOWED_LLM) == {"sneaky.py"}, (
        "the walk saw the import but the allowlist comparison would not have flagged it"
    )
