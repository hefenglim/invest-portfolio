"""probe_live.py — measure the RUNNING app instead of guessing from the source.

This is the anti-guess instrument for /demo-cycle. The v0.1.24 audit got the SYMPTOM
right on every finding and the ROOT CAUSE wrong on four of them, because four root
causes were derived by reading code. Two classes of truth are simply not readable:

  * CSS cascade — which declaration actually wins (order at equal specificity; media
    queries adding no specificity). Only the browser answers this. -> ``css`` mode.
  * Whether a control is wired to anything at all. -> ``wiring`` mode.
  * What actually overflows / gets clipped at a given viewport. -> ``layout`` mode.

Everything here reads; nothing mutates the app. Safe against a demo or a local
instance. (Do NOT point it at prod: it is read-only, but it is also pointless there.)

Usage (from the repo root, with the repo venv):

    .venv/Scripts/python.exe .claude/skills/demo-cycle/tools/probe_live.py \
        layout http://127.0.0.1:8477 --widths 390,768,1280 --out <dir>

    .venv/Scripts/python.exe .claude/skills/demo-cycle/tools/probe_live.py \
        css http://127.0.0.1:8477 --selector .topbar --prop flex-wrap --width 1257

    .venv/Scripts/python.exe .claude/skills/demo-cycle/tools/probe_live.py \
        wiring http://127.0.0.1:8477 --pages /data-center.html [--click]

Run these from PowerShell, not Git Bash: MSYS rewrites a leading-`/` argument into a
Windows path, so `--pages /` silently probes the wrong URL. Set PYTHONIOENCODING=utf-8.

Exit code is 0 unless the probe itself failed; findings are DATA, not failures.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright

REPO_ROOT = Path(__file__).resolve().parents[4]

# Viewport widths that matter for this project: phone, tablet, the 1257px window the
# owner actually reported, and desktop.
DEFAULT_WIDTHS = (390, 768, 1257, 1440)

# ---------------------------------------------------------------------------- JS


# Returns every rule that MATCHES the element and DECLARES the property, in cascade
# order, with an approximate specificity — and marks the winner. Media queries are
# reported with their condition text precisely because they add NO specificity, which
# is invisible when reading the file.
_JS_CSS_TRACE = r"""
(args) => {
  const { selector, prop } = args;
  const el = document.querySelector(selector);
  if (!el) return { error: 'selector matched nothing: ' + selector };

  function spec(sel) {
    // Approximate CSS specificity (a,b,c). Good enough to explain a cascade upset;
    // exact enough for #id / .class / [attr] / :pseudo / element counting.
    let s = sel.replace(/\((?:[^()]|\([^()]*\))*\)/g, ' ');   // strip :not(...) innards
    const a = (s.match(/#[\w-]+/g) || []).length;
    const b = (s.match(/\.[\w-]+/g) || []).length
            + (s.match(/\[[^\]]+\]/g) || []).length
            + (s.match(/:(?!:)[\w-]+/g) || []).length;
    const c = (s.match(/(^|[\s>+~])[a-zA-Z][\w-]*/g) || []).length
            + (s.match(/::[\w-]+/g) || []).length;
    return [a, b, c];
  }
  const cmp = (x, y) => (x[0] - y[0]) || (x[1] - y[1]) || (x[2] - y[2]);

  const hits = [];
  let order = 0;
  function walk(rules, media, href) {
    for (const r of rules) {
      if (r.cssRules && (r.media || r.conditionText !== undefined) && !r.selectorText) {
        walk(r.cssRules, (media ? media + ' AND ' : '') + (r.conditionText || ''), href);
        continue;
      }
      if (!r.selectorText || !r.style) continue;
      const value = r.style.getPropertyValue(prop);
      if (!value) continue;
      // A @media rule only participates in the cascade when its condition CURRENTLY
      // holds. Listing every breakpoint as a candidate is exactly the misreading this
      // tool exists to prevent, so the applies-now flag is computed, not assumed.
      let applies = true;
      if (media) { try { applies = window.matchMedia(media).matches; } catch (e) { applies = false; } }
      for (const raw of r.selectorText.split(',')) {
        const sel = raw.trim();
        let matches = false;
        try { matches = el.matches(sel); } catch (e) { continue; }
        if (!matches) continue;
        hits.push({
          selector: sel, media: media || null, applies, value: value.trim(),
          important: r.style.getPropertyPriority(prop) === 'important',
          specificity: spec(sel), order: order++,
          sheet: href ? href.split('/').pop() : '(inline)',
        });
      }
    }
  }
  for (const sheet of document.styleSheets) {
    let rules = null;
    try { rules = sheet.cssRules; } catch (e) { continue; }   // cross-origin
    if (rules) walk(rules, null, sheet.href);
  }

  // Inline style beats every author rule (short of !important).
  const inlineVal = el.style.getPropertyValue(prop);
  if (inlineVal) {
    hits.push({ selector: '[element style attribute]', media: null, applies: true,
                value: inlineVal.trim(),
                important: el.style.getPropertyPriority(prop) === 'important',
                specificity: [1, 0, 0, 0], order: order++, sheet: '(inline attr)' });
  }

  const ranked = hits.filter(h => h.applies).sort((x, y) => {
    if (x.important !== y.important) return x.important ? -1 : 1;
    const s = cmp(y.specificity.slice(-3), x.specificity.slice(-3));
    if (s !== 0) return s;
    return y.order - x.order;                                  // later source wins
  });
  const winner = ranked[0] || null;
  if (winner) winner.winner = true;

  const r = el.getBoundingClientRect();
  return {
    selector, prop,
    computed: getComputedStyle(el).getPropertyValue(prop),
    box: { w: Math.round(r.width), h: Math.round(r.height) },
    scrollWidth: el.scrollWidth, clientWidth: el.clientWidth,
    clipped: el.scrollWidth > el.clientWidth + 1,
    candidates: hits.sort((x, y) => x.order - y.order),
    winner,
  };
}
"""


# Everything that actually overflows or gets clipped at the current viewport, with the
# scrolling ancestor that legitimises it (a table inside overflow-x:auto is FINE — the
# audit's M-class findings turned on exactly this distinction).
_JS_LAYOUT = r"""
() => {
  const de = document.scrollingElement;
  const vw = de.clientWidth;
  const out = { docScrollWidth: de.scrollWidth, docClientWidth: vw,
                overflowPx: Math.max(0, de.scrollWidth - vw), offenders: [], clipped: [] };

  function scrollableAncestor(el) {
    for (let p = el.parentElement; p; p = p.parentElement) {
      const ox = getComputedStyle(p).overflowX;
      if (ox === 'auto' || ox === 'scroll') return p;
    }
    return null;
  }
  function label(el) {
    const id = el.id ? '#' + el.id : '';
    const cls = (el.className && typeof el.className === 'string')
      ? '.' + el.className.trim().split(/\s+/).slice(0, 3).join('.') : '';
    return el.tagName.toLowerCase() + id + cls;
  }

  for (const el of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;

    if (r.right > vw + 1 && !scrollableAncestor(el)) {
      out.offenders.push({ el: label(el), right: Math.round(r.right), over: Math.round(r.right - vw),
                           width: Math.round(r.width), text: (el.textContent || '').trim().slice(0, 60) });
    }
    // Clipped text: content wider than the box, no scrollbar to reach it.
    if (el.scrollWidth > el.clientWidth + 1 && cs.overflowX !== 'auto' && cs.overflowX !== 'scroll') {
      const t = (el.textContent || '').trim();
      if (t && el.children.length === 0) {
        out.clipped.push({ el: label(el), scrollWidth: el.scrollWidth, clientWidth: el.clientWidth,
                           whiteSpace: cs.whiteSpace, overflow: cs.overflowX,
                           textOverflow: cs.textOverflow, display: cs.display, text: t.slice(0, 60) });
      }
    }
  }
  // Deepest-first is noisy; report the widest offenders first.
  out.offenders.sort((a, b) => b.over - a.over);
  out.clipped.sort((a, b) => (b.scrollWidth - b.clientWidth) - (a.scrollWidth - a.clientWidth));
  return out;
}
"""


# Every interactive control on the page, with the handles a JS file could bind to.
_JS_CONTROLS = r"""
() => {
  const sel = 'button, [role=button], input[type=button], input[type=submit], a[href],' +
              ' select, [data-action], [onclick]';
  // A re-findable path for the click pass: the control must be locatable again on a
  // freshly loaded page, and most of this app's chrome carries no id.
  function cssPath(el) {
    const parts = [];
    for (let n = el; n && n.nodeType === 1 && n !== document.body; n = n.parentElement) {
      if (n.id) { parts.unshift('#' + CSS.escape(n.id)); break; }
      const i = Array.prototype.indexOf.call(n.parentElement.children, n) + 1;
      parts.unshift(n.tagName.toLowerCase() + ':nth-child(' + i + ')');
    }
    return (parts[0] || '').startsWith('#') ? parts.join(' > ') : 'body > ' + parts.join(' > ');
  }
  const out = [];
  for (const el of document.querySelectorAll(sel)) {
    const cs = getComputedStyle(el);
    const data = {};
    for (const k in el.dataset) data[k] = el.dataset[k];
    out.push({
      path: cssPath(el),
      tag: el.tagName.toLowerCase(),
      id: el.id || null,
      cls: (typeof el.className === 'string' ? el.className.trim() : '') || null,
      text: (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 40),
      href: el.getAttribute('href'),
      type: el.getAttribute('type'),
      data,
      inlineHandler: !!el.getAttribute('onclick'),
      inForm: !!el.closest('form'),
      visible: cs.display !== 'none' && cs.visibility !== 'hidden' && el.getBoundingClientRect().width > 0,
      disabled: !!el.disabled,
    });
  }
  return out;
}
"""


# --------------------------------------------------------------------- helpers


def _pages_arg(raw: str | None) -> list[str]:
    if raw:
        return [p if p.startswith("/") else "/" + p for p in raw.split(",") if p.strip()]
    return ["/"]


def _new_page(pw: Any, base: str, width: int, errors: list[str]) -> tuple[Any, Page]:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": width, "height": 900})
    page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}")
            if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    return browser, page


def _url(base: str, path: str) -> str:
    """Join base+path, but let a direct file:// or *.html target be used verbatim.

    A generated report is a legitimate probe target (checking the REPORT's own cascade),
    and it has no path to append.
    """
    if base.endswith(".html") and path in ("", "/"):
        return base
    return base.rstrip("/") + path


def _goto(page: Page, url: str) -> None:
    page.goto(url, wait_until="networkidle", timeout=30_000)
    page.wait_for_timeout(400)  # let ECharts / late renders settle


def _fmt_spec(s: list[int]) -> str:
    return "(" + ",".join(str(x) for x in s[-3:]) + ")"


# ------------------------------------------------------------------ mode: css


def cmd_css(args: argparse.Namespace) -> dict[str, Any]:
    errors: list[str] = []
    with sync_playwright() as pw:
        browser, page = _new_page(pw, args.base, args.width, errors)
        try:
            _goto(page, _url(args.base, args.page))
            result: dict[str, Any] = page.evaluate(
                _JS_CSS_TRACE, {"selector": args.selector, "prop": args.prop}
            )
        finally:
            browser.close()

    if result.get("error"):
        print(f"!! {result['error']}", file=sys.stderr)
        return result

    print(f"\n  {args.selector}  {{ {args.prop} }}   @ {args.width}px  {args.page}")
    print(f"  computed  = {result['computed']!r}"
          f"   box {result['box']['w']}x{result['box']['h']}"
          f"   {'CLIPPED' if result['clipped'] else 'not clipped'}")
    active = [c for c in result["candidates"] if c["applies"]]
    print(f"  {len(result['candidates'])} matching declaration(s) "
          f"({len(active)} active at this width), in source order:\n")
    for c in result["candidates"]:
        mark = "->" if c.get("winner") else ("  " if c["applies"] else " x")
        media = f"  @media {c['media']}" if c["media"] else ""
        imp = " !important" if c["important"] else ""
        note = "" if c["applies"] else "   (condition false at this width)"
        print(f"  {mark} {_fmt_spec(c['specificity'])} #{c['order']:<3} "
              f"{c['selector']}{media}  =>  {c['value']}{imp}   [{c['sheet']}]{note}")

    w = result["winner"]
    if w:
        print(f"\n  WINNER: {w['selector']}{' @media ' + w['media'] if w['media'] else ''}"
              f" -> {w['value']}")
        losers = [c for c in active if not c.get("winner") and c["value"] != w["value"]]
        same = [c for c in losers if c["specificity"][-3:] == w["specificity"][-3:]]
        if same:
            print(f"  NOTE: {same[0]['selector']}"
                  f"{' @media ' + same[0]['media'] if same[0]['media'] else ''} declares "
                  f"{same[0]['value']!r} at EQUAL specificity and loses on SOURCE ORDER."
                  "\n        Editing it cannot fix this — remove or retarget the winner.")
        outspec = [c for c in losers if c["media"] and not w["media"]
                   and c["specificity"][-3:] < w["specificity"][-3:]]
        if outspec:
            print(f"  NOTE: the @media rule {outspec[0]['selector']} "
                  f"@media {outspec[0]['media']} is OUT-SPECIFIED by an unconditional "
                  "rule.\n        Media queries add NO specificity — that breakpoint is "
                  "dead for this element until its selector is raised.")
        # A declaration in fr / % / calc / var / repeat() legitimately resolves to px;
        # only flag a mismatch the cascade does NOT explain.
        resolvable = re.search(r"\b(fr|repeat|calc|var|clamp|min|max|%)\b|%",
                               w["value"]) is not None
        if w["value"].strip() != result["computed"].strip() and not resolvable:
            print(f"  NOTE: computed ({result['computed']!r}) != winning declaration "
                  f"({w['value']!r}) — a shorthand, inheritance, or an animation is "
                  "involved. Trust `computed`.")
    else:
        print(f"\n  No author rule declares {args.prop} on this element — computed "
              f"({result['computed']!r}) comes from inheritance or the UA stylesheet.")
    if errors:
        print(f"\n  page errors: {len(errors)}")
        for e in errors[:5]:
            print("   -", e)
    return result


# --------------------------------------------------------------- mode: layout


def cmd_layout(args: argparse.Namespace) -> dict[str, Any]:
    out_dir = Path(args.out) if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"base": args.base, "pages": {}}

    with sync_playwright() as pw:
        for path in _pages_arg(args.pages):
            report["pages"][path] = {}
            for width in [int(w) for w in args.widths.split(",")]:
                errors: list[str] = []
                browser, page = _new_page(pw, args.base, width, errors)
                try:
                    _goto(page, _url(args.base, path))
                    res: dict[str, Any] = page.evaluate(_JS_LAYOUT)
                    res["errors"] = errors
                    if out_dir:
                        slug = (path.strip("/") or "index").replace("/", "_").replace(".html", "")
                        shot = out_dir / f"{slug}-{width}.png"
                        page.screenshot(path=str(shot), full_page=True)
                        res["screenshot"] = str(shot)
                finally:
                    browser.close()
                report["pages"][path][width] = res

                over = res["overflowPx"]
                flag = "OVERFLOW" if over else "ok      "
                print(f"  {flag} {path:<26} {width:>5}px  doc={res['docScrollWidth']:<5}"
                      f" vw={res['docClientWidth']:<5} +{over}px"
                      f"  offenders={len(res['offenders'])} clipped={len(res['clipped'])}"
                      f" errors={len(errors)}")
                for o in res["offenders"][:3]:
                    print(f"      -> +{o['over']}px  {o['el']}  {o['text'][:36]!r}")
                for c in res["clipped"][:3]:
                    print(f"      ~~ clipped {c['scrollWidth']}>{c['clientWidth']} "
                          f"{c['el']}  ws={c['whiteSpace']} disp={c['display']} "
                          f"ellipsis={'yes' if c['textOverflow'] == 'ellipsis' else 'NO'}"
                          f"  {c['text'][:30]!r}")
                for e in errors[:3]:
                    print(f"      !! {e[:110]}")
    if out_dir:
        (out_dir / "layout.json").write_text(
            json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"\n  -> {out_dir / 'layout.json'}  (+ screenshots)")
    return report


# --------------------------------------------------------------- mode: wiring


def _web_sources(web_dir: Path) -> str:
    """The BEHAVIOUR haystack: .js files + inline <script> bodies. Markup is excluded.

    Excluding markup is the whole point. A dead button's own ``id="btn-dead"`` lives in
    the HTML, so an all-files haystack matches every control against its own
    declaration and reports 100% wired — which is what the fixture caught on the first
    run of this probe. Only code that could bind a handler counts as evidence.
    """
    blob: list[str] = []
    for f in sorted(web_dir.rglob("*.js")):
        try:
            blob.append(f.read_text(encoding="utf-8"))
        except OSError:
            continue
    for f in sorted(web_dir.rglob("*.html")):
        try:
            html = f.read_text(encoding="utf-8")
        except OSError:
            continue
        blob.extend(re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S | re.I))
    return "\n".join(blob)


_JS_WATCH = r"""
() => {
  window.__mut = 0;
  new MutationObserver(rs => { window.__mut += rs.length; })
    .observe(document.documentElement,
             { subtree: true, childList: true, attributes: true, characterData: true });
}
"""


def _click_probe(pw: Any, base: str, path: str, targets: list[dict[str, Any]],
                 settle_ms: int) -> None:
    """Click each target on a FRESH page and record what actually happened.

    This is the only decisive test for a dead control: static evidence can only say
    "nothing references it", never "it does nothing". MUTATES APP STATE — demo/test
    instances only.
    """
    browser = pw.chromium.launch()
    try:
        for t in targets:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            reqs: list[str] = []
            logs: list[str] = []
            dialogs: list[str] = []
            page.on("request", lambda r: reqs.append(r.url))
            page.on("console", lambda m: logs.append(f"{m.type}:{m.text}"[:120]))
            page.on("dialog", lambda d: (dialogs.append(d.message[:80]), d.dismiss()))
            try:
                _goto(page, _url(base, path))
                before_url = page.url
                page.evaluate(_JS_WATCH)
                reqs.clear()
                page.locator(t["path"]).first.click(timeout=3_000)
                page.wait_for_timeout(settle_ms)
                mut = page.evaluate("() => window.__mut || 0")
                effects = []
                if page.url != before_url:
                    effects.append(f"navigated -> {page.url.split('/')[-1]}")
                if reqs:
                    effects.append(f"{len(reqs)} request(s) e.g. {reqs[0].split('/')[-1][:40]}")
                if mut:
                    effects.append(f"{mut} DOM mutation(s)")
                if dialogs:
                    effects.append(f"dialog: {dialogs[0]}")
                if logs:
                    effects.append(f"console: {logs[0]}")
                t["click"] = {"effects": effects,
                              "verdict": "responds" if effects else "NO EFFECT"}
            except Exception as exc:  # noqa: BLE001 — a failed click is a finding
                t["click"] = {"effects": [], "verdict": "unclickable",
                              "error": str(exc).splitlines()[0][:120]}
            finally:
                page.close()
    finally:
        browser.close()


def cmd_wiring(args: argparse.Namespace) -> dict[str, Any]:
    web_dir = Path(args.web_dir) if args.web_dir else REPO_ROOT / "web"
    if not web_dir.is_dir():
        print(f"!! web dir not found: {web_dir}", file=sys.stderr)
        sys.exit(2)
    blob = _web_sources(web_dir)

    report: dict[str, Any] = {"base": args.base, "webDir": str(web_dir), "pages": {}}
    with sync_playwright() as pw:
        for path in _pages_arg(args.pages):
            errors: list[str] = []
            browser, page = _new_page(pw, args.base, 1440, errors)
            try:
                _goto(page, _url(args.base, path))
                controls: list[dict[str, Any]] = page.evaluate(_JS_CONTROLS)
            finally:
                browser.close()

            # How many controls on this page wear each class. A class worn by one or
            # two controls identifies them; `.btn` worn by 21 identifies nothing.
            cls_freq: dict[str, int] = {}
            for c in controls:
                for cls in (c["cls"] or "").split():
                    cls_freq[cls] = cls_freq.get(cls, 0) + 1

            rows: list[dict[str, Any]] = []
            for c in controls:
                # STRONG evidence = this exact control is addressable from code.
                strong: list[str] = []
                if c["inlineHandler"]:
                    strong.append("inline onclick")
                if c["tag"] == "a" and c["href"] and not c["href"].startswith("#"):
                    strong.append(f"link -> {c['href']}")
                if c["type"] == "submit" and c["inForm"]:
                    strong.append("form submit")
                if c["id"]:
                    for needle in (f"'{c['id']}'", f'"{c["id"]}"', f"#{c['id']}"):
                        if needle in blob:
                            strong.append(f"id referenced ({needle})")
                            break
                for k, v in (c["data"] or {}).items():
                    # Values shorter than 3 chars ("1", "on") match by accident, and
                    # code may WRITE dataset at runtime (the fixture's `dataset.seen =
                    # '1'` made every button look dispatched-on). Require the key to be
                    # read in code AND a non-trivial value.
                    if not v or len(str(v)) < 3:
                        continue
                    key_seen = f"dataset.{k}" in blob or f"data-{k}" in blob
                    if key_seen and (f"'{v}'" in blob or f'"{v}"' in blob):
                        strong.append(f"data-{k}={v} dispatched on")
                        break
                # A class worn by only a couple of controls IDENTIFIES them, so a quoted
                # reference is real evidence — including the bare form, because this app
                # builds its chrome programmatically (`el('button','sb-collapse')`), where
                # the class never appears as a `.selector` at all.
                weak: list[str] = []
                for cls in sorted((c["cls"] or "").split(), key=lambda x: cls_freq.get(x, 0)):
                    freq = cls_freq.get(cls, 0)
                    ref = (f"'.{cls}'" in blob or f'".{cls}"' in blob
                           or f"'{cls}'" in blob or f'"{cls}"' in blob)
                    if not ref:
                        continue
                    if freq <= 2:
                        strong.append(f"class {cls!r} referenced (worn by {freq} control)")
                        break
                    if not weak:
                        weak.append(f"class .{cls} referenced but shared by {freq} "
                                    "controls — proves nothing about THIS one")

                verdict = "wired" if strong else ("weak" if weak else "SUSPECT")
                rows.append({**c, "evidence": strong + weak, "verdict": verdict})

            report["pages"][path] = rows
            vis = [r for r in rows if r["visible"]]
            suspects = [r for r in vis if r["verdict"] == "SUSPECT"]
            weaks = [r for r in vis if r["verdict"] == "weak"]
            print(f"\n  {path}: {len(rows)} controls ({len(vis)} visible) — "
                  f"{len(vis) - len(suspects) - len(weaks)} wired, {len(weaks)} weak, "
                  f"{len(suspects)} SUSPECT")
            for s in suspects:
                ident = s["id"] or s["cls"] or s["tag"]
                print(f"      ? SUSPECT {s['tag']:<7} {ident[:32]:<32} {s['text'][:28]!r}")
            for s in weaks[:8]:
                ident = s["id"] or s["cls"] or s["tag"]
                print(f"      ~ weak    {s['tag']:<7} {ident[:32]:<32} {s['text'][:28]!r}")
            if len(weaks) > 8:
                print(f"      ~ ... and {len(weaks) - 8} more weak (see --out JSON)")
            if errors:
                print(f"      !! {len(errors)} page error(s): {errors[0][:100]}")

            if args.click:
                targets = suspects + weaks
                if targets:
                    print(f"      clicking {len(targets)} lead(s), fresh page each "
                          f"(MUTATES STATE)…")
                    _click_probe(pw, args.base, path, targets, args.settle_ms)
                    for t in targets:
                        cl = t["click"]
                        ident = t["id"] or t["cls"] or t["tag"]
                        mark = {"NO EFFECT": "DEAD?", "unclickable": "BLOCK"}.get(
                            cl["verdict"], "  ok ")
                        detail = "; ".join(cl["effects"]) or cl.get("error", "nothing happened")
                        print(f"      {mark} {ident[:30]:<30} {detail[:76]}")

    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"\n  -> {p}")
    print("\n  SUSPECT = nothing in the frontend sources addresses this control.  "
          "weak = only a\n  shared class selector matched, which proves nothing about "
          "THIS control. Both are\n  LEADS, not verdicts — click it in a browser before "
          "putting it in a report.")
    return report


# ------------------------------------------------------------------------ cli


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode", required=True)

    p_l = sub.add_parser("layout", help="overflow / clipped text / console errors per width")
    p_l.add_argument("base")
    p_l.add_argument("--pages", help="comma-separated paths (default /)")
    p_l.add_argument("--widths", default=",".join(str(w) for w in DEFAULT_WIDTHS))
    p_l.add_argument("--out", help="directory for screenshots + layout.json")
    p_l.set_defaults(func=cmd_layout)

    p_c = sub.add_parser("css", help="which CSS rule ACTUALLY wins for a property")
    p_c.add_argument("base")
    p_c.add_argument("--page", default="/")
    p_c.add_argument("--selector", required=True)
    p_c.add_argument("--prop", required=True)
    p_c.add_argument("--width", type=int, default=1257)
    p_c.set_defaults(func=cmd_css)

    p_w = sub.add_parser("wiring", help="interactive controls vs frontend source references")
    p_w.add_argument("base")
    p_w.add_argument("--pages", help="comma-separated paths (default /)")
    p_w.add_argument("--web-dir", help="frontend source dir (default <repo>/web)")
    p_w.add_argument("--out", help="write JSON here")
    p_w.add_argument("--click", action="store_true",
                     help="CLICK every weak/SUSPECT lead and record the effect. "
                          "MUTATES APP STATE — demo/test instances only, never prod.")
    p_w.add_argument("--settle-ms", type=int, default=700,
                     help="wait after each click before measuring (default 700)")
    p_w.set_defaults(func=cmd_wiring)

    args = ap.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
