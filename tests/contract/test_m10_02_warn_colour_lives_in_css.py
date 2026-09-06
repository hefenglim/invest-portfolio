"""M10-02 tail: the amber 'warn' face is a class with a stylesheet rule, not an inline style.

The third toast face and the amber 上次執行 dot were added with their colour written INLINE
(``t.style.borderColor = …; t.style.color = …`` in ``shell.js``, ``dot.style.background`` in
``settings-scheduler.js``) because ``styles.css`` was outside that change's range lock. The
class name ``toast-warn`` existed with no rule behind it — the next person to add one would
have watched the inline declaration outrank it and wondered why. This moves the same two
values (``--amber`` — the token ``.pill-warn`` uses — and the 0.5-alpha amber border) into
``.toast-warn`` / ``.dot-warn`` and drops the inline assignments. Same pixels, one owner.
"""

import re
from pathlib import Path

_WEB = Path(__file__).resolve().parents[2] / "web"
_CSS = (_WEB / "styles.css").read_text(encoding="utf-8")
_SETTINGS = (_WEB / "settings.html").read_text(encoding="utf-8")
_SHELL = (_WEB / "shell.js").read_text(encoding="utf-8")
_SCHED = (_WEB / "settings-scheduler.js").read_text(encoding="utf-8")


def test_toast_warn_has_a_stylesheet_rule_with_the_inline_values() -> None:
    m = re.search(r"\.toast-warn\s*\{([^}]*)\}", _CSS)
    assert m, "styles.css has no .toast-warn rule — the amber face exists only as an inline style"
    body = m.group(1)
    assert re.search(r"color:\s*var\(--amber\)", body), body      # .pill-warn's token
    assert re.search(r"border-color:\s*rgba\(217,\s*161,\s*63,\s*0?\.5\)", body), body


def test_dot_warn_sits_beside_dot_ok_and_dot_err() -> None:
    assert re.search(r"\.dot-warn\s*\{\s*background:\s*var\(--amber\);?\s*\}", _SETTINGS), (
        "settings.html defines .dot-ok / .dot-err but no .dot-warn")


def test_the_toast_sets_no_inline_colour() -> None:
    m = re.search(r"window\.toast = function \(msg, kind, sub\) \{(.*?)\n  \};", _SHELL, re.S)
    assert m, "window.toast not found"
    assert "toast-warn" in m.group(1)
    assert ".style." not in m.group(1), "the warn toast still paints itself inline"


def test_the_partial_dot_uses_the_class() -> None:
    assert "dot.style.background" not in _SCHED, "the partial dot still paints itself inline"
    assert re.search(r"'partial'\s*\?\s*'dot-warn'", _SCHED), "partial does not map to dot-warn"
