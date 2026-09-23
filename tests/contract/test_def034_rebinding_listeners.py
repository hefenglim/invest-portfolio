"""DEF-034 (2026-09-23): a re-runnable boot()/render() must never re-bind a page element.

系統設定 › AI 與額度: ``boot()`` (settings-llm.js) ended with ``initAutoAiToggle()``, whose
``t.addEventListener('click', …)`` bound 「代號查無時自動 AI 辨識」 — an element that lives in
settings.html and is never re-created. ``boot()`` is the page's RELOAD: saving the 低額度警示
threshold, toggling / deleting / saving a model, changing a role and topping up all end in
``await boot()``. So every save stacked one more listener, and after two threshold saves a
single click on the switch sent THREE ``PUT /api/ui-prefs`` and three toasts (and, with an
odd count, flipped the value back and forth to a net no-op).

Two guards:

* settings-llm.js specifically — ``boot()`` and every same-file function it reaches may not
  call ``addEventListener`` on a page element (a node the function did not create);
* the class, across web/*.js — a function with two or more call sites (or reached from one)
  that binds a listener to a persistent element must carry a visible guard (a ``*Wired`` /
  ``*Bound`` flag, ``dataset.bound``, ``{ once: true }``, an ``AbortController`` or a matching
  ``removeEventListener``). The 2026-09-23 census: 7 such functions, 6 guarded, 1 not — this
  one.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

_WEB = Path(__file__).resolve().parents[2] / "web"
_NL = "\n"

_FUNC = re.compile(
    r"(?:async\s+)?function\s+(\w+)\s*\([^)]*\)\s*\{"
    r"|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>\s*\{"
)
#: A local bound to an element that already exists in the page.
_PERSIST_VAR = re.compile(
    r"(?:const|let|var)\s+(\w+)\s*=\s*(?:\$\(\s*['\"]|document\.getElementById\(|"
    r"document\.querySelector\()"
)
#: A listener bound straight onto a page lookup, or onto window / document.
_DIRECT = re.compile(
    r"(?:\$\([^)]*\)|document\.getElementById\([^)]*\)|document\.querySelector\([^)]*\)"
    r"|\bwindow|\bdocument)\.addEventListener\("
)
_GUARD = re.compile(
    r"dataset\.bound|once:\s*true|AbortController|removeEventListener|\bbound\b|_bound"
    r"|\w+(?:Wired|Bound)\b|_endArrival\("
)


def _blank(m: re.Match[str]) -> str:
    return "".join(_NL if ch == _NL else " " for ch in m.group(0))


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", _blank, src, flags=re.S)
    return re.sub(r"(?<![:'\"\\])//[^\n]*", _blank, src)


def _body_end(src: str, open_idx: int) -> int:
    depth, i, quote = 0, open_idx, ""
    while i < len(src):
        c = src[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = ""
        elif c in "'\"`":
            quote = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(src)


def _functions(src: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _FUNC.finditer(src):
        name = m.group(1) or m.group(2)
        start = m.end() - 1
        out.setdefault(name, src[start:_body_end(src, start) + 1])
    return out


def _persistent_binds(body: str) -> list[str]:
    hits = [f"{v}.addEventListener" for v in set(_PERSIST_VAR.findall(body))
            if re.search(r"(?<![\w.])" + re.escape(v) + r"\.addEventListener\(", body)]
    return hits + [m.group(0) for m in _DIRECT.finditer(body)]


_CALL = re.compile(r"(?<![\w.$])(\w+)\s*\(")
_DEF = re.compile(r"function\s+(\w+)\s*\(")


def _calls(body: str, names: set[str]) -> set[str]:
    return set(_CALL.findall(body)) & names


def _reentrant(src: str, funcs: dict[str, str]) -> set[str]:
    names = set(funcs)
    counts = Counter(_CALL.findall(src))
    counts.subtract(Counter(_DEF.findall(src)))
    out = {n for n in names if counts[n] >= 2}
    grew = True
    while grew:
        grew = False
        for outer in list(out):
            for inner in _calls(funcs[outer], names) - out - {outer}:
                out.add(inner)
                grew = True
    return out


def _unguarded(src: str) -> list[str]:
    src = _strip_comments(src)
    funcs = _functions(src)
    return sorted(n for n in _reentrant(src, funcs)
                  if _persistent_binds(funcs[n]) and not _GUARD.search(funcs[n]))


def test_the_detector_sees_the_measured_shape() -> None:
    """Positive control: the DEF-034 code, verbatim in shape, must be flagged."""
    measured = """
      async function boot() { await load(); initAutoAiToggle(); }
      function initAutoAiToggle() {
        const t = $('#pref-auto-ai');
        t.addEventListener('click', async () => { await save(); });
      }
      boot();
      async function save() { await put(); await boot(); }
    """
    assert _unguarded(measured) == ["initAutoAiToggle"]
    fixed = measured.replace("const t = $('#pref-auto-ai');",
                             "const t = $('#pref-auto-ai'); if (t.dataset.bound) return; "
                             "t.dataset.bound = '1';")
    assert _unguarded(fixed) == []


def test_settings_llm_boot_binds_no_page_element() -> None:
    """boot() is the reload path — it and everything it reaches may only bind nodes it built."""
    src = _strip_comments((_WEB / "settings-llm.js").read_text(encoding="utf-8"))
    funcs = _functions(src)
    assert "boot" in funcs
    seen, todo, offenders = set(), ["boot"], {}
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        found = _persistent_binds(funcs[name])
        if found:
            offenders[name] = found
        todo.extend(_calls(funcs[name], set(funcs)) - seen)
    assert "renderQuota" in seen and "renderModels" in seen, seen   # guard the walk itself
    assert not offenders, (
        f"boot() re-binds page elements on every reload: {offenders} — bind once at page "
        f"init and keep boot() to data")


def test_no_reentrant_function_rebinds_a_page_element_without_a_guard() -> None:
    offenders = {p.name: bad for p in sorted(_WEB.glob("*.js"))
                 if p.name != "echarts.min.js"
                 and (bad := _unguarded(p.read_text(encoding="utf-8")))}
    assert not offenders, (
        f"re-runnable functions that stack listeners on page elements: {offenders}")
