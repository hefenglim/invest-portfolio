"""M6-01 guard: every inbox surface that changes the pending set must repaint the badge.

``shell.js`` computes the sidebar 收件匣 badge once at load and exposes
``window.pdRefreshInboxBadge`` so a surface that changes the count can repaint it. Its own
comment names the three callers it exists for — 「a rebate confirm/skip, a dividend commit, an
inbox action」. ``rebate-inbox.js`` calls it from both of its mutating paths. ``inbox.js``, the
dividend half, called it from none, so 確認入帳 / 略過 / 取消忽略 moved the panel badge and left
the sidebar reading the old number with a matching stale title — permanently, since nothing was
scheduled to correct it. The captured network trace after a confirm contained no ``/count``
request at all: an omission, not a race.

This is asserted STATICALLY rather than only through the browser flow because the failure mode
is *a call that is not there*. A missing call renders identically to a present one and costs
nothing at runtime, so nothing observes it except the number itself — and a badge that is
merely out of date looks exactly like a badge that is correct. The e2e
(``tests/e2e/test_m6_inbox_badge_and_strip_flow.py``) proves the number actually moves; this
proves a NEW mutating path cannot ship without the call, which is the property that decays.

Deliberately shape-based, not a count: it pairs each mutation with the refresh in the same
function, so adding a fourth inbox action fails here on its own rather than passing because two
other functions call it twice.
"""

import re
from pathlib import Path

_WEB = Path(__file__).resolve().parents[2] / "web"
#: A CALL, not a mention — the comment beside each call names the symbol too, and a guard
#: satisfied by prose is a guard that survives the deletion of the line it is guarding.
_REFRESH = re.compile(r"window\.pdRefreshInboxBadge\s*\(")

#: file -> the functions that POST/DELETE something which changes the pending count.
_MUTATORS = {
    "inbox.js": ("act", "doUnskip", "undoConfirmed"),
    "rebate-inbox.js": ("act", "doConfirm"),   # the working reference — must stay working
}


def _body(source: str, name: str) -> str:
    """The source of ``function name(...)`` up to the next top-level ``function`` at the
    same indentation. Crude on purpose: a parser would be a second implementation of the
    thing under test, and every function here is a plain declaration."""
    start = re.search(r"^(\s*)(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(",
                      source, re.M)
    assert start is not None, f"{name}() not found — has it been renamed?"
    indent = start.group(1)
    rest = source[start.end():]
    nxt = re.search(r"^" + indent + r"(?:async\s+)?function\s", rest, re.M)
    return rest[: nxt.start()] if nxt else rest


def test_the_refresh_helper_still_exists_to_be_called() -> None:
    """A guard that greps for a renamed symbol passes forever."""
    shell = (_WEB / "shell.js").read_text(encoding="utf-8")
    assert "window.pdRefreshInboxBadge = " in shell, (
        "shell.js no longer publishes the badge refresh — this guard needs re-pointing")


def test_every_inbox_mutation_repaints_the_sidebar_badge() -> None:
    missing = []
    for filename, functions in _MUTATORS.items():
        source = (_WEB / filename).read_text(encoding="utf-8")
        for name in functions:
            if not _REFRESH.search(_body(source, name)):
                missing.append(f"{filename}::{name}()")
    assert not missing, (
        "these change the 收件匣 pending set without repainting the sidebar badge, so the "
        f"number the owner sees on every other page goes stale silently: {missing}")
