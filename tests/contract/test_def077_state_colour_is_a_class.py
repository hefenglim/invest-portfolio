"""DEF-077 R7 (verifier, 2026-09-26): a STATE colour is a class with a stylesheet rule, never an
inline style — across ``web/``, not in two hand-picked functions.

R6 rendered the FX-triangle note with ``el('div', 'fresh-note')`` and then
``if (t.ok === false) n.style.color = 'var(--amber)'``. ``.fresh-note`` is amber by DEFAULT
(``styles.css``), so the inline colour could only ADD a state the default already had: 一致 and
無法比較 stayed in the amber warning box, and the e2e — which read ``n.style.color``, the inline
value — was green. The same shape was live on the AI 與額度 status chip: 額度偏低 set amber
INLINE and only 啟用中 cleared it, so after a save re-rendered the chip as 已關閉 or 額度歸零 it
kept the amber.

M10-02 (``test_m10_02_warn_colour_lives_in_css.py``) already stated the rule — "the amber warn
face is a class, not an inline style" — but guarded only the two functions it had fixed (the
toast, the partial dot). This is the rule over every file: every inline assignment whose text
names a state colour token (``--amber`` / ``--up`` / ``--down`` / ``--ok`` or their rgba) is a
REVIEWED site below with its reason, and anything else fails. A reviewed entry that no longer
exists fails too, so the list cannot rot. Measured 2026-09-26 at R7: 71 inline style
assignments in 13 files (vendored ``echarts.min.js`` excluded); 12 of them named a state
colour, 8 remain after R7 (the FX-triangle note's one and the AI chip's three are classes
now) — each on an element built fresh for one state, or whose every branch assigns.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

_WEB = Path(__file__).resolve().parents[2] / "web"
_VENDORED = {"echarts.min.js"}

_ASSIGN = re.compile(
    r"\.style\.(?:color|background|backgroundColor|borderColor|borderLeftColor|borderTopColor"
    r"|cssText)\s*=(?!=)|\.style\.setProperty\(|setAttribute\(\s*['\"]style['\"]")
_STATE = re.compile(r"--(?:amber|up|down|ok)\b|rgba\(\s*(?:217|168)\s*,\s*(?:161|123)"
                    r"|rgba\(\s*(?:240|216)\s*,\s*(?:84|67)|rgba\(\s*(?:47|31|79|46)\s*,")

#: (file, the assignment's first line, stripped) → (how many times, why an inline colour is safe
#: there). Reviewed 2026-09-26 (DEF-077 R7 class scan). "Safe" means no state can inherit
#: another state's colour: the element is created for exactly one state, or every branch of
#: the assignment writes the property.
_REVIEWED: dict[tuple[str, str], tuple[int, str]] = {
    ("broker-import.js", "input.style.borderColor = bad ? 'var(--up)' : '';"):
        (1, "both branches write the property; the .input-error class is toggled beside it"),
    ("broker-import.js", "msg.style.color = 'var(--up)';"):
        (1, "the message element is created only when the ratio is bad and removed otherwise"),
    ("cash.js", "if (String(r.balance).indexOf('-') === 0) { tdBal.style.color = "
                "'var(--amber)'; }"):
        (1, "a plain td.num rebuilt on every render; amber marks a negative running balance"),
    ("detail.js", "badge.style.cssText = 'margin-left:8px;font-weight:700;color:'"):
        (1, "a classless span rebuilt on every render; both states write the colour "
            "(✓ 對帳一致 --down / ⚠ 對帳不一致 --up)"),
    ("detail.js", "row.style.cssText = 'margin-top:4px;color:var(--up)';"):
        (2, "issue lines created only for an issue; always the error colour"),
    ("instruments.js", "warn.style.color = 'var(--amber)';"):
        (1, "a .hint created only when the symbol has ledger history; always a warning"),
    ("settings-llm.js", "qv.style.color = remNum !== null && remNum <= 0"):
        (1, "#quota-value: every branch writes the colour (歸零 --up / 偏低 --amber / else '')"),
}


def _sources() -> list[tuple[str, list[str]]]:
    out: list[tuple[str, list[str]]] = []
    for path in sorted(_WEB.glob("*.js")) + sorted(_WEB.glob("*.html")):
        if path.name in _VENDORED:
            continue
        out.append((path.name, path.read_text(encoding="utf-8").splitlines()))
    return out


def _statement(lines: list[str], i: int) -> str:
    """The assignment from line ``i`` to the line that ends it (a trailing ``;``), at most 4."""
    parts: list[str] = []
    for line in lines[i:i + 4]:
        parts.append(line)
        if line.split("//")[0].rstrip().endswith(";"):
            break
    return "\n".join(parts)


def _inline_assignments() -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    for name, lines in _sources():
        for i, line in enumerate(lines):
            if _ASSIGN.search(line):
                found.append((name, line.strip(), _statement(lines, i)))
    return found


def _state_colour_sites() -> Counter[tuple[str, str]]:
    return Counter((name, first) for name, first, stmt in _inline_assignments()
                   if _STATE.search(stmt))


def test_the_scan_sees_the_inline_assignments_it_exists_to_judge() -> None:
    """A scanner that matched nothing would pass every file — prove it reads this code base."""
    everything = _inline_assignments()
    assert len(everything) >= 50, len(everything)
    assert len({name for name, _, _ in everything}) >= 10


def test_every_inline_state_colour_is_a_reviewed_site() -> None:
    sites = _state_colour_sites()
    unreviewed = {k: n for k, n in sites.items() if k not in _REVIEWED}
    assert not unreviewed, (
        "a state colour set INLINE — make it a class with a stylesheet rule instead (DEF-077: "
        "an inline colour cannot remove a colour the element's class paints by default, and "
        "a branch that does not clear it hands its colour to the next state). If an inline "
        f"colour is genuinely safe, add it to _REVIEWED with the reason: {unreviewed}")
    wrong_count = {k: (sites[k], _REVIEWED[k][0]) for k in sites if sites[k] != _REVIEWED[k][0]}
    assert not wrong_count, f"(found, reviewed) counts differ: {wrong_count}"


def test_no_reviewed_entry_has_rotted() -> None:
    gone = set(_REVIEWED) - set(_state_colour_sites())
    assert not gone, f"reviewed sites that no longer exist — delete them: {sorted(gone)}"


def test_the_fx_triangle_note_and_the_ai_chip_take_their_state_from_a_class() -> None:
    """The two R7 sites, by name: the class exists in the stylesheet and the code applies it."""
    css = (_WEB / "styles.css").read_text(encoding="utf-8")
    assert re.search(r"\.fresh-note\.fresh-note-info\s*\{[^}]*color:\s*var\(--text-2\)", css)
    app = (_WEB / "app.js").read_text(encoding="utf-8")
    assert "t.ok === false ? 'fresh-note' : 'fresh-note fresh-note-info'" in app
    llm = (_WEB / "settings-llm.js").read_text(encoding="utf-8")
    assert "chip.className = 'pill pill-warn';" in llm
    assert "chip.style." not in llm
