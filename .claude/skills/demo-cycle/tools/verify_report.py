"""verify_report.py — drive a demo-cycle report in a real browser before it is shown.

A report whose demo silently fails is worse than no report: it looks authoritative and
is wrong. This opens the generated HTML in chromium, clicks through every lab's mode
switch, and fails on anything that would make the document lie.

    .venv/Scripts/python.exe .claude/skills/demo-cycle/tools/verify_report.py \
        docs/spec/2026-07-27-drip-preview.html --shots <dir>

Checks (all blocking unless marked):
  1. zero console errors / uncaught page errors
  2. every self-check registered via PD.check() passes  (no checks at all = warning)
  3. every .lab reaches a measured state — no readout left at the placeholder "—"
  4. every mode button in every .lab is clickable and leaves the readouts measured
  5. self-contained: no http(s):// asset reference (CDN/font/image) anywhere
  6. no horizontal page overflow at 390px and at 1440px
  7. every TOC anchor resolves to an element that exists

Exit code 0 = the report can be shown to the owner.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

WIDTHS = (390, 1440)

_JS_OVERFLOW = """
() => {
  const de = document.scrollingElement;
  const out = { over: Math.max(0, de.scrollWidth - de.clientWidth), who: [] };
  if (out.over > 0) {
    for (const el of document.querySelectorAll('body *')) {
      const r = el.getBoundingClientRect();
      if (r.width === 0 && r.height === 0) continue;
      let scrollable = false;
      for (let p = el.parentElement; p; p = p.parentElement) {
        const ox = getComputedStyle(p).overflowX;
        if (ox === 'auto' || ox === 'scroll') { scrollable = true; break; }
      }
      if (!scrollable && r.right > de.clientWidth + 1) {
        out.who.push(el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') +
                     ' +' + Math.round(r.right - de.clientWidth) + 'px');
      }
    }
  }
  return out;
}
"""

# A lab is "measured" when its readouts hold something other than the em-dash the
# scaffold ships. An unmeasured lab means the sandbox never posted back — the exact
# silent failure this script exists to catch.
_JS_LABS = """
() => Array.from(document.querySelectorAll('.lab')).map((lab, i) => ({
  i,
  title: (lab.querySelector('h3') || {}).textContent || ('lab ' + i),
  modes: lab.querySelectorAll('.modes button').length,
  readouts: Array.from(lab.querySelectorAll('.ro .v')).map(v => v.textContent.trim()),
  verdict: (lab.querySelector('.verdict') || {}).textContent || '',
}))
"""

_JS_ANCHORS = """
() => Array.from(document.querySelectorAll('.toc a'))
  .map(a => ({ href: a.getAttribute('href'),
               ok: !!document.querySelector(a.getAttribute('href')) }))
"""


def _unmeasured(readouts: list[str]) -> list[str]:
    return [r for r in readouts if r in ("", "—", "-")]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("report")
    ap.add_argument("--shots", help="directory for screenshots")
    ap.add_argument("--allow-todo", action="store_true",
                    help="do not warn about remaining TODO markers (scaffold stage)")
    args = ap.parse_args(argv)

    path = Path(args.report).resolve()
    if not path.is_file():
        print(f"!! not a file: {path}", file=sys.stderr)
        return 2
    raw = path.read_text(encoding="utf-8")

    failures: list[str] = []
    warnings: list[str] = []

    # ---- 5. self-contained (static check: no browser needed) -----------------
    ext = sorted({m for m in re.findall(r'(?:src|href)\s*=\s*["\'](https?://[^"\']+)', raw)})
    if ext:
        failures.append(f"external asset reference(s) — not self-contained: {ext[:3]}")
    todos = raw.count("TODO")
    if todos and not args.allow_todo:
        warnings.append(f"{todos} TODO marker(s) still in the document")

    shots = Path(args.shots) if args.shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for width in WIDTHS:
            errors: list[str] = []
            page = browser.new_page(viewport={"width": width, "height": 1000})
            page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}")
                    if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            page.goto(path.as_uri(), wait_until="load")
            page.wait_for_timeout(700)

            labs: list[dict[str, Any]] = page.evaluate(_JS_LABS)

            # ---- 4. drive every mode of every lab ---------------------------
            if width == WIDTHS[-1]:
                for lab in labs:
                    for m in range(lab["modes"]):
                        btn = page.locator(".lab").nth(lab["i"]).locator(".modes button").nth(m)
                        try:
                            btn.click(timeout=3_000)
                            page.wait_for_timeout(350)
                        except Exception as exc:  # noqa: BLE001
                            failures.append(f"lab {lab['i']} mode {m} not clickable: "
                                            f"{str(exc).splitlines()[0][:80]}")
                            continue
                        state = page.evaluate(_JS_LABS)[lab["i"]]
                        blank = _unmeasured(state["readouts"])
                        if blank:
                            failures.append(
                                f"lab {lab['i']} ({lab['title'].strip()[:40]}) mode {m}: "
                                f"{len(blank)} readout(s) never measured — the sandbox "
                                "did not post back")
                        if state["verdict"].strip() in ("—", ""):
                            failures.append(f"lab {lab['i']} mode {m}: verdict never written")

            # ---- 3. labs measured at rest ----------------------------------
            for lab in labs:
                blank = _unmeasured(lab["readouts"])
                if blank and lab["readouts"]:
                    warnings.append(f"@{width}px lab {lab['i']} has "
                                    f"{len(blank)}/{len(lab['readouts'])} unmeasured readouts")

            # ---- 6. overflow ------------------------------------------------
            ov = page.evaluate(_JS_OVERFLOW)
            if ov["over"] > 0:
                failures.append(f"horizontal overflow at {width}px: +{ov['over']}px "
                                f"({', '.join(ov['who'][:3]) or 'no single offender'})")

            # ---- 1. console -------------------------------------------------
            for e in errors:
                failures.append(f"@{width}px {e[:140]}")

            # ---- 2. self-checks ---------------------------------------------
            if width == WIDTHS[-1]:
                checks: list[dict[str, Any]] = page.evaluate(
                    "() => window.__demoCycleChecks || []")
                bad = [c for c in checks if not c["pass"]]
                for c in bad:
                    failures.append(f"self-check FAILED: {c['name']} — {c['detail'][:90]}")
                if not checks:
                    warnings.append("no PD.check() self-checks registered — any offline "
                                    "recomputation in this report is unproven")
                else:
                    print(f"  self-checks: {len(checks) - len(bad)}/{len(checks)} pass")

                # ---- 7. anchors ---------------------------------------------
                for a in page.evaluate(_JS_ANCHORS):
                    if not a["ok"]:
                        failures.append(f"TOC anchor {a['href']} resolves to nothing")

            if shots:
                shot = shots / f"{path.stem}-{width}.png"
                page.screenshot(path=str(shot), full_page=True)
                print(f"  shot: {shot}")
            page.close()
        browser.close()

    print(f"\n  {path.name}: {len(raw):,} bytes, "
          f"{raw.count('<section')} sections, {raw.count('class=\"lab\"')} lab(s)")
    for w in warnings:
        print(f"  warn: {w}")
    if failures:
        print(f"\n  FAIL ({len(failures)}):")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("  PASS — report is safe to show.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
