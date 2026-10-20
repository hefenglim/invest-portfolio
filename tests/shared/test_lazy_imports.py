"""LiteLLM, yfinance and pandas are imported on first use, never by booting the app.

PERF spec 2026-10-18 §16 / PERF-S13. Measured: importing the app cost 252 MB / 22.8 s, and
58 MB / 7.6 s without LiteLLM and yfinance (pandas comes with yfinance). The interactive heavy
calls (AI door, LLM ping, manual quote refresh, quick-add backfill, manual news fetch) still
import them in the web process, on first use.

* **The walk** (a fresh interpreter — four test modules import ``litellm`` / ``pandas`` at top
  level, so this process always holds them): boot the app through its real lifespan, call
  every ``/api`` route — every GET, and every mutating route with an empty body (a 4xx is
  fine) — with the providers stubbed at the fetch seam, and see no attempt to import any of
  the three. The child refuses each attempt as it records it, so an import in one phase
  cannot hide another's, and proves the guard works by trying one itself at the end.
* **The seams:** ``shared/llm.py`` binds no module-level ``litellm`` and reaches it only
  through its accessor; ``llm.litellm`` (what ≈130 tests ``monkeypatch.setattr`` on) is the
  real module, imported and cached on first access; no module of the package imports one of
  the three at module level.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from portfolio_dash.shared import llm

_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE = _ROOT / "portfolio_dash"
HEAVY = ("litellm", "yfinance", "pandas")


def _child_env() -> dict[str, str]:
    """The child runs THIS checkout's code: cwd and PYTHONPATH = the checkout (never -I)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_ROOT)
    return env


def _run(args: list[str], timeout: int) -> str:
    done = subprocess.run(
        [sys.executable, *args], cwd=_ROOT, env=_child_env(), capture_output=True,
        text=True, encoding="utf-8", timeout=timeout,
    )
    assert done.returncode == 0, f"child failed:\n{done.stdout[-2000:]}\n{done.stderr[-4000:]}"
    return done.stdout.strip().splitlines()[-1]


def test_booting_and_calling_every_route_never_imports_litellm_yfinance_or_pandas(
    tmp_path: Path,
) -> None:
    report = json.loads(_run(["-m", "tests.shared.lazy_import_walk", str(tmp_path)], 900))
    assert Path(report["package"]).is_relative_to(_PACKAGE), report["package"]
    assert report["routes"] >= 200, f"route enumeration broke: {report['routes']}"
    attempts = report["attempts"]
    assert attempts.pop("self-test", None) == ["pandas"], "the import guard recorded nothing"
    assert not attempts, (
        "these phases imported a heavy library (import it inside the function that needs "
        f"it — PERF spec §16): {attempts}")
    assert report["loaded"] == [], report["loaded"]


_FAKE_LITELLM = """
import sys, types
fake = types.ModuleType("litellm")
fake.exceptions = types.ModuleType("litellm.exceptions")
sys.modules["litellm"] = fake
sys.modules["litellm.exceptions"] = fake.exceptions
from portfolio_dash.shared import llm
assert "litellm" not in vars(llm) and "litellm_errors" not in vars(llm), "bound at import"
assert llm.litellm is fake
assert vars(llm)["litellm"] is fake, "not cached into the module globals"
assert llm._litellm() is fake
assert llm.litellm_errors is fake.exceptions and llm._litellm_errors() is fake.exceptions
replacement = types.ModuleType("replacement")
llm.litellm = replacement
assert llm._litellm() is replacement, "an assignment to llm.litellm must reach the calls"
try:
    llm.no_such_attribute
except AttributeError:
    pass
else:
    raise AssertionError("module __getattr__ swallowed an unknown attribute")
print("ok")
"""


def test_llm_binds_litellm_on_first_access_and_caches_it() -> None:
    # A stand-in module in sys.modules keeps the child fast (the real import takes ~15 s).
    assert _run(["-c", _FAKE_LITELLM], 300) == "ok"


def test_monkeypatching_llm_litellm_reaches_the_internal_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake(**_kwargs: object) -> str:
        return "patched"

    monkeypatch.setattr(llm.litellm, "completion", fake)
    assert llm.litellm is sys.modules["litellm"]
    assert llm._litellm().completion is fake
    assert llm.litellm_errors is sys.modules["litellm.exceptions"]


def test_llm_never_names_litellm_bare() -> None:
    """Inside ``shared/llm.py`` a bare ``litellm`` / ``litellm_errors`` is a NameError until
    the first attribute access binds it — every use goes through ``_litellm()`` /
    ``_litellm_errors()``."""
    tree = ast.parse((_PACKAGE / "shared" / "llm.py").read_text(encoding="utf-8"))
    bare = [f"line {n.lineno}" for n in ast.walk(tree)
            if isinstance(n, ast.Name) and n.id in ("litellm", "litellm_errors")
            and isinstance(n.ctx, ast.Load)]
    assert not bare, f"bare LiteLLM names in shared/llm.py: {bare}"


def _module_level_imports(tree: ast.Module) -> list[tuple[int, str]]:
    """Imports executed when the module is imported: the module body and class bodies, and
    any ``if`` / ``try`` / ``with`` among them — never a function body, never a
    ``TYPE_CHECKING`` block."""
    found: list[tuple[int, str]] = []

    def visit(stmts: list[ast.stmt]) -> None:
        for s in stmts:
            if isinstance(s, ast.Import):
                found.extend((s.lineno, a.name) for a in s.names)
            elif isinstance(s, ast.ImportFrom) and s.module and not s.level:
                found.append((s.lineno, s.module))
            elif isinstance(s, ast.If):
                if not (isinstance(s.test, ast.Name) and s.test.id == "TYPE_CHECKING"):
                    visit(s.body)
                visit(s.orelse)
            elif isinstance(s, ast.Try):
                visit(s.body)
                for h in s.handlers:
                    visit(h.body)
                visit(s.orelse)
                visit(s.finalbody)
            elif isinstance(s, ast.With | ast.ClassDef):
                visit(s.body)

    visit(tree.body)
    return found


def test_no_package_module_imports_a_heavy_library_at_module_level() -> None:
    offenders: list[str] = []
    for path in sorted(_PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for lineno, name in _module_level_imports(tree):
            if name.split(".")[0] in HEAVY:
                offenders.append(f"{path.relative_to(_ROOT).as_posix()}:{lineno} {name}")
    assert not offenders, "module-level heavy imports:\n" + "\n".join(offenders)
