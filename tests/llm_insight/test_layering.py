"""Architecture-boundary tests for the llm_insight layer (spec 04 fix #2).

architecture.md: lower layers never import the web layer. llm_insight/ imported
``to_wire`` from ``portfolio_dash.api.serialize`` — a reverse (lower→web) import. The
wire encoder now lives in ``shared/wire.py``; ``api.serialize`` re-exports it so existing
``api.serialize.to_wire`` callers keep working. These tests lock both halves.
"""

import ast
from pathlib import Path

import portfolio_dash.llm_insight as llm_insight_pkg


def _llm_insight_sources() -> list[Path]:
    pkg_dir = Path(llm_insight_pkg.__file__).parent
    return sorted(pkg_dir.rglob("*.py"))


def test_no_api_import_under_llm_insight() -> None:
    """No source file under llm_insight/ may import from portfolio_dash.api (architecture.md)."""
    offenders: list[str] = []
    for path in _llm_insight_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                if node.module == "portfolio_dash.api" or node.module.startswith(
                    "portfolio_dash.api."
                ):
                    offenders.append(f"{path.name}: from {node.module} import ...")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "portfolio_dash.api" or alias.name.startswith(
                        "portfolio_dash.api."
                    ):
                        offenders.append(f"{path.name}: import {alias.name}")
    assert offenders == [], f"llm_insight imports the web layer: {offenders}"


def test_to_wire_importable_from_shared() -> None:
    from portfolio_dash.shared.wire import to_wire

    assert to_wire({"a": "b"}) == {"a": "b"}


def test_api_serialize_to_wire_still_importable() -> None:
    # The re-export keeps every existing api.serialize.to_wire caller working.
    from portfolio_dash.api.serialize import to_wire as api_to_wire
    from portfolio_dash.shared.wire import to_wire as shared_to_wire

    assert api_to_wire is shared_to_wire
