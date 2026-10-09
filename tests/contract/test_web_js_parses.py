"""Every shipped web/*.js must actually parse (2026-08-28).

A one-character escaping slip -- a `\\n` that became a real newline inside a string
literal -- made `data-center.js` unparseable. Nothing in the fast suite noticed. The page
simply rendered empty, because a JS parse error kills the WHOLE file: the unrelated code
above the break stopped running too, so a panel nobody had touched went blank.

It was caught 30 minutes later by an e2e smoke test, and only after a full chunked run.
This test catches the same class in about a second, which is the difference between
"fix it now" and "fix it after the gate you were about to declare green".

Node comes from Playwright's bundled driver -- already a dev dependency, no new package.
If it is absent (a prod-only venv), the test skips rather than failing: this is a
developer guard, not a runtime requirement.
"""

import subprocess
from pathlib import Path

import pytest

_WEB = Path(__file__).resolve().parents[2] / "web"
#: Vendored third-party bundle: pinned by checksum elsewhere, not ours to parse-police.
_SKIP = {"echarts.min.js"}


def _node() -> Path | None:
    import playwright

    node = Path(playwright.__file__).parent / "driver" / "node.exe"
    if node.exists():
        return node
    node = Path(playwright.__file__).parent / "driver" / "node"
    return node if node.exists() else None


def _scripts() -> list[Path]:
    return sorted(p for p in _WEB.glob("*.js") if p.name not in _SKIP)


def test_there_are_scripts_to_check() -> None:
    """Guard the guard: a glob that matches nothing would pass by being blind."""
    assert len(_scripts()) >= 10


@pytest.mark.parametrize("script", _scripts(), ids=lambda p: p.name)
def test_script_parses(script: Path) -> None:
    node = _node()
    if node is None:
        pytest.skip("Playwright's bundled node is not installed in this venv")
    proc = subprocess.run(
        [str(node), "--check", str(script)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, (
        f"{script.name} does not parse — the whole file stops running, including code "
        f"far from the break:\n{proc.stderr}"
    )
