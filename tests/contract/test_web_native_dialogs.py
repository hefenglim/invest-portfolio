"""F-15 guard: confirmations go through the app's dialog, not the browser's.

``window.confirm`` fits one line of text. ``confirmDialog`` carries a title, a body, a danger
style and a named action button — and every destructive confirmation in this app used it
except two, both on the fee-rule cards, both changing what every FUTURE trade will cost.

The gap was not cosmetic. The other delete confirmations say how many rows go and whether it
can be undone; the native one could only ask the question. The one fact the owner needs
before deciding — that historical rows keep their own fee snapshot and are not recomputed —
was being delivered afterwards, in the success toast.

``alert`` and ``prompt`` are included because they are the same mistake with the same excuse.
"""

import re
from pathlib import Path

_WEB = Path(__file__).resolve().parents[2] / "web"
_SKIP = {"echarts.min.js"}

# The leading class excludes `.` (a method on some other object), a word character
# (…confirm as a suffix) and `-` (`#m-confirm (`, which appears in prose).
_NATIVE = re.compile(r"(?<![.\w-])(?:window\.)?(?:confirm|alert|prompt)\s*\(")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"(?<!:)//[^\n]*")


def _sources() -> list[Path]:
    return [p for p in sorted([*_WEB.glob("*.js"), *_WEB.glob("*.html")])
            if p.name not in _SKIP]


def _blank(match: "re.Match[str]") -> str:
    """Replace a comment with spaces, keeping its newlines so line numbers stay true."""
    return re.sub(r"[^\n]", " ", match.group(0))


def _code_only(text: str) -> str:
    """Comments blanked out before scanning.

    This frontend is dense with prose that mentions these very words — 「a small prompt (…」,
    「the system prompt (GET …」, 「#m-confirm (that flash…」 — and a guard that flags a comment
    is a guard its next reader switches off.
    """
    return _LINE_COMMENT.sub(_blank, _BLOCK_COMMENT.sub(_blank, text))


def test_no_native_modal_dialogs_in_the_frontend() -> None:
    offenders: list[str] = []
    for path in _sources():
        code = _code_only(path.read_text(encoding="utf-8"))
        for lineno, line in enumerate(code.splitlines(), 1):
            if _NATIVE.search(line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()[:90]}")
    assert not offenders, (
        "native browser dialogs — use window.confirmDialog / window.toast instead: "
        + "; ".join(offenders))


def test_the_scan_can_see_the_replacement_it_asks_for() -> None:
    """A guard that matches nothing passes forever — pin both halves against real files."""
    joined = "\n".join(p.read_text(encoding="utf-8") for p in _sources())
    assert "window.confirmDialog = function" in joined
    assert joined.count("confirmDialog({") > 5


def test_the_scan_still_sees_a_native_call_when_there_is_one() -> None:
    """Proof the comment-blanking did not disarm the pattern it exists to serve."""
    sample = "/* a small prompt (not code) */\nif (!window.confirm('x')) return;\n"
    lines = [i for i, ln in enumerate(_code_only(sample).splitlines(), 1)
             if _NATIVE.search(ln)]
    assert lines == [2], lines
