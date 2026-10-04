"""R12 (final review of the CR-07 merge, 2026-10-04): ONE helper decides what a corporate-action
acknowledgement names.

Two doors acknowledge corporate-action warnings — the form (``web/corp-action-form.js``) and the
公司行動 ledger tab's edit dialog (``web/ledger.js`` ``editAction``) — and each built its own
``(code, text)`` list. Two copies of one rule is how the second drifted from the first, and R1
showed the rule itself was wrong (the text is api.js's RESOLVED copy). The rule now has one home,
``window.pdActionAckKeys`` in corp-action-form.js (the server's per-issue ``ack_key``), and the
edit dialog calls it — unguarded, so every page that runs the dialog must load the helper first.
"""

import re
from pathlib import Path

_WEB = Path(__file__).resolve().parents[2] / "web"
_SCRIPT = re.compile(r'<script\s+src="([^"?]+)')


def _js(name: str) -> str:
    return (_WEB / name).read_text(encoding="utf-8")


def test_the_helper_is_defined_once_and_both_doors_use_it() -> None:
    defining = [p.name for p in sorted(_WEB.glob("*.js"))
                if "window.pdActionAckKeys =" in p.read_text(encoding="utf-8")]
    assert defining == ["corp-action-form.js"], defining
    form, ledger = _js("corp-action-form.js"), _js("ledger.js")
    assert "acked_keys = ackKeys(" in form
    assert "window.pdActionAckKeys(err.issues)" in ledger
    # Nothing in web/ reads an issue's ack_key except the helper's own file — and nothing
    # rebuilds the old (code, text) acknowledgement anywhere.
    readers = [p.name for p in sorted(_WEB.glob("*.js"))
               if ".ack_key" in p.read_text(encoding="utf-8")]
    assert readers == ["corp-action-form.js"], readers
    assert all("acked_warnings" not in p.read_text(encoding="utf-8")
               for p in _WEB.glob("*.js"))


def test_every_page_running_the_edit_dialog_loads_the_helper_first() -> None:
    pages = [p for p in sorted(_WEB.glob("*.html"))
             if "ledger.js" in _SCRIPT.findall(p.read_text(encoding="utf-8"))]
    assert pages, "no page loads ledger.js — the scan sees nothing"
    for page in pages:
        scripts = _SCRIPT.findall(page.read_text(encoding="utf-8"))
        assert "corp-action-form.js" in scripts, page.name
        assert scripts.index("corp-action-form.js") < scripts.index("ledger.js"), page.name
