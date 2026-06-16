"""Spec-19 §6 guardrail: the static frontend has exactly ONE fetch seam.

These are deterministic SOURCE-SCAN tests (no DB, no network, no fixtures): they read
the files under ``web/`` and assert structural invariants of the front/back contract.

Invariants guarded here:

1. **pdApi is the single fetch layer.** No ``web/*.js`` file EXCEPT ``web/api.js`` may
   contain a raw ``fetch(`` call — every ``/api/*`` request routes through
   ``window.pdApi`` (decision B, spec 19.1). A new page calling ``fetch`` directly would
   bypass the money-passthrough guarantee + structured-error handling and is a bug.
2. **The retired mock files are gone.** ``mock-data.js`` / ``history-mock.js`` /
   ``input-mock-data.js`` / ``pipeline-data.js`` were deleted once every live consumer
   was wired to ``/api/*`` (Tasks 2.x + 3.1). They must not reappear, and no ``<script>``
   tag in any page may reference them.

The scan strips JS comments before looking for ``fetch(`` so prose mentions of the word
"fetch" in docstrings/comments are not false positives.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# tests/contract/this_file.py -> parents[2] == worktree root (web/ lives here).
_WORKTREE_ROOT = Path(__file__).resolve().parents[2]
_WEB_DIR = _WORKTREE_ROOT / "web"

# The single sanctioned fetch layer; every other page JS must go through window.pdApi.
_FETCH_LAYER = "api.js"

# Retired mock data files (Tasks 2.x + 3.1). They must stay deleted.
_RETIRED_MOCKS = (
    "mock-data.js",
    "history-mock.js",
    "input-mock-data.js",
    "pipeline-data.js",
)

# A `fetch(` call: word-boundary `fetch` then optional whitespace then `(`.
_FETCH_CALL = re.compile(r"\bfetch\s*\(")

# JS comments: `/* ... */` (incl. multi-line) and `// ... <eol>`. Stripped before scanning
# so a comment like `/* re-rendered on each fetch (symbol change) */` is not a match.
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"//[^\n]*")


def _strip_js_comments(src: str) -> str:
    """Remove block and line comments. Good enough for this guardrail: a `fetch(` inside
    a string literal is not something this codebase does, and comments are the only real
    source of false positives we have observed."""
    src = _BLOCK_COMMENT.sub("", src)
    src = _LINE_COMMENT.sub("", src)
    return src


def _web_js_files() -> list[Path]:
    return sorted(_WEB_DIR.glob("*.js"))


def test_web_dir_exists_and_has_js() -> None:
    """Sanity: the scan targets a real, non-empty web/ so the asserts below are meaningful."""
    assert _WEB_DIR.is_dir(), f"web/ not found at {_WEB_DIR}"
    js = _web_js_files()
    assert js, "expected web/*.js files to scan"
    assert (_WEB_DIR / _FETCH_LAYER).is_file(), "web/api.js (the fetch layer) is missing"


def test_only_api_js_calls_fetch() -> None:
    """No page JS calls raw fetch(); all network goes through window.pdApi (spec 19 §6)."""
    offenders = []
    for path in _web_js_files():
        if path.name == _FETCH_LAYER:
            continue
        body = _strip_js_comments(path.read_text(encoding="utf-8"))
        if _FETCH_CALL.search(body):
            offenders.append(path.name)
    assert not offenders, (
        "raw fetch( found outside web/api.js — route all /api/* through window.pdApi: "
        + ", ".join(offenders)
    )


def test_api_js_is_the_fetch_layer() -> None:
    """Positive control: api.js itself DOES call fetch (proves the scan can detect it)."""
    body = _strip_js_comments((_WEB_DIR / _FETCH_LAYER).read_text(encoding="utf-8"))
    assert _FETCH_CALL.search(body), "web/api.js should contain the fetch( calls"


@pytest.mark.parametrize("mock_name", _RETIRED_MOCKS)
def test_retired_mock_file_deleted(mock_name: str) -> None:
    """The retired mock data files stay deleted (the frontend reads /api/* now)."""
    assert not (_WEB_DIR / mock_name).exists(), (
        f"web/{mock_name} should be deleted — it is a retired mock; the frontend reads /api/*"
    )


def test_no_html_script_references_retired_mocks() -> None:
    """No <script src=...> in any page references a retired mock file."""
    offenders = []
    for html in sorted(_WEB_DIR.glob("*.html")):
        text = html.read_text(encoding="utf-8")
        for mock_name in _RETIRED_MOCKS:
            if re.search(r"src=[\"'][^\"']*" + re.escape(mock_name), text):
                offenders.append(f"{html.name} -> {mock_name}")
    assert not offenders, "retired mock files referenced by <script>: " + ", ".join(offenders)
