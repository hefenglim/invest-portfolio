"""F-07 guard: a degrade panel's action button must actually do something.

A ``.degrade-panel`` is what the user sees when a feature cannot run — 「AI 未啟用」,
「AI 額度用盡」, 「LLM 服務暫時無法連線」. Two of the three offer a way out as an ``<a>``
(前往 AI 與額度設定). The third offers a 「重試」 ``<button>`` that had no listener anywhere in
the repository, so its card was the only one that left the reader on a dead end — and it is
that card's ONLY action.

Nothing catches this by inspection: a button with no listener renders identically to one with
a listener, and a wiring sweep that counts listeners counts zero here and reports nothing
because there is nothing to count. So the shape is asserted statically instead: an action on
a degrade panel is a ``<button>`` with an id that some JS references, or it is an ``<a href>``
that navigates. There is no third kind that is still an offer of help.
"""

import re
from pathlib import Path

_WEB = Path(__file__).resolve().parents[2] / "web"
_PANEL = re.compile(r'<div class="degrade-panel"[^>]*>(.*?)</div>', re.S)
_BUTTON = re.compile(r"<button[^>]*>")
_ID = re.compile(r'id="([^"]+)"')


def _js_source() -> str:
    return "\n".join(p.read_text(encoding="utf-8")
                     for p in sorted(_WEB.glob("*.js")) if p.name != "echarts.min.js")


def _panel_buttons() -> list[tuple[str, str | None]]:
    out: list[tuple[str, str | None]] = []
    for path in sorted(_WEB.glob("*.html")):
        for panel in _PANEL.finditer(path.read_text(encoding="utf-8")):
            for button in _BUTTON.finditer(panel.group(1)):
                found = _ID.search(button.group(0))
                out.append((path.name, found.group(1) if found else None))
    return out


def test_the_scan_finds_the_panel_it_exists_for() -> None:
    """A guard that matches nothing passes forever."""
    assert _panel_buttons(), "no degrade-panel buttons found — has the markup changed?"


def test_every_degrade_panel_button_is_referenced_by_some_script() -> None:
    js = _js_source()
    orphans = [f"{page}:{ident or '<no id>'}"
               for page, ident in _panel_buttons()
               if ident is None or ident not in js]
    assert not orphans, (
        "a degrade panel offers an action that no script listens for — the user is left on "
        f"a dead end with a button that looks live: {orphans}")
