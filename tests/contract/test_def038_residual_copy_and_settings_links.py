"""DEF-038 (2026-09-23): no stale "not wired yet" copy, and every settings link lands on target.

① The dashboard's ⟳重新整理 tooltip read 「更新報價或重建統計（後端接線後生效）」 on a button
that has worked for months (shell.js). The class sweep found two more on
「設定 › AI 提示詞 › 數據變數總表」: 「其中 N 個待後端新增」 and 「需新增資料快照（spec 06）」, over
variables the backend registers as
``available=True`` (llm_insight/variables.py). A sentence that says a feature is missing when it
is not is as wrong as a number that is off — and nothing looked for it.

② 洞察管線's 「進化設定」 card linked to bare ``settings.html`` and landed on 帳戶與費率, while its
siblings carried ``#prompts``. settings.html now also resolves ``#<tab>/<anchor>`` to the element
in that tab carrying ``data-anchor="<anchor>"``, so a link can land ON its block.

This file pins the copy (comment-stripped, so historical notes about retired mocks stay legal)
and every settings link in the pipeline pages: a known tab, and an anchor that really exists.
"""

from __future__ import annotations

import re
from pathlib import Path

_WEB = Path(__file__).resolve().parents[2] / "web"
_NL = "\n"

#: Phrases that claim a shipped feature is not there yet. User-visible text only.
_STALE = re.compile(r"接線後生效|尚未接線|待後端新增|需新增資料快照")

_TABS = {"llm", "prompts", "scheduler", "accounts", "datasources", "alerts", "notify",
         "exports"}

#: Bare `settings.html` references that are right to land on the default tab, with why.
#: `(file, needle)`; each must still exist (see the staleness test).
_BARE_OK: dict[tuple[str, str], str] = {
    ("shell.js", "href: 'settings.html'"): "the sidebar's 系統設定 entry IS the default tab",
    ("whatsnew.js", "_currentPage() === 'settings.html'"): "a page-name comparison, not a link",
}


def _blank(m: re.Match[str]) -> str:
    return "".join(_NL if ch == _NL else " " for ch in m.group(0))


def _visible(path: Path) -> str:
    """The file with comments blanked: JS block/line comments and HTML comments."""
    src = path.read_text(encoding="utf-8")
    src = re.sub(r"<!--.*?-->", _blank, src, flags=re.S)
    src = re.sub(r"/\*.*?\*/", _blank, src, flags=re.S)
    return re.sub(r"(?<![:'\"\\])//[^\n]*", _blank, src)


def _web_files() -> list[Path]:
    return sorted(p for p in list(_WEB.glob("*.js")) + list(_WEB.glob("*.html"))
                  if p.name != "echarts.min.js")


def test_no_user_visible_copy_says_a_shipped_feature_is_missing() -> None:
    hits = []
    for path in _web_files():
        text = _visible(path)
        for m in _STALE.finditer(text):
            hits.append(f"{path.name}:{text.count(_NL, 0, m.start()) + 1} {m.group(0)}")
    assert not hits, f"stale 'not wired yet' copy: {hits}"


def test_the_refresh_tooltip_is_the_plain_sentence() -> None:
    shell = (_WEB / "shell.js").read_text(encoding="utf-8")
    assert "btn.title = '更新報價或重建統計';" in shell


def _anchors() -> set[str]:
    html = (_WEB / "settings.html").read_text(encoding="utf-8")
    found = set(re.findall(r'data-anchor="([\w-]+)"', html))
    for js in _WEB.glob("settings*.js"):
        found |= set(re.findall(r"dataset\.anchor\s*=\s*'([\w-]+)'", js.read_text("utf-8")))
    return found


def test_every_settings_link_names_a_tab_and_a_real_anchor() -> None:
    anchors = _anchors()
    assert {"templates", "vars", "evolution"} <= anchors, anchors   # guard the parse itself
    bad = []
    for path in _web_files():
        text = _visible(path)
        for m in re.finditer(r"settings\.html(#[\w/-]+)?(?=['\"])", text):
            line = text.count(_NL, 0, m.start()) + 1
            row = text.splitlines()[line - 1]
            frag = (m.group(1) or "").lstrip("#")
            glued = re.search(r"settings\.html'\s*\+.*?'#([\w/-]+)'", row)
            if not frag and glued:          # the redirect stubs: 'settings.html' + qs + '#tab'
                frag = glued.group(1)
            if not frag:
                if not any(f == path.name and needle in row for (f, needle) in _BARE_OK):
                    bad.append(f"{path.name}:{line} bare settings.html")
                continue
            tab, _, anchor = frag.partition("/")
            if tab not in _TABS:
                bad.append(f"{path.name}:{line} unknown tab #{tab}")
            elif anchor and anchor not in anchors:
                bad.append(f"{path.name}:{line} #{frag} — no data-anchor=\"{anchor}\"")
    assert not bad, "settings links that land on the wrong place:\n" + "\n".join(bad)


def test_the_bare_link_whitelist_is_not_stale() -> None:
    for (name, needle), _why in _BARE_OK.items():
        assert needle in (_WEB / name).read_text(encoding="utf-8"), (
            f"{name} no longer contains {needle!r} — drop it from _BARE_OK")


def test_the_evolution_card_lands_on_the_evolution_panel() -> None:
    hub = (_WEB / "pipeline-hub.html").read_text(encoding="utf-8")
    assert 'href="settings.html#prompts/evolution"><b>進化設定</b>' in hub
    prompts = (_WEB / "settings-prompts.js").read_text(encoding="utf-8")
    evo = prompts.index("'自我進化設定'")
    assert "panel.dataset.anchor = 'evolution'" in prompts[evo - 400:evo], (
        "the 自我進化設定 panel lost its data-anchor")
    router = (_WEB / "settings.html").read_text(encoding="utf-8")
    assert "function goAnchor(" in router and "split('/')" in router
