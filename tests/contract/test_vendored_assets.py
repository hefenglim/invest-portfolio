"""Third-party front-end assets are VENDORED, and stay vendored (owner ruling 2026-08-12).

ECharts used to load from ``cdn.jsdelivr.net``. A CDN outage therefore took the dashboard's
charts down — and worse than blank: ``web/charts.js`` threw before it wired the trend-mode
controls, leaving dead buttons with no cue. The library now ships in the repo as
``web/echarts.min.js`` and is served by the app itself.

**The webfont deliberately stays remote.** ``--font-ui`` / ``--font-num`` end in a generic
family, 9 of 18 pages never request the font at all, and the e2e suite already runs the whole
app against an EMPTY Google Fonts stylesheet. A font outage is cosmetic and continuously
tested; a chart-library outage was neither.

Three guards, all source scans (no DB, no network, no browser):

* ``test_no_page_loads_a_remote_script`` — the three ``<head>`` tags.
* ``test_js_never_injects_a_remote_script`` — the seam a grep of the HTML cannot see:
  ``shell.js::pdEnsureDrawer`` injected a **fourth** copy at runtime, on every page, and
  ``detail.js`` injects ``pager.js`` the same way.
* ``test_vendored_echarts_is_the_pinned_artifact`` — the bytes are the bytes.

The pin below is the ONE machine-readable copy: ``scripts/vendor_echarts.py`` imports it
rather than restating it, so the downloader and the guard can never disagree. Provenance and
the upgrade procedure live in ``docs/reference/vendored-assets.md``.

Note on scope: these are cheap tripwires, not the coverage. A source scan stays green when a
render branch is un-mounted while every string stays in the file (measured on this repo,
2026-08-12). The behavioural coverage is ``tests/e2e/test_echarts_selfhosted.py``.
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
from pathlib import Path

# tests/contract/this_file.py -> parents[2] == worktree root (web/ lives here).
_WORKTREE_ROOT = Path(__file__).resolve().parents[2]
_WEB_DIR = _WORKTREE_ROOT / "web"

# --- the pin (single source of truth; scripts/vendor_echarts.py imports these) ------------
ECHARTS_VERSION = "5.5.0"
ECHARTS_FILENAME = "echarts.min.js"
ECHARTS_SIZE = 1_029_203
ECHARTS_SHA256 = "42f8329d989b6f6539dd2b15bbdf0d82025762ac112fbb60dc57b27d7bcf3946"
# Two independent mirrors of the SAME npm tarball. vendor_echarts.py requires both to answer
# byte-identically AND to match the hash above before it writes anything, so a single
# compromised or truncated mirror cannot land in the tree.
ECHARTS_SOURCES = (
    f"https://cdn.jsdelivr.net/npm/echarts@{ECHARTS_VERSION}/dist/{ECHARTS_FILENAME}",
    f"https://unpkg.com/echarts@{ECHARTS_VERSION}/dist/{ECHARTS_FILENAME}",
)
ECHARTS_PATH = _WEB_DIR / ECHARTS_FILENAME

# A LOCAL asset reference: a BARE relative filename. No scheme (``https:``), no protocol-
# relative ``//host``, no path separator — the same shape scripts/stamp_asset_version.py and
# tests/contract/test_static_cache_discipline.py already require, which is precisely why the
# vendored file sits flat in web/ and not in web/vendor/: a nested path is invisible to the
# ?v= stamper and to its guard, so it would silently drop out of the cache discipline.
_BARE_LOCAL_JS = re.compile(r"^[A-Za-z0-9._-]+\.js(\?[^\"']*)?$")
_BARE_LOCAL_CSS = re.compile(r"^[A-Za-z0-9._-]+\.css(\?[^\"']*)?$")

_HTML_SCRIPT_SRC = re.compile(
    r"<script\b[^>]*?\bsrc\s*=\s*(?P<q>[\"'])(?P<src>.*?)(?P=q)", re.IGNORECASE | re.DOTALL
)


# --- T1 -----------------------------------------------------------------------------------
def test_no_page_loads_a_remote_script() -> None:
    """Every ``<script src>`` in web/*.html is a bare relative filename.

    NO host allow-list, deliberately. An allow-list is where a carve-out leaks: today it
    would say "jsdelivr is fine for ECharts", and tomorrow it is fine for something else.
    The webfont needs no carve-out here because it is a ``<link>``, not a ``<script>``.
    """
    offenders: list[str] = []
    seen = 0
    for html in sorted(_WEB_DIR.glob("*.html")):
        for m in _HTML_SCRIPT_SRC.finditer(html.read_text(encoding="utf-8")):
            seen += 1
            src = m.group("src")
            if not _BARE_LOCAL_JS.match(src):
                offenders.append(f"{html.name} -> {src}")
    assert seen > 0, "expected <script src> tags to scan (web/*.html)"
    assert not offenders, (
        "page(s) load a script that is not a bare local filename — vendor it into web/ "
        f"(see docs/reference/vendored-assets.md): {offenders}"
    )


# --- comment blanking (shared by T2 and its stylesheet twin) ------------------------------
def _is_division_context(prev: str) -> bool:
    """True when a ``/`` following ``prev`` is division, not the start of a regex literal."""
    return prev.isalnum() or prev in "_$)]}'\"`"


def _blank_comments(js: str) -> str:
    """Replace comment bodies with spaces, preserving offsets, strings and regex literals.

    Written as a scanner rather than a regex because both naive directions are wrong here:
    a regex that strips ``//...`` mangles the ``https://`` inside a perfectly legal string
    literal (``settings-llm.js``'s provider API base, ``settings-notify.js``'s ntfy default),
    and a regex that skips strings still trips over regex literals containing quotes.
    Offsets are preserved so a reported line number still points at the real line.
    """
    out = list(js)
    i, n = 0, len(js)
    prev = ""
    while i < n:
        ch = js[i]
        if ch in "'\"`":
            quote = ch
            i += 1
            while i < n:
                if js[i] == "\\":
                    i += 2
                    continue
                if js[i] == quote:
                    i += 1
                    break
                i += 1
            prev = quote
            continue
        if ch == "/" and i + 1 < n:
            nxt = js[i + 1]
            if nxt == "/":
                while i < n and js[i] != "\n":
                    out[i] = " "
                    i += 1
                continue
            if nxt == "*":
                while i < n and not (js[i] == "*" and i + 1 < n and js[i + 1] == "/"):
                    if js[i] != "\n":
                        out[i] = " "
                    i += 1
                for j in range(i, min(i + 2, n)):
                    out[j] = " "
                i += 2
                continue
            if not _is_division_context(prev):  # regex literal — skip it whole
                i += 1
                in_class = False
                while i < n:
                    c = js[i]
                    if c == "\\":
                        i += 2
                        continue
                    if c == "\n":
                        break
                    if c == "[":
                        in_class = True
                    elif c == "]":
                        in_class = False
                    elif c == "/" and not in_class:
                        i += 1
                        break
                    i += 1
                prev = "/"
                continue
        if not ch.isspace():
            prev = ch
        i += 1
    return "".join(out)


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


# ``function pdLoadScript(src)`` is the DEFINITION, whose parameter is an identifier by
# construction; the ``def`` group lets the scan skip it without a variable-length lookbehind.
_LOADER_CALL = re.compile(r"(?P<def>\bfunction\s+)?\bpdLoadScript\s*\(\s*")
_CSS_LOADER_CALL = re.compile(r"(?P<def>\bfunction\s+)?\bpdLoadCss\s*\(\s*")
_SRC_ASSIGN = re.compile(r"\.src\s*=\s*(?P<q>[\"'])(?P<lit>.*?)(?P=q)")


def _literal_at(text: str, pos: int) -> str | None:
    """The string literal starting at ``pos``, or None when the argument is not a literal.

    A template literal (backtick) counts as "not a literal": it can interpolate, so its value
    is not knowable from source — exactly the hole ``pdLoadScript(echartsCdn)`` sat in.
    """
    if pos >= len(text) or text[pos] not in "'\"":
        return None
    quote = text[pos]
    end = text.find(quote, pos + 1)
    return None if end < 0 else text[pos + 1 : end]


# --- T2 -----------------------------------------------------------------------------------
def test_js_never_injects_a_remote_script() -> None:
    """No JS seam can inject a script from another host.

    This is the guard for the load site an HTML grep cannot find. Before 2026-08-12
    ``shell.js`` held ``const echartsCdn = 'https://cdn.jsdelivr.net/...'`` and passed the
    IDENTIFIER to ``pdLoadScript`` — a fourth copy of the dependency, live on every page via
    ``pdEnsureDrawer``, that three ``<script>`` tags in the markup completely masked.

    Two rules, because one alone is bypassable: every ``pdLoadScript(`` argument must be a
    string LITERAL (so its value is knowable from source at all), and every literal reaching
    a loader or a ``.src =`` must be a bare local filename.
    """
    offenders: list[str] = []
    call_sites = 0
    for js in sorted(_WEB_DIR.glob("*.js")):
        raw = js.read_text(encoding="utf-8")
        code = _blank_comments(raw)
        for m in _LOADER_CALL.finditer(code):
            if m.group("def"):
                continue  # the definition itself, not a call
            call_sites += 1
            where = f"{js.name}:{_line_of(code, m.start())}"
            lit = _literal_at(code, m.end())
            if lit is None:
                offenders.append(f"{where} pdLoadScript(...) argument is not a string literal")
            elif not _BARE_LOCAL_JS.match(lit):
                offenders.append(f"{where} pdLoadScript({lit!r}) is not a bare local filename")
        for m in _SRC_ASSIGN.finditer(code):
            lit = m.group("lit")
            if not _BARE_LOCAL_JS.match(lit):
                offenders.append(
                    f"{js.name}:{_line_of(code, m.start())} .src = {lit!r} "
                    "is not a bare local filename"
                )
    assert call_sites > 0, "expected pdLoadScript(...) call sites to scan (web/*.js)"
    assert not offenders, (
        "JS would inject a non-local script — vendor it into web/ instead "
        f"(see docs/reference/vendored-assets.md): {offenders}"
    )


def test_js_never_injects_a_remote_stylesheet() -> None:
    """The same rule for ``pdLoadCss`` — the other runtime asset-injection seam.

    Not required by the ruling (only ECharts was vendored), but it is the identical hole one
    function lower in the same file, and leaving it open would mean the next remote asset
    enters through the door we just finished proving is dangerous.
    """
    offenders: list[str] = []
    for js in sorted(_WEB_DIR.glob("*.js")):
        code = _blank_comments(js.read_text(encoding="utf-8"))
        for m in _CSS_LOADER_CALL.finditer(code):
            if m.group("def"):
                continue
            lit = _literal_at(code, m.end())
            where = f"{js.name}:{_line_of(code, m.start())}"
            if lit is None:
                offenders.append(f"{where} pdLoadCss(...) argument is not a string literal")
            elif not _BARE_LOCAL_CSS.match(lit):
                offenders.append(f"{where} pdLoadCss({lit!r}) is not a bare local filename")
    assert not offenders, f"JS would inject a non-local stylesheet: {offenders}"


# --- T3 -----------------------------------------------------------------------------------
def test_vendored_echarts_is_the_pinned_artifact() -> None:
    """web/echarts.min.js is byte-for-byte the pinned upstream build.

    Size AND digest AND two content probes: a digest alone would still pass on a file that is
    the right bytes of the WRONG library, and the two probes cost nothing. Re-fetch with
    ``.venv/Scripts/python scripts/vendor_echarts.py`` — never hand-edit the artifact, and
    never "fix" a failure by editing the constants above (that only tests the test).
    """
    assert ECHARTS_PATH.is_file(), (
        f"{ECHARTS_PATH.name} is missing — run scripts/vendor_echarts.py "
        "(the charts have no CDN fallback by design)"
    )
    raw = ECHARTS_PATH.read_bytes()
    assert len(raw) == ECHARTS_SIZE, f"size {len(raw)} != pinned {ECHARTS_SIZE}"
    digest = hashlib.sha256(raw).hexdigest()
    assert digest == ECHARTS_SHA256, f"sha256 {digest} != pinned {ECHARTS_SHA256}"
    assert f'version:"{ECHARTS_VERSION}"'.encode() in raw, "not the pinned ECharts version"
    assert b"Licensed to the Apache Software Foundation" in raw[:1000], (
        "missing the Apache-2.0 banner — is this the upstream dist build?"
    )


def test_vendored_asset_is_exempt_from_eol_conversion() -> None:
    """`.gitattributes` marks the vendored bundle ``-text``, or the pin breaks on Windows.

    This repo is developed with ``core.autocrlf=true``. A minified bundle contains no NUL
    bytes, so git's heuristic calls it text and rewrites LF -> CRLF on CHECKOUT. Measured
    2026-08-12 before the fix: ``web/echarts.min.js`` checked out as 1,029,248 bytes with 45
    conversions and a different digest — on a file nobody had edited.

    Without this test, that regression surfaces as ``test_vendored_echarts_is_the_pinned
    _artifact`` failing with "size mismatch, re-run scripts/vendor_echarts.py" — advice that
    leads straight into a loop, because re-running writes correct LF bytes that git converts
    again on the next checkout. Same defect, precise message.
    """
    attrs = _WORKTREE_ROOT / ".gitattributes"
    assert attrs.is_file(), ".gitattributes is missing — the vendored bundle needs `-text`"
    covered = False
    for line in attrs.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        pattern, *rest = stripped.split()
        if "-text" in rest and (
            fnmatch.fnmatch(ECHARTS_FILENAME, pattern)
            or fnmatch.fnmatch(f"web/{ECHARTS_FILENAME}", pattern)
        ):
            covered = True
            break
    assert covered, (
        f"no `-text` rule in .gitattributes covers {ECHARTS_FILENAME} — a Windows checkout "
        "will rewrite its line endings and break the pinned sha256"
    )
