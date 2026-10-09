"""L5 (demo audit 2026-09-16, re-verified 2026-09-17): zh copy uses full-width punctuation.

The audit found 「配息／配股偵測,台美馬全市場」 and 「排程:每日 06:00(設定→排程 可調)」 —
half-width `,` `:` `(` inside Traditional-Chinese copy. The fix changed the three sentences
the audit quoted; the re-verification then counted 25 more of the same kind across eleven
files (the rebate inbox alone had 16 rendered instances). An instance was fixed, the class
was not — so this file is the class: every string the web layer can RENDER (HTML text nodes
and the title / placeholder / aria-label / alt / value attributes, plus every string and
template literal in the page scripts) is scanned for a half-width `, : ; ! ? ( )` that
touches a CJK character on either side — or is separated from one by spaces only (widened
2026-09-22, L5-b; see `_MIXED`) — and the count must be zero.

What is deliberately NOT scanned: JS comments and HTML comments (not rendered), `<style>`
bodies, and any punctuation that touches only ASCII on both sides (an English sentence, a
code-like token such as `date(YYYY-MM-DD)` when it stands alone). `/` `~` `·` are not in
the set: they are not the confusable pairs the audit was about.

The backend is the SAME class on a different surface, and is scanned below by AST
(string constants, docstrings excluded): batch 1 (owner ruling 2026-09-17) swept the 57
user-facing messages, batch 2 the what's-new catalog (29 strings); `_BACKEND_PENDING`
is empty and stays as the home for a future deferral. Regexes that deliberately accept
both widths, prompt text, one fee formula, the `http(s)` scheme notation and one message
that lists forbidden characters are allowed by name with a reason.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_WEB_DIR = Path(__file__).resolve().parents[2] / "web"

#: CJK ideographs + CJK punctuation block + full-width forms.
_CJK = "　-〿一-鿿＀-￯"
#: A half-width confusable next to a CJK character on either side — touching it, or
#: separated from it by spaces only.
#:
#: ⚠ "Touching" was the whole rule until the second full re-verification (2026-09-22,
#: L5-b), and it missed the English-typing habit of a space AFTER the mark: the daily
#: digest's run line 「組合 +2.53%, 警示 0, 訊號 0; 示範模式略過推播」 has no half-width mark
#: that touches a CJK character, and f-string interpolation splits it further, so the
#: literal parts are `", 警示 "` / `"; "`. The same widening measured 16 more strings of the
#: shape 「年化報酬 (XIRR)」 / 「notify: 靜音時段」: 12 fixed with it, 4 named in the allowlist.
_MIXED = re.compile("[" + _CJK + "][ \t]*[,:;!?()]|[,:;!?()][ \t]*[" + _CJK + "]")

#: Rendered strings that are ALLOWED to mix (none today). Format: "file.js:exact text".
#: An entry needs a reason next to it; an entry nobody can justify gets deleted.
_ALLOWED: frozenset[str] = frozenset()

_QUOTES = ("'", '"', "`")


def js_strings(src: str) -> list[tuple[int, str]]:
    """Every string / template literal in *src* with its 1-based line; comments skipped.

    A small state machine rather than a regex: a regex cannot tell a `//` inside a URL
    string from a line comment, and the comment bodies are exactly what must NOT be
    scanned (they are full of half-width punctuation next to zh, and are never rendered).
    A single- or double-quoted literal that reaches a newline is abandoned there, so a
    regex literal containing a quote cannot desynchronise the rest of the file.
    """
    out: list[tuple[int, str]] = []
    i, n, line = 0, len(src), 1
    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
            i += 1
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            line += src.count("\n", i, j)
            i = j
            continue
        if c in _QUOTES:
            j = i + 1
            buf: list[str] = []
            while j < n and src[j] != c:
                if src[j] == "\\":
                    buf.append(src[j:j + 2])
                    j += 2
                    continue
                if c != "`" and src[j] == "\n":
                    break
                buf.append(src[j])
                j += 1
            text = "".join(buf)
            out.append((line, text))
            line += text.count("\n")
            i = j + 1
            continue
        i += 1
    return out


def html_strings(src: str) -> list[tuple[int, str]]:
    """Rendered text of an HTML page: text nodes, user-visible attributes, script strings."""
    src = re.sub(r"<!--.*?-->", lambda m: "\n" * m.group(0).count("\n"), src, flags=re.S)
    out: list[tuple[int, str]] = []
    for m in re.finditer(r"<script\b[^>]*>(.*?)</script>", src, flags=re.S):
        base = src.count("\n", 0, m.start(1)) + 1
        out.extend((base + ln - 1, t) for ln, t in js_strings(m.group(1)))
    rest = re.sub(
        r"<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>",
        lambda m: "\n" * m.group(0).count("\n"), src, flags=re.S,
    )
    attr = r'(?:title|placeholder|aria-label|alt|data-tip|value)="([^"]*)"'
    for m in re.finditer(attr, rest):
        out.append((rest.count("\n", 0, m.start()) + 1, m.group(1)))
    for m in re.finditer(r">([^<]+)<", rest):
        out.append((rest.count("\n", 0, m.start()) + 1, m.group(1)))
    return out


def mixed_punctuation(strings: list[tuple[int, str]]) -> list[tuple[int, str]]:
    return [(ln, t.strip()) for ln, t in strings if _MIXED.search(t)]


def _web_files() -> list[Path]:
    pages = sorted(_WEB_DIR.glob("*.html"))
    scripts = [p for p in sorted(_WEB_DIR.glob("*.js")) if p.name != "echarts.min.js"]
    return pages + scripts


def test_the_detector_can_see_the_audited_strings() -> None:
    """Positive control: the audit's three quoted strings and the re-verification's shapes."""
    audited = [
        "配息／配股偵測,台美馬全市場",
        "排程:每日 06:00(設定→排程 可調)",
        "偵測是持續進行的:每日排程掃描,",
        "改記為分割(SPLIT)",
        "晚於今日,確認無誤?",
        # L5-b (2026-09-22): a space between the mark and the zh text
        ": 組合 ",
        ", 警示 ",
        "年化報酬 (XIRR)",
        "notify: 靜音時段",
    ]
    for text in audited:
        assert _MIXED.search(text), text
    # …and their full-width forms, an English sentence, and a code-like token are clean.
    for text in ("配息／配股偵測，台美馬全市場", "改記為分割（SPLIT）", "Hello, world (ok)!",
                 "date(YYYY-MM-DD)・shares", "逐則 token／成本追蹤，協助評估用量",
                 # an English run line keeps its own marks; only the zh tail's are full-width
                 "3 alert(s) [a, b], 2 dispatched; notify：無啟用通道", "notify: error"):
        assert not _MIXED.search(text), text


def test_js_scanner_skips_comments_and_reads_literals() -> None:
    src = (
        "// 註解,不算\n"
        "/* 區塊註解:也不算 */\n"
        "const a = '已有抓取正在進行,請稍候';\n"
        "const u = 'http://x/y'; // 網址,含雙斜線\n"
        "const t = `多行\n模板(字串)`;\n"
    )
    found = mixed_punctuation(js_strings(src))
    assert found == [(3, "已有抓取正在進行,請稍候"), (5, "多行\n模板(字串)")]


def test_html_scanner_reads_text_nodes_attributes_and_inline_scripts() -> None:
    src = (
        "<!-- 註解,不算 -->\n"
        "<span title=\"提示:半形\">內文,半形</span>\n"
        "<style>.x{content:'樣式,不算'}</style>\n"
        "<script>el('td', null, '(未命名)');</script>\n"
    )
    found = set(mixed_punctuation(html_strings(src)))
    assert found == {(2, "提示:半形"), (2, "內文,半形"), (4, "(未命名)")}


@pytest.mark.parametrize("path", _web_files(), ids=lambda p: p.name)
def test_rendered_zh_copy_uses_fullwidth_punctuation(path: Path) -> None:
    src = path.read_text(encoding="utf-8")
    strings = html_strings(src) if path.suffix == ".html" else js_strings(src)
    found = [
        (ln, t) for ln, t in mixed_punctuation(strings)
        if f"{path.name}:{t}" not in _ALLOWED
    ]
    assert not found, (
        f"web/{path.name} renders zh copy with half-width punctuation next to CJK text: "
        + "; ".join(f"line {ln}: {t[:60]!r}" for ln, t in found)
        + " — use ，：；！？（）(L5)."
    )


def test_the_allowlist_is_not_stale() -> None:
    """An allowlist nobody uses is an allowlist nobody notices (D39, applied here)."""
    for entry in sorted(_ALLOWED):
        name, _, text = entry.partition(":")
        path = _WEB_DIR / name
        assert path.exists(), f"_ALLOWED names web/{name}, which does not exist"
        assert text in path.read_text(encoding="utf-8"), (
            f"_ALLOWED entry no longer appears in web/{name} — delete it: {text!r}"
        )


# --- the backend surface (Q1 batch 1, owner ruling 2026-09-17) ---------------------------

_PKG_DIR = Path(__file__).resolve().parents[2] / "portfolio_dash"

#: Files whose strings are NOT user copy and are excluded as a whole, with the reason.
#:   official_templates.py / variables.py — LLM prompt text; the model reads it, no one
#:   else does, and `RSI(14)` there is notation, not punctuation.
_NOT_USER_COPY: frozenset[str] = frozenset({
    "llm_insight/official_templates.py",
    "llm_insight/variables.py",
})

#: Files still carrying the class, deferred to a LATER batch by owner ruling (2026-09-17:
#: batch 1 = the 57 user-facing messages; batch 2 = the what's-new catalog, 29 strings).
#: Same contract as `_PENDING` in test_account_name_single_source.py: an entry must still
#: violate, and fixing the file means deleting the entry.
_BACKEND_PENDING: frozenset[str] = frozenset()  # batch 2 (whatsnew.py) landed 2026-09-17

#: Individual strings that legitimately mix, keyed "relative/path.py:exact substring".
#: Every entry names its reason; an entry nobody can justify gets deleted.
_BACKEND_ALLOWED: dict[str, str] = {
    # regexes that deliberately accept BOTH widths — the half-width half is the point
    "data_ingestion/csv_import.py:[(（][^)）]*[)）]": "header-annotation regex",
    "data_ingestion/dateparse.py:年(\\d{1,2})月(\\d{1,2})日$": "zh date regex",
    "llm_insight/figure_check.py:[-/年]": "date-token regex",
    "llm_insight/figure_check.py:[(（]\\s*(\\d{4,6}": "parenthesised-code regex",
    # an English scheduler job description that names a zh page in passing
    "scheduler/jobs.py:(feeds 待確認匯入)": "English job description",
    # fee-rule formula notation: `ceil(金額/1,000)` is a function call, not prose
    "api/wire.py:印花 ceil(金額/": "formula notation",
    # --- admitted when the detector learned to see across a space (L5-b, 2026-09-22) ---
    # a URL scheme written as notation: `http(s)` is one token, not a parenthesis in prose
    "api/routers/notify.py:http(s) ": "URL-scheme notation",
    # the message LISTS the forbidden characters; this `:` is the character itself
    "api/routers/notify.py:不可含 / @ : 或空白": "names the forbidden characters",
    # a line of the master-calibration PROMPT; the model reads it, no page renders it
    "llm_insight/master.py:: 校準誤差": "LLM prompt text",
}


def py_strings(src: str) -> list[tuple[int, str]]:
    """Every string constant in a module, docstrings excluded (they are not rendered).

    f-strings contribute their literal parts, so `f"{label}過大,無法處理"` is scanned as
    `過大,無法處理` — the part the user actually reads.
    """
    tree = ast.parse(src)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings):
            out.append((node.lineno, node.value))
    return out


def _backend_files() -> list[Path]:
    return sorted(_PKG_DIR.glob("**/*.py"))


def _rel(path: Path) -> str:
    return path.relative_to(_PKG_DIR).as_posix()


def _backend_hits(path: Path) -> list[tuple[int, str]]:
    src = path.read_text(encoding="utf-8")
    rel = _rel(path)
    hits = []
    for ln, text in mixed_punctuation(py_strings(src)):
        if any(k.startswith(rel + ":") and k[len(rel) + 1:] in text for k in _BACKEND_ALLOWED):
            continue
        hits.append((ln, text))
    return hits


def test_backend_scanner_reads_literals_and_fstring_parts_not_docstrings() -> None:
    src = (
        '"""模組說明,不算"""\n'
        "def f(label: str) -> str:\n"
        '    """函式說明:也不算"""\n'
        '    return f"{label}過大,無法處理"\n'
        'MSG = "現金將不足(可能漏登入金),確認要寫入?"\n'
    )
    found = sorted(mixed_punctuation(py_strings(src)))  # ast.walk is breadth-first
    assert found == [(4, "過大,無法處理"), (5, "現金將不足(可能漏登入金),確認要寫入?")]


@pytest.mark.parametrize("path", _backend_files(), ids=_rel)
def test_backend_user_copy_uses_fullwidth_punctuation(path: Path) -> None:
    rel = _rel(path)
    if rel in _NOT_USER_COPY or rel in _BACKEND_PENDING:
        pytest.skip(f"{rel}: excluded / deferred by name (see the two sets above)")
    found = _backend_hits(path)
    assert not found, (
        f"portfolio_dash/{rel} builds zh copy with half-width punctuation next to CJK text: "
        + "; ".join(f"line {ln}: {t[:60]!r}" for ln, t in found)
        + " — use ，：；！？（）(L5, Q1 batch 1)."
    )


def test_backend_pending_and_allowed_entries_are_not_stale() -> None:
    """D39 again: a deferral that no longer violates, or an allowance whose string is gone,
    must be deleted rather than left as cover."""
    for rel in sorted(_BACKEND_PENDING):
        path = _PKG_DIR / rel
        assert path.exists(), f"_BACKEND_PENDING names {rel}, which does not exist"
        assert _backend_hits(path), f"{rel} is clean now — remove it from _BACKEND_PENDING"
    for key, reason in _BACKEND_ALLOWED.items():
        rel, _, text = key.partition(":")
        path = _PKG_DIR / rel
        assert reason, key
        assert path.exists(), f"_BACKEND_ALLOWED names {rel}, which does not exist"
        assert any(text in t for _, t in py_strings(path.read_text(encoding="utf-8"))), (
            f"_BACKEND_ALLOWED entry no longer appears in {rel} — delete it: {text!r}"
        )
