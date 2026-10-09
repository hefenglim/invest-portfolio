"""F-04 guard: every ``window.pdXxx()`` the frontend CALLS must be DEFINED somewhere in web/.

``broker-import.js`` called ``window.pdAfterLedgerChange()`` from two places. Nothing in the
repository ever assigned that name. Because every such call site is written defensively —
``if (window.pdX) window.pdX()`` — a wrong name does not throw, does not log, and does not
show up in a console-error sweep: it silently does nothing. Measured by the 2026-08-27 sweep:
an undo deleted three rows, toasted 「已復原 刪除 3 筆」, and left the ledger table below
still listing all three.

That is the mirror image of the export-centre buttons — one reported success and did nothing,
this one did the work and failed to show it — and neither is visible to a wiring sweep that
counts listeners (v0.1.26: 2,287 controls, 0 dead).

Static, because the alternative is an e2e test per seam and the seams are only discovered
once they break. The scan is deliberately blunt: any ``window.pd*`` that is called and never
assigned fails, which is exactly the class of typo that produced this one.
"""

import re
from pathlib import Path

_WEB = Path(__file__).resolve().parents[2] / "web"
_ASSIGN = re.compile(r"window\.(pd[A-Za-z0-9_]*)\s*=")
_CALL = re.compile(r"window\.(pd[A-Za-z0-9_]*)\s*\(")
# Vendored third-party bundle: not ours, and minified names would only add noise.
_SKIP = {"echarts.min.js"}


def _sources() -> list[Path]:
    return [p for p in sorted([*_WEB.glob("*.js"), *_WEB.glob("*.html")])
            if p.name not in _SKIP]


def test_every_called_pd_global_is_assigned_somewhere() -> None:
    defined: set[str] = set()
    called: dict[str, set[str]] = {}
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        defined.update(m.group(1) for m in _ASSIGN.finditer(text))
        for m in _CALL.finditer(text):
            called.setdefault(m.group(1), set()).add(path.name)
    orphans = {name: sorted(where) for name, where in called.items() if name not in defined}
    assert not orphans, (
        "these window.pd* functions are called but never defined — the "
        "`if (window.x)` guard makes each one a silent no-op: " + repr(orphans))


def test_the_scan_can_actually_see_a_call_and_a_definition() -> None:
    """A guard that matches nothing passes forever. Pin both halves against real files."""
    defined: set[str] = set()
    called: set[str] = set()
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        defined.update(m.group(1) for m in _ASSIGN.finditer(text))
        called.update(m.group(1) for m in _CALL.finditer(text))
    assert "pdLedgerRefresh" in defined and "pdLedgerRefresh" in called
    assert len(defined) > 10, f"the assignment scan found almost nothing: {sorted(defined)}"
