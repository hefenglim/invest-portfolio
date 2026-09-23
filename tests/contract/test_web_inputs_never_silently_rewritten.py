"""DEF-005 (functional test manual D-02, 2026-09-23): a typed value is never rewritten.

``web/corp-action-form.js::intBox`` stripped every non-digit from the ratio box on every
keystroke — a "courtesy" — so a typed ``1.5`` became ``15`` in silence: the preview
computed a 15:3 reverse split where the owner meant 1.5:3, and the save button stayed
enabled. The server's E6/E6a rejection (the REAL guard, as the file's own preamble says)
never saw the decimal, because the browser had already turned it into a legal integer.

This file is the CLASS, not the instance: every ``web/*.js`` is scanned for the two shapes
that turn a typed value into a different legal one without telling the owner —

* a digit-only strip (``.replace(/[^0-9]/g, '')`` and its ``\\d`` / ``\\D`` spellings);
* an integer truncation of a typed value (``parseInt`` / ``Math.trunc`` / ``Math.floor`` on
  a ``.value``, or a ``String(parseInt(…))`` round trip that feeds the truncated number back
  into a payload).

The rule the fix implements — and the form now states inline — is: the value stays
exactly as typed, the box is flagged, the error is named, and preview + save are refused
until the owner fixes it. ``_PENDING`` lists a hit in a file outside this change's scope;
an entry that stops matching gets deleted, never kept.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_WEB = Path(__file__).resolve().parents[2] / "web"
_SKIP = {"echarts.min.js"}

#: `x.replace(/[^0-9]/g, '')`, `/[^\d]/`, `/\D/` — the value comes back with its decimal
#: point removed, which is a DIFFERENT number, not a cleaned one.
_DIGIT_STRIP = re.compile(
    r"\.replace\(\s*/(?:\[\^0-9\]|\[\^\\d\]|\\D)/[a-z]*\s*,\s*(?:''|\"\")\s*\)")
#: An integer truncation of a typed value.
_INT_TRUNC = re.compile(
    r"(?:parseInt|Math\.trunc|Math\.floor)\(\s*[A-Za-z_$][\w$.]*\.value\b"
    r"|String\(\s*parseInt\(")

#: Hits that are NOT a rewrite of typed input, by file, with the reason. An entry that
#: stops matching gets deleted.
_ALLOWED: dict[str, str] = {
    # `parseInt(sel.value, 10)` on a <select> of tax years: the options are integers by
    # construction, nothing is typed, and the parsed value is not written back anywhere.
    "settings-alerts.js": "parseInt on a <select> of integer years — no typed input",
}

#: Files with a known hit OUTSIDE this change's scope. Format: file -> why it is pending.
#: EMPTY since 2026-09-23: broker-import.js's `String(parseInt(to, 10))` (a typed 1.5 sent
#: as 1) now keeps the term as typed and refuses a non-integer inline (DEF-027 wave).
_PENDING: dict[str, str] = {}

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"(?<!:)//[^\n]*")


def _blank(match: re.Match[str]) -> str:
    return re.sub(r"[^\n]", " ", match.group(0))


def _code_only(text: str) -> str:
    """Comments blanked out (newlines kept) — the fixed file quotes the banned line in
    prose, and a guard that flags a comment is a guard its next reader switches off."""
    return _LINE_COMMENT.sub(_blank, _BLOCK_COMMENT.sub(_blank, text))


def _sources() -> list[Path]:
    return [p for p in sorted(_WEB.glob("*.js")) if p.name not in _SKIP]


def _hits(text: str) -> list[str]:
    code = _code_only(text)
    return [m.group(0) for m in _DIGIT_STRIP.finditer(code)] + \
           [m.group(0) for m in _INT_TRUNC.finditer(code)]


def test_the_detector_can_see_the_audited_line() -> None:
    """Detection power: the exact line the verifier quoted, and the cross-package shape."""
    assert _hits("const cleaned = n.value.replace(/[^0-9]/g, '');") == \
        [".replace(/[^0-9]/g, '')"]
    assert _hits("String(parseInt(to, 10)), String(parseInt(from, 10))") == \
        ["String(parseInt(", "String(parseInt("]
    assert _hits("const n = parseInt(box.value, 10);") == ["parseInt(box.value"]
    assert _hits("Math.floor(f.value)") == ["Math.floor(f.value"]
    # …and not an index, a layout figure or a string clean-up that keeps the number.
    assert _hits("parseInt(cb.dataset.n, 10)") == []
    assert _hits("Math.round(r.bottom + 8)") == []
    assert _hits("int.replace(/^0+(?=\\d)/, '')") == []
    # Comments do not count.
    assert _hits("/* n.value.replace(/[^0-9]/g, '') */ x = 1;") == []


@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
def test_no_field_silently_rewrites_what_the_owner_typed(path: Path) -> None:
    hits = _hits(path.read_text(encoding="utf-8"))
    if path.name in _PENDING:
        pytest.xfail(f"{path.name}: {_PENDING[path.name]}")
    if path.name in _ALLOWED:
        assert hits, f"{path.name} is allowed but has no hit — delete the entry"
        return
    assert hits == [], (
        f"{path.name} rewrites a typed value into a different legal one: {hits}. "
        "Leave the value as typed, flag the field, state the error inline and refuse "
        "preview/save (DEF-005) — never rewrite.")


def test_the_pending_list_is_not_stale() -> None:
    for name, why in {**_PENDING, **_ALLOWED}.items():
        path = _WEB / name
        assert path.exists(), f"{name} is pending but gone: delete the entry ({why})"
        assert _hits(path.read_text(encoding="utf-8")), (
            f"{name} no longer has the hit it is pending for: delete the entry ({why})")


def test_the_corp_action_form_refuses_a_decimal_inline() -> None:
    """The instance: the ratio boxes flag a non-integer, say why in the owner's words, and
    gate the preview and the save on it — the value itself is left alone."""
    src = _code_only((_WEB / "corp-action-form.js").read_text(encoding="utf-8"))
    assert "比例只收整數（不要填算好的小數）" in src
    assert "ratioTermsValid()" in src and "ca-int-bad" in src
    # `ready()` — the gate both the preview and the save button hang off — runs the check.
    ready = src[src.index("function ready()"):src.index("function syncSave()")]
    assert "ratioTermsValid()" in ready
    # The old courtesy strip is gone for good.
    assert "n.value = cleaned" not in src
