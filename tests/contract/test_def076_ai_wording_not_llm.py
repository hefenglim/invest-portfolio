"""DEF-076 addendum (owner ruling 2026-09-26, extending ruling ⑤ b): the product says 「AI」.

Ruling ⑤ b made the quota alert's title 「AI 額度偏低」 because the rule, the settings tab
(「AI 與額度」), the top-bar chip (「AI 額度」) and the fail log (「AI 失敗紀錄」) all say 「AI」.
The owner then widened it to every user-visible 「LLM 額度」, and the scan behind this file
found the same word in more places the owner reads: the pipeline hub's 「LLM 額度」 card and
its skip reasons, the dry-run gate name, the 資料庫統計 labels, the 系統紀錄 action labels,
the AI-input degrade cards and the 數據變數庫 descriptions. Everything a user reads says 「AI」;
「LLM」 stays in code, comments, identifiers and docstrings (not user-facing).

Two structural scans, no hand-written file list:

* ``web/`` — every page and script. Comments (JS ``//`` and ``/* */``, HTML ``<!-- -->``,
  CSS) are removed with a small tokenizer; what is left — string literals, template
  literals, HTML text and attribute values — must not contain 「LLM」. 「LiteLLM」 is the
  gateway's product name (like FinMind or yfinance on the same pages) and is not a hit.
* ``portfolio_dash/`` — every non-docstring string constant that contains Chinese (the
  user-facing messages: labels, gate texts, job details, error bodies, variable
  descriptions) must not contain 「LLM」.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_WEB = _REPO / "web"
_PKG = _REPO / "portfolio_dash"
_CJK = re.compile(r"[一-鿿]")
_WORD = re.compile(r"LLM")


def _llm_in(text: str) -> bool:
    return bool(_WORD.search(text.replace("LiteLLM", "")))


def js_visible_text(src: str) -> list[tuple[int, str]]:
    """(line, text) of every string / template literal in JS source, comments skipped."""
    out: list[tuple[int, str]] = []
    i, n, line = 0, len(src), 1
    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if ch == "\n":
            line += 1
            i += 1
        elif ch == "/" and nxt == "*":
            end = src.find("*/", i + 2)
            end = n if end == -1 else end + 2
            line += src.count("\n", i, end)
            i = end
        elif ch == "/" and nxt == "/":
            end = src.find("\n", i)
            i = n if end == -1 else end
        elif ch in "'\"`":
            j, start_line = i + 1, line
            while j < n and src[j] != ch:
                if src[j] == "\\":
                    j += 1
                elif src[j] == "\n":
                    line += 1
                    if ch != "`":
                        break
                j += 1
            out.append((start_line, src[i + 1:j]))
            i = j + 1
        else:
            i += 1
    return out


_SCRIPT = re.compile(r"(<script\b[^>]*>)(.*?)</script>", re.S | re.I)
_STYLE = re.compile(r"<style\b[^>]*>.*?</style>", re.S | re.I)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)


def html_visible_text(src: str) -> list[tuple[int, str]]:
    """(line, text) of an HTML page's markup text + attributes + inline-script literals."""
    out: list[tuple[int, str]] = []

    def blank(m: re.Match[str]) -> str:
        return "\n" * m.group(0).count("\n")

    for m in _SCRIPT.finditer(src):
        base = src.count("\n", 0, m.start(2))
        out.extend((base + ln, t) for ln, t in js_visible_text(m.group(2)))
    markup = _SCRIPT.sub(blank, src)
    markup = _STYLE.sub(blank, markup)
    markup = _HTML_COMMENT.sub(blank, markup)
    out.extend((k + 1, text) for k, text in enumerate(markup.split("\n")))
    return out


def web_hits() -> list[str]:
    hits: list[str] = []
    for path in sorted(_WEB.iterdir()):
        if path.name == "echarts.min.js" or path.suffix not in (".js", ".html"):
            continue
        src = path.read_text(encoding="utf-8")
        texts = js_visible_text(src) if path.suffix == ".js" else html_visible_text(src)
        hits.extend(f"web/{path.name}:{ln}: {t.strip()[:80]}" for ln, t in texts if _llm_in(t))
    return hits


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) \
                and body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant):
            ids.add(id(body[0].value))
    return ids


def backend_hits() -> list[str]:
    hits: list[str] = []
    for path in sorted(_PKG.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docs = _docstring_ids(tree)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docs and _CJK.search(node.value)
                    and _llm_in(node.value)):
                rel = path.relative_to(_REPO).as_posix()
                hits.append(f"{rel}:{node.lineno}: {node.value.strip()[:80]}")
    return hits


def test_no_page_or_script_shows_llm() -> None:
    hits = web_hits()
    assert not hits, "user-visible 「LLM」 in web/ — say 「AI」:\n" + "\n".join(hits)


def test_no_chinese_backend_message_says_llm() -> None:
    hits = backend_hits()
    assert not hits, "a Chinese message says 「LLM」 — say 「AI」:\n" + "\n".join(hits)


def test_the_scanners_see_what_they_must_and_skip_comments() -> None:
    """Self-test: a literal, a template, HTML text and an attribute are seen; comments and
    the 「LiteLLM」 product name are not (a scanner that saw nothing would pass trivially)."""
    js = "// LLM\n/* LLM */\nvar a = 'LLM 額度';\nvar b = `x ${y} LLM`;\nvar c = 'LiteLLM';\n"
    assert [ln for ln, t in js_visible_text(js) if _llm_in(t)] == [3, 4]
    html = ('<!-- LLM -->\n<style>/* LLM */</style>\n<span title="LLM 額度">LLM 服務</span>\n'
            "<script>\n// LLM\nx('LLM 呼叫');\n</script>\n")
    assert sorted(ln for ln, t in html_visible_text(html) if _llm_in(t)) == [3, 6]
