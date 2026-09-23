"""DEF-019 (functional test manual C-04b / C-05, 2026-09-23): one refresh after every ledger change.

Measured: 最近匯入 › 復原 removed a 0.028-share DRIP and the 交易輸入 picker kept annotating
the symbol 「85.067255 股 均價 158.87」 — the API already said 85.039255 / 158.9266 — until a
page reload. The undo (broker-import.js) refreshed its batch list and the ledger tables; the
per-account holdings cache that the pickers and sell hints read lives in input.js and was
dropped only by input.js's OWN commits. Each write path had grown its own list of things to
re-fetch, and the lists drifted.

The fix is one function, ``refreshAfterLedgerChange`` in web/input.js, and this file pins it
the three ways it can be bypassed again:

1. **in input.js** — every function that posts to a ledger-writing endpoint must REACH it
   through the file's own call graph (a new commit path that toasts and forgets fails here);
   and it is the ONLY place that drops the holdings cache or calls the ledger table refresh;
2. **the seam** — input.js adopts ``window.pdLedgerRefresh`` (the global the other modules
   already call after a write) and re-points it at the single refresh;
3. **across files** — every other script on trades.html that writes the ledger must call that
   seam afterwards. ``_PENDING`` holds the one known exception outside this change's scope.
"""

from __future__ import annotations

import re
from pathlib import Path

_WEB = Path(__file__).resolve().parents[2] / "web"
_INPUT = _WEB / "input.js"

#: Endpoints whose success changes the ledger (and therefore every holdings figure).
_LEDGER_WRITE = re.compile(
    r"""\.(?:post|put|del)\(\s*['"`]/api/(?:import/commit|import/batches|input/manual/commit"""
    r"""|ledgers/|cash/movements|cash/fx|dividends/inbox|rebates)""")

#: Scripts on trades.html that write the ledger WITHOUT reaching the seam, with the reason.
#: An entry that stops being true gets deleted (test_the_pending_list_is_not_stale).
_PENDING: dict[str, str] = {}


def _strip(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", lambda m: " " * len(m.group(0)), src, flags=re.S)
    return re.sub(r"(?m)(?<![:'\"])//[^\n]*$", "", src)


_REGEX_PREV = set("(,=:[!&|?{};+-*%<>~^")
_NL = chr(10)


def _skip_literal(src: str, i: int) -> int:
    """Index just past the string / template / regex literal starting at *i*, else *i*.

    Quotes inside a regex literal (``/[",\n]/``) would otherwise open a phantom string and
    desynchronise every brace after it. A quoted string abandons at a newline, like the
    punctuation guard's scanner, so one odd literal cannot eat the rest of the file."""
    c = src[i]
    if c in "'\"`":
        j = i + 1
        while j < len(src) and src[j] != c:
            if c != "`" and src[j] == _NL:
                return j
            j += 2 if src[j] == "\\" else 1
        return j + 1
    if c == "/":
        k = i - 1
        while k >= 0 and src[k].isspace():
            k -= 1
        prev = src[k] if k >= 0 else "("
        if prev in _REGEX_PREV or src[max(0, k - 5):k + 1].endswith("return"):
            j, in_class = i + 1, False
            while j < len(src) and src[j] != _NL:
                ch = src[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == "[":
                    in_class = True
                elif ch == "]":
                    in_class = False
                elif ch == "/" and not in_class:
                    return j + 1
                j += 1
    return i


def _body(src: str, open_at: int) -> str:
    depth = 0
    i = open_at
    while i < len(src):
        c = src[i]
        nxt = _skip_literal(src, i)
        if nxt != i:
            i = nxt
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[open_at:i + 1]
        i += 1
    raise AssertionError("unbalanced function body")


def _functions(src: str) -> dict[str, str]:
    """Every named ``function f(...) {…}`` in *src*, with its body (nested bodies included)."""
    out: dict[str, str] = {}
    for m in re.finditer(r"(?:async\s+)?function\s+(\w+)\s*\([^)]*\)\s*\{", src):
        out.setdefault(m.group(1), _body(src, m.end() - 1))
    return out


def _reaches(fns: dict[str, str], start: str, target: str) -> bool:
    seen: set[str] = set()
    todo = [start]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        body = fns.get(name, "")
        if re.search(r"\b" + target + r"\(", body):
            return True
        todo.extend(n for n in fns if n != name and re.search(r"\b" + n + r"\(", body))
    return False


def test_input_js_has_one_refresh_and_the_old_name_is_gone() -> None:
    src = _strip(_INPUT.read_text(encoding="utf-8"))
    fns = _functions(src)
    assert "refreshAfterLedgerChange" in fns
    assert "afterCommitRefresh" not in src


def test_every_ledger_write_in_input_js_reaches_the_single_refresh() -> None:
    src = _strip(_INPUT.read_text(encoding="utf-8"))
    fns = _functions(src)
    writers = sorted(n for n, b in fns.items()
                     if _LEDGER_WRITE.search(b) and n != "refreshAfterLedgerChange")
    # The write paths known today — a positive control, so an empty scan cannot pass.
    assert {"commitManual", "oversellDialog", "commitCsv", "runAiCommit",
            "commitOneRow"} <= set(writers), writers
    stranded = [n for n in writers if not _reaches(fns, n, "refreshAfterLedgerChange")]
    assert not stranded, (
        f"ledger-writing path(s) {stranded} never reach refreshAfterLedgerChange — after the "
        "write the pickers / sell hints read a stale holdings cache (DEF-019)")


def test_only_the_single_refresh_touches_the_caches_it_owns() -> None:
    """A second place dropping the cache or refreshing the tables is how the lists drifted."""
    src = _strip(_INPUT.read_text(encoding="utf-8"))
    fns = _functions(src)
    owners = {n for n, b in fns.items()
              if re.search(r"delete acctHoldingsCache\[", b) or re.search(r"\bledgerTables\(", b)}
    assert owners == {"refreshAfterLedgerChange"}, owners


def test_input_js_adopts_the_page_seam() -> None:
    src = _strip(_INPUT.read_text(encoding="utf-8"))
    fns = _functions(src)
    adopt = fns.get("adoptLedgerSeam", "")
    assert re.search(r"ledgerTables\s*=\s*own", adopt), "the ledger.js refresh is not kept"
    assert re.search(r"window\.pdLedgerRefresh\s*=\s*\(kind\)\s*=>\s*\n?\s*"
                     r"refreshAfterLedgerChange\(", adopt), (
        "window.pdLedgerRefresh is not re-pointed at refreshAfterLedgerChange")
    assert "adoptLedgerSeam();" in fns["refreshAfterLedgerChange"]
    # the only assignment to the global in this file is the adoption itself
    assert len(re.findall(r"window\.pdLedgerRefresh\s*=(?!=)", src)) == 1


def _trades_scripts() -> list[str]:
    html = (_WEB / "trades.html").read_text(encoding="utf-8")
    return [m.split("?")[0] for m in re.findall(r'<script src="([^"]+)"', html)]


def test_every_other_ledger_writer_on_the_page_calls_the_seam() -> None:
    scripts = _trades_scripts()
    assert "input.js" in scripts and "ledger.js" in scripts
    # input.js must load BEFORE the modules it serves are used, and ledger.js (which defines
    # the seam input.js adopts) after it — adoption happens on DOMContentLoaded or first use.
    offenders: dict[str, str] = {}
    writers: list[str] = []
    for name in scripts:
        if name == "input.js":
            continue
        path = _WEB / name
        if not path.exists():
            continue
        src = _strip(path.read_text(encoding="utf-8"))
        if not _LEDGER_WRITE.search(src):
            continue
        writers.append(name)
        if name in _PENDING:
            continue
        if not re.search(r"window\.pdLedgerRefresh\(", src):
            offenders[name] = "writes the ledger and never calls window.pdLedgerRefresh"
    assert {"broker-import.js", "corp-action-form.js", "ledger.js"} <= set(writers), writers
    assert not offenders, offenders


def test_the_pending_list_is_not_stale() -> None:
    for name in _PENDING:
        src = _strip((_WEB / name).read_text(encoding="utf-8"))
        assert _LEDGER_WRITE.search(src), f"{name} no longer writes the ledger — drop it"
        assert not re.search(r"window\.pdLedgerRefresh\(", src), (
            f"{name} now calls the seam — remove it from _PENDING so it is guarded")


def test_the_reachability_check_can_fail() -> None:
    """Positive control: a writer that only toasts is reported as stranded."""
    src = ("function commitX() { api.post('/api/import/commit', {}); toast('ok'); }\n"
           "function commitY() { api.post('/api/import/commit', {}); done(); }\n"
           "function done() { refreshAfterLedgerChange('x'); }\n"
           "async function refreshAfterLedgerChange(k) { }\n")
    fns = _functions(src)
    assert not _reaches(fns, "commitX", "refreshAfterLedgerChange")
    assert _reaches(fns, "commitY", "refreshAfterLedgerChange")


def test_ledger_js_settles_every_edit_and_delete_through_the_seam() -> None:
    """I-7: ledger.js left _PENDING. Presence of the call is not enough (the cross-file test
    above only greps for it) — the function every edit/delete settles through must REACH it,
    and boot() may remain only as the fallback for a page with no seam at all."""
    src = _strip((_WEB / "ledger.js").read_text(encoding="utf-8"))
    fns = _functions(src)
    assert _reaches(fns, "mutationOk", r"window\.pdLedgerRefresh")
    assert not re.search(r"\bboot\(", fns["mutationOk"]), "mutationOk calls boot() directly"
    for settle in ("runMutation", "saveFromModal"):
        assert re.search(r"\bmutationOk\(", fns[settle]), settle
