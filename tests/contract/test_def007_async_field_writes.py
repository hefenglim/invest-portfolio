"""DEF-007 (functional test manual A-02, 2026-09-23): a late response never overwrites typing.

The verifier's sequence on 出金入金: 嘉信 Schwab → USD → 2026-09-23 → 1,000 → 取得成本「匯率」
31.5, typed BEFORE ``GET /api/cash/acq-rate`` answered (live latency 5–30 s). The answer then
landed on top: the field became 31.698999, the mode was forced back to 匯率, and 確認 booked
``acq_home_amount`` 31,699 for a conversion dealt at 31,500. The hint also read 「參考值：
2026-09-23 收盤」 for a rate the API dated 2026-09-22.

``prefillAcq`` fenced its response against LATER PREFILLS (a sequence token) and against
nothing else. The fix is one guard, ``writeIfUntouched`` — a late write may replace only what
the page knows is in the field (nothing, or its own last auto-fill) — and this file is the
class for ``web/cash.js``: every ``.value =`` that runs after an ``await`` (or inside a
``.then(`` callback) must go through it. The scan is by function body, so a write in a
synchronous helper CALLED after an await is out of its sight; in this file that is
``clearAutoEstimate``, which now writes through ``autoFill`` as well (asserted below by
name). The other files' hits are reported, not fixed, here (see the handback's 跨包命中).

I-12 (2026-09-23): the class, not the instance. The guard moved to ``web/format.js``
(``window.pdField`` — every page with an editable field loads format.js) and the scan below
covers EVERY ``web/*.js``. Measured before the fix: 39 raw late writes in 12 files. Two shapes
are exempt BY STRUCTURE rather than by list — a node the page creates (``el(…)`` /
``document.createElement(…)``) and fills before any ``await`` (nobody can have typed into it
yet), and a write inside a synchronous ``addEventListener`` callback (it runs on the owner's
own action, not on a late response) — which left 18. 11 were routed through the guard
(settings-prefs ×3, export ×1, input ×1, settings-users ×3, settings-prompts ×3) and 7 carry
their OWN equivalent guard — a dirty / override / pristine flag the field's input handler
sets — and are listed in ``_OWN_GUARD`` with that reason.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

_WEB = Path(__file__).resolve().parents[2] / "web"
_CASH = _WEB / "cash.js"
_GUARD = _WEB / "format.js"   # window.pdField — the ONE late-write guard (I-12)

_QUOTES = "'\"`"
_WRITE = re.compile(r"\.value\s*=(?!=)")


def _blank(src: str) -> str:
    """Comments and string bodies blanked (newlines kept) — prose quotes the banned line."""
    out = list(src)
    i, n = 0, len(src)
    while i < n:
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            out[i:j] = " " * (j - i)
            i = j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out[i:j] = [c if c == "\n" else " " for c in src[i:j]]
            i = j
            continue
        c = src[i]
        if c in _QUOTES:
            j = i + 1
            while j < n and src[j] != c:
                if src[j] == "\\":
                    j += 2
                    continue
                if c != "`" and src[j] == "\n":
                    break
                j += 1
            out[i + 1:min(j, n)] = [ch if ch == "\n" else " " for ch in src[i + 1:min(j, n)]]
            i = j + 1
            continue
        i += 1
    return "".join(out)


def _match(s: str, i: int, op: str, cl: str) -> int:
    depth = 0
    for j in range(i, len(s)):
        if s[j] == op:
            depth += 1
        elif s[j] == cl:
            depth -= 1
            if depth == 0:
                return j
    return len(s)


def _async_bodies(s: str) -> list[tuple[int, int]]:
    """(start, end) of every async function / arrow / method body with a block."""
    out: list[tuple[int, int]] = []
    for m in re.finditer(r"\basync\b", s):
        k = m.end()
        while k < len(s) and s[k].isspace():
            k += 1
        if s.startswith("function", k):
            k = _match(s, s.find("(", k), "(", ")") + 1
        elif k < len(s) and s[k] == "(":
            k = _match(s, k, "(", ")") + 1
        else:
            ident = re.match(r"[A-Za-z_$][\w$]*\s*", s[k:])
            if ident:
                k += ident.end()
                if k < len(s) and s[k] == "(":
                    k = _match(s, k, "(", ")") + 1
        body = re.match(r"\s*(?:=>)?\s*\{", s[k:])
        if body:
            b = k + body.end() - 1
            out.append((b, _match(s, b, "{", "}")))
    return out


def late_writes(src: str, *, structural: bool = False) -> list[int]:
    """1-based lines of every raw ``.value =`` after an ``await`` or inside ``.then(``.

    ``structural=True`` drops the two shapes that are not late writes by construction (see
    the module docstring): a write to a node created earlier in the same stretch of code with
    no ``await`` in between, and a write inside a synchronous event-listener callback.
    """
    s = _blank(src)
    hits: set[int] = set()
    for b, e in _async_bodies(s):
        first = re.search(r"\bawait\b", s[b:e])
        if first:
            for w in _WRITE.finditer(s, b + first.end(), e):
                if not (structural and _exempt(s, w.start())):
                    hits.add(s.count("\n", 0, w.start()) + 1)
    for m in re.finditer(r"\.then\(", s):
        for w in _WRITE.finditer(s, m.end(), _match(s, m.end() - 1, "(", ")")):
            if not (structural and _exempt(s, w.start())):
                hits.add(s.count("\n", 0, w.start()) + 1)
    return sorted(hits)


_TARGET = re.compile(r"([A-Za-z_$][\w$]*)\s*$")
_LISTENER = re.compile(
    r"\.addEventListener\(\s*'[^']*'\s*,\s*(async\s+)?(?:\([^)]*\)|\w+)\s*=>\s*\{"
    r"|\.addEventListener\(\s*'[^']*'\s*,\s*(async\s+)?function\s*\([^)]*\)\s*\{")


def _exempt(s: str, at: int) -> bool:
    """A write at *at* that is not a late write by structure."""
    target = _TARGET.search(s[:at])
    if target:
        name = re.escape(target.group(1))
        created = None
        for c in re.finditer(r"\b(?:const|let|var)\s+" + name
                             + r"\s*=\s*(?:el|document\.createElement)\(", s[:at]):
            created = c
        if created is not None and not re.search(r"\bawait\b|\.then\(|addEventListener\(",
                                                  s[created.end():at]):
            return True     # a node the page just made — nobody can have typed into it yet
    for lis in _LISTENER.finditer(s, 0, at):
        if lis.group(1) or lis.group(2):
            continue        # an async listener is a late-write site of its own
        end = _match(s, lis.end() - 1, "{", "}")
        if lis.end() <= at <= end:
            return True     # runs on the owner's own action, synchronously
    return False


#: ``file: statement`` -> why the write is safe WITHOUT window.pdField: the field carries its
#: own dirty / override / pristine flag, set by its input handler, and the write is gated on
#: it — the same rule (never over a value the owner typed) through an older mechanism. A new
#: entry needs the same proof; a stale one fails ``test_the_own_guard_list_is_not_stale``.
_OWN_GUARD: dict[str, str] = {
    "input.js: if (!m.feeOverride) $('#m-fee').value = resp.fee !== undefined ? "
    "f.money(resp.fee, ccy) : '0';":
        "gated on m.feeOverride, set when the owner types a fee (the auto-fee preview)",
    "input.js: if (!m.taxOverride) $('#m-tax').value = resp.tax !== undefined ? "
    "f.money(resp.tax, ccy) : '0';":
        "gated on m.taxOverride, set when the owner types a tax",
    "inst-quickadd.js: if (namePristine) nameIn.value = '';":
        "gated on namePristine, cleared by the name field's input handler",
    "inst-quickadd.js: if (industryPristine) industryIn.value = '';":
        "gated on industryPristine, cleared by the industry field's input handler",
    "inst-quickadd.js: if (namePristine && r.name && !(wasAiResolve && nameIn.value)) "
    "nameIn.value = r.name;":
        "gated on namePristine (and never over an AI-resolved name)",
}


#: Real late writes found by the widened scan in a file outside this change's scope, named
#: so they cannot be forgotten. Can only shrink (``test_the_own_guard_list_is_not_stale``).
_PENDING: dict[str, str] = {}   # the last entry (同步官方) was fixed on 2026-09-24


def _web_late_writes() -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(_WEB.glob("*.js")):
        if path.name.endswith(".min.js"):
            continue
        src = path.read_text(encoding="utf-8")
        lines = src.splitlines()
        for ln in late_writes(src, structural=True):
            out[f"{path.name}: {lines[ln - 1].strip()}"] = f"line {ln}"
    return out


#: The pre-fix prefill, verbatim in shape — the detector must see all three late writes.
_PRE_FIX = """
async function prefillAcq() {
  const seq = ++acqPrefillSeq;
  try {
    const r = await api.get('/api/cash/acq-rate', { account_id: a, ccy: c, on: on });
    if (seq !== acqPrefillSeq) return;
    if (r.available) {
      $('#cm-acq-mode').value = 'rate';
      $('#cm-acq').value = r.rate;
    } else {
      $('#cm-acq').value = '';
    }
  } catch (err) { /* $('#x').value = 'in a comment'; */ }
}
api.get('/x').then((r) => { $('#y').value = r.v; });
const s = 'a string with .value = inside';
"""


def test_the_detector_sees_the_pre_fix_shape() -> None:
    assert late_writes(_PRE_FIX) == [8, 9, 11, 15]


def test_no_web_js_has_an_unguarded_late_write() -> None:
    """I-12: the class — every web/*.js, not the one file the defect was found in."""
    found = {k: v for k, v in _web_late_writes().items()
             if k not in _OWN_GUARD and k not in _PENDING}
    assert not found, (
        "a web/*.js writes an editable field after an await without the shared guard — use "
        "window.pdField.writeIfUntouched / autoFill (format.js), or prove an equivalent "
        "guard in _OWN_GUARD: " + "; ".join(f"{k} ({v})" for k, v in found.items()))


def test_the_own_guard_list_is_not_stale() -> None:
    present = set(_web_late_writes())
    assert not (set(_OWN_GUARD) - present), set(_OWN_GUARD) - present
    assert not (set(_PENDING) - present), f"fixed — remove from _PENDING: {set(_PENDING) - present}"


def test_the_structural_exemptions_are_narrow() -> None:
    """A fresh node filled AFTER an await, or an ASYNC listener, is still a late write."""
    src = """
async function a() {
  const o = el('option', null, 'x'); o.value = 'fresh';
  const ta = el('textarea');
  await api.get('/x');
  ta.value = 'late';
}
api.get('/y').then(() => {
  btn.addEventListener('click', () => { f.value = 'sync'; });
  btn.addEventListener('click', async () => { await g(); h.value = 'late'; });
});
"""
    assert late_writes(src, structural=True) == [6, 10]


def test_cash_js_has_no_raw_late_write() -> None:
    src = _CASH.read_text(encoding="utf-8")
    lines = src.splitlines()
    found = [(ln, lines[ln - 1].strip()) for ln in late_writes(src)]
    assert not found, (
        "web/cash.js writes an editable field after an await without the DEF-007 guard — "
        "use writeIfUntouched / autoFill: " + "; ".join(f"line {ln}: {t}" for ln, t in found))


def _function(src: str, name: str) -> str:
    m = re.search(r"\n(\s*)(?:async\s+)?function " + name + r"\(", src)
    assert m, f"function {name} not found in web/cash.js"
    start = src.index("{", m.end())
    return src[m.start():_match(src, start, "{", "}") + 1]


def test_the_prefill_never_switches_the_mode_and_dates_the_rate_by_as_of() -> None:
    src = _CASH.read_text(encoding="utf-8")
    code = _blank(src)
    assert not re.search(r"#cm-acq-mode'\)\.value\s*=(?!=)", src), (
        "the 取得成本 mode is the owner's choice — no code path may set it")
    body = _function(src, "prefillAcq")
    assert "r.as_of" in body, "the 參考值 hint must carry the RATE's date (r.as_of)"
    assert re.search(r"'參考值：' \+ asOf", body), body
    assert "autoFill($('#cm-acq'), r.rate)" in body
    # The FX estimate's retraction, a sync helper called after an await, uses the guard too.
    assert "autoFill(to, '')" in _function(src, "clearAutoEstimate")
    # One guard: the only raw writes of `input.value` live in the two helpers themselves —
    # in format.js since I-12, and none are left in cash.js, which binds the shared ones.
    assert not re.search(r"\binput\.value\s*=(?!=)", code)
    assert "const writeIfUntouched = window.pdField.writeIfUntouched;" in src
    guard = _blank(_GUARD.read_text(encoding="utf-8"))
    helper_writes = [m.start() for m in re.finditer(r"\binput\.value\s*=(?!=)", guard)]
    assert len(helper_writes) == 2, helper_writes   # writeIfUntouched + resetField


# --- behaviour of the guard itself, run in node over the REAL function text -------------

_HARNESS = r"""
const src = require('fs').readFileSync(process.argv[2], 'utf8');
function grab(name) {
  const i = src.indexOf('function ' + name + '(');
  let d = 0, j = src.indexOf('{', i);
  for (let k = j; k < src.length; k++) {
    if (src[k] === '{') d++;
    else if (src[k] === '}') { d--; if (d === 0) return src.slice(i, k + 1); }
  }
}
eval(grab('writeIfUntouched') + grab('autoFill') + grab('resetField'));
const field = (v) => ({ value: v, dataset: {} });
const out = {};
let f = field('');
out.fills_empty = [autoFill(f, '31.698999'), f.value, f.dataset.pdAuto];
f = field('31.5');                       // typed while the lookup was in flight
out.keeps_typed = [autoFill(f, '31.698999'), f.value];
f = field(''); autoFill(f, '31.69');     // the page's own value…
out.replaces_own = [autoFill(f, '31.70'), f.value];   // …is the page's to replace
f = field(''); autoFill(f, '31.69'); f.value = '31.5';
out.own_then_typed = [autoFill(f, ''), f.value];      // retraction spares a typed value
f = field('1000');                       // post-commit clear: only what was sent
out.clear_sent = [writeIfUntouched(f, '1000', ''), f.value];
f = field('2000');                       // the next entry typed during the round trip
out.clear_other = [writeIfUntouched(f, '1000', ''), f.value];
process.stdout.write(JSON.stringify(out));
"""


def _node() -> Path | None:
    import playwright

    for name in ("node.exe", "node"):
        node = Path(playwright.__file__).parent / "driver" / name
        if node.exists():
            return node
    return None


def test_the_guard_keeps_a_typed_value_and_replaces_only_its_own(tmp_path: Path) -> None:
    node = _node()
    if node is None:
        pytest.skip("Playwright's bundled node is not installed in this venv")
    harness = tmp_path / "h.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    proc = subprocess.run([str(node), str(harness), str(_GUARD)], capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["fills_empty"] == [True, "31.698999", "31.698999"]
    assert out["keeps_typed"] == [False, "31.5"]
    assert out["replaces_own"] == [True, "31.70"]
    assert out["own_then_typed"] == [False, "31.5"]
    assert out["clear_sent"] == [True, ""]
    assert out["clear_other"] == [False, "2000"]
