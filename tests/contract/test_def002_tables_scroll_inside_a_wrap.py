"""DEF-002 (2026-09-23): every table in web/ scrolls inside its OWN horizontal scroller.

「設定 › AI 提示詞 › 數據變數總表」 appended 11 tables straight into a flex column
(`settings-prompts.js` `el('table', 'data vars-table')` → `sec.appendChild(table)`), and
`table.data td` is `white-space: nowrap`, so the description column set the DOCUMENT width —
3,673px at 390px, 3,874px at 1,440px — the moment the panel opened. Every other table on the
site sits in a `.table-wrap` (styles.css: `overflow-x: auto`), so the rule was a convention
nobody had written down, and the one table that missed it was invisible to the layout sweep
because it lived under a closed <details> (fixed separately in
`tests/e2e/test_no_horizontal_scroll.py`).

This is the class guard: it enumerates EVERY table creation site in web/ — `el('table', …)`,
`document.createElement('table')` in the JS, `<table>` in the HTML — and requires each to be
inside `.table-wrap`, or inside a container on :data:`_SCROLLERS` whose CSS rule is READ BACK
here and must really declare `overflow-x: auto`. A whitelist entry therefore cannot outlive
the CSS that justifies it. The numbers are pinned too, so a new table has to be looked at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

_WEB = Path(__file__).resolve().parents[2] / "web"

#: container token (a class, or `#id`) -> (file holding its CSS, the rule's selector, reason).
#: Each rule is read back and must contain `overflow-x: auto` — see `_rule_scrolls`.
_SCROLLERS: dict[str, tuple[str, str, str]] = {
    "rbt-detail": ("styles.css", ".rbt-detail",
                   "折讓款明細: the collapsible detail block is itself the scroller"),
    "ca-acct": ("corp-action-form.js", ".ca-modal .ca-acct",
                "公司行動 per-account card: the card is the scroller (injected CSS)"),
    "#users-wrap": ("settings.css", "#users-wrap",
                    "授權用戶: host shared with the empty-state sentence (DEF-002 sweep)"),
    "ccyret-table-wrap": ("styles.css", ".ccyret-table-wrap",
                          "各幣別報酬: its own wrap scrolls at <=760px; wider widths are "
                          "measured by the e2e sweep with the <details> expanded"),
}

#: Tables that need no scroller, each with the reason. `(file, table class)`.
_NO_SCROLLER_NEEDED: dict[tuple[str, str], str] = {
    ("index.html", "fresh"): "資料新鮮度: two-column symbol/date list inside a 1fr grid "
                             "column; measured by the e2e sweep with the <details> expanded",
}

#: The pinned census (2026-09-23). A new table changes a number here, which is the point:
#: somebody has to decide which bucket it belongs in.
_EXPECTED_JS_SITES = 14
_EXPECTED_HTML_TABLES = 31

_JS_TABLE = re.compile(
    r"(?:const|let|var)\s+(\w+)\s*=\s*"
    r"(?:el\(\s*['\"]table['\"]\s*(?:,\s*['\"]([^'\"]*)['\"])?|"
    r"document\.createElement\(\s*['\"]table['\"]\s*\))"
)


def _rule_scrolls(css_file: str, selector: str) -> bool:
    text = (_WEB / css_file).read_text(encoding="utf-8")
    for m in re.finditer(re.escape(selector) + r"\s*\{([^}]*)\}", text):
        if re.search(r"overflow-x\s*:\s*(auto|scroll)", m.group(1)):
            return True
    return False


def _js_sites() -> list[dict[str, str]]:
    """Every JS table: its variable, its class, and the container it is appended into."""
    sites: list[dict[str, str]] = []
    for path in sorted(_WEB.glob("*.js")):
        if path.name == "echarts.min.js":
            continue
        src = path.read_text(encoding="utf-8")
        for m in _JS_TABLE.finditer(src):
            var, cls = m.group(1), m.group(2) or ""
            after = src[m.end():]
            app = re.search(
                r"(\w+)\.(?:appendChild|replaceChildren|append)\(\s*" + re.escape(var) + r"\s*\)",
                after)
            parent = app.group(1) if app else ""
            before = src[: m.end() + (app.start() if app else 0)]
            decl = None
            for d in re.finditer(
                r"(?:const|let|var)\s+" + re.escape(parent)
                + r"\s*=\s*(el\(\s*['\"]\w+['\"]\s*,\s*['\"]([^'\"]*)['\"]"
                + r"|\$\(\s*['\"]([^'\"]+)['\"]\s*\))",
                before,
            ):
                decl = d
            container = ""
            if decl is not None:
                container = decl.group(2) or decl.group(3) or ""
            line = src.count("\n", 0, m.start()) + 1
            sites.append({"file": path.name, "line": str(line), "var": var, "cls": cls,
                          "parent": parent, "container": container})
    return sites


@dataclass
class _HtmlTable:
    file: str
    line: int
    cls: str
    ancestors: list[str] = field(default_factory=list)


class _TableFinder(HTMLParser):
    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
             "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, list[str]]] = []
        self.tables: list[_HtmlTable] = []
        self.file = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        tokens = (a.get("class") or "").split()
        if a.get("id"):
            tokens.append("#" + str(a["id"]))
        if tag == "table":
            ancestors = [t for _, toks in self.stack for t in toks]
            self.tables.append(_HtmlTable(self.file, self.getpos()[0], a.get("class") or "",
                                          ancestors))
        if tag not in self._VOID:
            self.stack.append((tag, tokens))

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return


def _html_tables() -> list[_HtmlTable]:
    out: list[_HtmlTable] = []
    for path in sorted(_WEB.glob("*.html")):
        finder = _TableFinder()
        finder.file = path.name
        finder.feed(path.read_text(encoding="utf-8"))
        out.extend(finder.tables)
    return out


def _classify(file: str, cls: str, containers: list[str]) -> str:
    if "table-wrap" in containers:
        return "wrapped"
    for tok in containers:
        if tok in _SCROLLERS:
            return "scroller:" + tok
    first = cls.split()[0] if cls.split() else ""
    if (file, first) in _NO_SCROLLER_NEEDED:
        return "whitelist"
    return "UNWRAPPED"


def test_every_whitelisted_scroller_really_scrolls() -> None:
    """A whitelist entry is a claim about CSS — read the CSS back, do not trust the claim."""
    dead = [tok for tok, (css, sel, _why) in _SCROLLERS.items() if not _rule_scrolls(css, sel)]
    assert not dead, f"these containers no longer declare overflow-x: auto: {dead}"


def test_every_js_table_is_inside_a_horizontal_scroller() -> None:
    sites = _js_sites()
    assert len(sites) == _EXPECTED_JS_SITES, (
        f"JS table census changed ({len(sites)} != {_EXPECTED_JS_SITES}) — classify the new "
        f"site and update the count: {[(s['file'], s['line']) for s in sites]}")
    bad = []
    for s in sites:
        tokens = s["container"].split() if not s["container"].startswith("#") else [s["container"]]
        verdict = _classify(s["file"], s["cls"], tokens)
        if verdict == "UNWRAPPED":
            bad.append(f"{s['file']}:{s['line']} `{s['var']}` (class {s['cls']!r}) is appended "
                       f"into {s['parent'] or '?'} ({s['container'] or 'no class'})")
    assert not bad, "tables that would widen the page instead of scrolling:\n" + "\n".join(bad)


def test_every_html_table_is_inside_a_horizontal_scroller() -> None:
    tables = _html_tables()
    assert len(tables) == _EXPECTED_HTML_TABLES, (
        f"HTML table census changed ({len(tables)} != {_EXPECTED_HTML_TABLES}): "
        f"{[(t.file, t.line) for t in tables]}")
    bad = [f"{t.file}:{t.line} <table class={t.cls!r}>"
           for t in tables if _classify(t.file, t.cls, t.ancestors) == "UNWRAPPED"]
    assert not bad, "tables with no .table-wrap / scroller ancestor:\n" + "\n".join(bad)


def test_the_variable_tables_are_the_reproduction() -> None:
    """Guard the guard: the exact DEF-002 site must be found AND classified as wrapped."""
    hits = [s for s in _js_sites() if s["file"] == "settings-prompts.js"
            and "vars-table" in s["cls"]]
    assert len(hits) == 1, hits
    assert "table-wrap" in hits[0]["container"].split(), hits[0]
