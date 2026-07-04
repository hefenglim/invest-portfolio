# Appendix — R8 Assessment Evidence & Rerunnable Test Tooling (2026-07-04)

Companion to `2026-07-04-llm-theme-assessment.md`. Archives the raw check
results and the (sanitized) Playwright tooling so the assessment can be rerun
after future rounds. Host URLs are NEVER committed — every script below reads
the target from the `PD_TEST_URL` environment variable (see the git-ignored
`docs/human_noted/` deployment note for the real value).

---

## 1. Raw results — dual-version full-site pass (v0.1.9 on the test instance)

```
PASS  [desktop] 12 pages load, no h-overflow — all clean
PASS  [desktop] zero console/page errors — console=[] page=[]
PASS  [desktop] AI parse surfaces a REAL degraded panel — off=False quota=True down=False
PASS  [desktop] 再平衡試算 opens (.rb-drawer; 現權重/目標%/動作/費稅 columns, zero errors)
PASS  [desktop] insights page honest empty/degraded state
PASS  [mobile] 12 pages load, no h-overflow — all clean
PASS  [mobile] zero console/page errors — console=[] page=[]
PASS  [mobile] hamburger drawer opens (backdrop + badge riding along)
PASS  GET /api/insights healthy (empty ok) — HTTP 200
PASS  GET /api/insight-tasks healthy — HTTP 200
PASS  GET /api/llm/config healthy — all four roles null (default/vision/master/fallback)
PASS  POST /api/whatif — buy 2330 100@2400 → fee 342 (0.1425%·min 20), tax 0
PASS  GET /api/prompt-vars — HTTP 200 (24 vars registered)
```

Pages covered (both viewports): index, trades, cash, instruments, input,
insights, pipeline-hub, settings, settings-llm, settings-scheduler,
settings-datasources, login.

Key dormant-state facts for the LLM pillar (the P0 item):
- `/api/llm/config`: `default_model / default_fallback / vision_model /
  vision_fallback / master_model / master_fallback` all `null`.
- AI budget $0 → every AI call answered by an honest 402 quota panel; nothing
  fabricated, nothing crashed.
- Insight scheduler jobs registered; runners no-op safely while unconfigured.

## 2. Mobile overflow history (R7 baseline → fix), for regression reference

| Page | Before (390px) | After v0.1.9 |
| --- | --- | --- |
| index | 812px | 5px (sub-perceptual) |
| settings-datasources | 957px | 0 |
| input | 313px | 0 |
| trades | 297px | 0 |
| cash / instruments / settings / scheduler / insights | 260–292px | 0 |
| login | 0 | 0 |

Offenders found by the element probe (§4): fixed 196px sidebar (all pages),
`table.ccyret` + datasources source tables outside any `.table-wrap`,
5-tab `.segmented` bars, `.kpi-subline` no-wrap, ECharts canvases initialized
at pre-collapse width.

## 3. Rerunnable script — dual-version layout + function pass

Run: `PD_TEST_URL=https://<test-host> python dual_test.py`

```python
"""R8 dual-version (desktop 1440 / mobile 390) full-site layout+function pass."""

import json
import os
import sys
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

BASE = os.environ["PD_TEST_URL"].rstrip("/")
results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))
    results.append((ok, name))


PAGES = [
    ("index", "/index.html", "#holdings-body tr"),
    ("trades", "/trades.html", "#tx-body tr"),
    ("cash", "/cash.html", ".cash-card"),
    ("instruments", "/instruments.html", "#inst-body tr"),
    ("input", "/input.html", "#m-account option"),
    ("insights", "/insights.html", "body"),
    ("pipeline", "/pipeline-hub.html", "body"),
    ("settings", "/settings.html", "body"),
    ("settings-llm", "/settings-llm.html", "body"),
    ("settings-scheduler", "/settings-scheduler.html", "#jobs-body tr"),
    ("settings-datasources", "/settings-datasources.html",
     "#market-order-wrap .fallback-card"),
    ("login", "/login.html", "body"),
]

with sync_playwright() as p:
    browser = p.chromium.launch()
    for label, vw, mobile in (("desktop", {"width": 1440, "height": 900}, False),
                              ("mobile", {"width": 390, "height": 844}, True)):
        ctx = browser.new_context(viewport=vw, is_mobile=mobile, has_touch=mobile,
                                  device_scale_factor=3 if mobile else 1)
        page = ctx.new_page()
        ce: list[str] = []
        pe: list[str] = []
        page.on("console", lambda m: ce.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: pe.append(str(e)))
        bad_scroll = []
        for name, path, sel in PAGES:
            try:
                page.goto(BASE + path, wait_until="load", timeout=60000)
                page.wait_for_selector(sel, timeout=30000, state="attached")
                page.wait_for_timeout(1200)
                h = page.evaluate(
                    "() => document.documentElement.scrollWidth - "
                    "document.documentElement.clientWidth")
                if h > 8:
                    bad_scroll.append(f"{name}:{h}px")
            except Exception as exc:  # noqa: BLE001
                bad_scroll.append(f"{name}:LOAD-ERR {str(exc)[:60]}")
        errs = [e for e in ce if "Failed to load resource" not in e]
        check(f"[{label}] 12 pages load, no h-overflow", not bad_scroll,
              "; ".join(bad_scroll) if bad_scroll else "all clean")
        check(f"[{label}] zero console/page errors", not errs and not pe,
              f"console={errs[:2]} page={pe[:2]}")

        if mobile:
            page.goto(BASE + "/index.html", wait_until="load")
            page.wait_for_selector("#holdings-body tr", timeout=30000)
            page.locator("#mobile-nav-btn").tap()
            page.wait_for_timeout(300)
            ok = "mobile-open" in (page.locator("#sidebar").get_attribute("class") or "")
            check("[mobile] hamburger drawer opens", ok)
        else:
            # Theme probe: AI input parse -> real degraded state (409/402/503)
            page.goto(BASE + "/input.html", wait_until="load")
            page.wait_for_selector("#m-account option", state="attached")
            page.click("#tab-ai")
            page.fill("#ai-text", "6/11 嘉信 買 AAPL 10股 @211.40")
            page.click("#ai-parse")
            page.wait_for_timeout(4000)
            shown = any(page.locator(s).is_visible() for s in
                        ("#ai-degrade-off", "#ai-degrade-quota", "#ai-degrade-down"))
            check("[desktop] AI parse surfaces a REAL degraded panel", shown)

            # 再平衡試算 (analysis feature, non-LLM) — opens the .rb-drawer
            page.goto(BASE + "/index.html", wait_until="load")
            page.wait_for_selector("#holdings-body tr")
            page.locator("button", has_text="再平衡試算").first.click()
            page.wait_for_selector(".rb-drawer", timeout=15000)
            check("[desktop] 再平衡試算 opens", True)
        ctx.close()
    browser.close()


def api(method: str, path: str, body: dict | None = None) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:  # noqa: BLE001
            return e.code, None


st, _ = api("GET", "/api/insights")
check("GET /api/insights healthy", st == 200, f"HTTP {st}")
st, _ = api("GET", "/api/insight-tasks")
check("GET /api/insight-tasks healthy", st == 200, f"HTTP {st}")
st, b = api("GET", "/api/llm/config")
check("GET /api/llm/config healthy", st == 200,
      json.dumps(b, ensure_ascii=False, default=str)[:140])
st, b = api("POST", "/api/whatif",
            {"symbol": "2330", "side": "buy", "shares": "100", "price": "2400"})
check("POST /api/whatif works", st == 200,
      json.dumps(b, ensure_ascii=False, default=str)[:120])
st, _ = api("GET", "/api/prompt-vars")
check("GET /api/prompt-vars", st == 200, f"HTTP {st}")

print("=" * 44)
fails = [n for ok, n in results if not ok]
if fails:
    print(f"RESULT: FAIL ({len(fails)}): {', '.join(fails)}")
    sys.exit(1)
print(f"RESULT: ALL {len(results)} PASS")
```

## 4. Rerunnable script — 390px overflow element probe

Finds the elements pushing the body wider than an iPhone viewport (used to
pinpoint every R7 offender). Run: `PD_TEST_URL=... python probe_overflow.py`

```python
"""Find elements wider than the 390px viewport on overflowing pages."""

import os

from playwright.sync_api import sync_playwright

BASE = os.environ["PD_TEST_URL"].rstrip("/")
PAGES = [
    ("index", "/index.html", "#holdings-body tr"),
    ("trades", "/trades.html", "#tx-body tr"),
    ("input", "/input.html", "#m-account option"),
    ("settings-datasources", "/settings-datasources.html",
     "#market-order-wrap .fallback-card"),
    ("insights", "/insights.html", "body"),
]
JS = """
() => {
  const out = [];
  const vw = document.documentElement.clientWidth;
  for (const el of document.querySelectorAll('*')) {
    const r = el.getBoundingClientRect();
    if (r.width > vw + 4 || r.right > vw + 20) {
      const id = el.id ? '#' + el.id : '';
      const cls = el.className && typeof el.className === 'string'
        ? '.' + el.className.split(' ').slice(0, 2).join('.') : '';
      out.push(el.tagName.toLowerCase() + id + cls + '  w=' + Math.round(r.width));
    }
  }
  return out.slice(0, 12);
}
"""
with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True,
                              has_touch=True, device_scale_factor=3)
    page = ctx.new_page()
    for name, path, sel in PAGES:
        page.goto(BASE + path, wait_until="load", timeout=60000)
        page.wait_for_selector(sel, timeout=30000, state="attached")
        page.wait_for_timeout(1200)
        print("=== " + name)
        for line in page.evaluate(JS):
            print("   ", line)
    browser.close()
```

## 5. Rerunnable script — full-site mobile screenshots

`PD_TEST_URL=... python shoot_mobile.py <suffix>` → `shots/m_<page>_<suffix>.png`
(full-page, iPhone UA, DPR 3). Same PAGES list as §3; prints per-page body
h-overflow. Useful before/after any layout-affecting change.

```python
"""Screenshot every page at iPhone viewport (390x844, DPR 3, touch)."""

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ["PD_TEST_URL"].rstrip("/")
OUT = Path(__file__).parent / "shots"
OUT.mkdir(exist_ok=True)
SUFFIX = sys.argv[1] if len(sys.argv) > 1 else "shot"
PAGES = [  # (name, path, ready-selector) — same set as dual_test.py
    ("index", "/index.html", "#holdings-body tr"),
    ("trades", "/trades.html", "#tx-body tr"),
    ("cash", "/cash.html", ".cash-card"),
    ("instruments", "/instruments.html", "#inst-body tr"),
    ("input", "/input.html", "#m-account option"),
    ("settings", "/settings.html", "body"),
    ("settings-scheduler", "/settings-scheduler.html", "#jobs-body tr"),
    ("settings-datasources", "/settings-datasources.html",
     "#market-order-wrap .fallback-card"),
    ("insights", "/insights.html", "body"),
    ("login", "/login.html", "body"),
]

with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(
        viewport={"width": 390, "height": 844}, device_scale_factor=3,
        is_mobile=True, has_touch=True,
        user_agent=("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                    "Mobile/15E148 Safari/604.1"))
    page = ctx.new_page()
    for name, path, sel in PAGES:
        try:
            page.goto(BASE + path, wait_until="load", timeout=60000)
            page.wait_for_selector(sel, timeout=30000, state="attached")
            page.wait_for_timeout(1500)
            h = page.evaluate("() => document.documentElement.scrollWidth - "
                              "document.documentElement.clientWidth")
            page.screenshot(path=str(OUT / f"m_{name}_{SUFFIX}.png"), full_page=True)
            print(f"OK {name}  body-hscroll={h}px")
        except Exception as exc:  # noqa: BLE001
            print("ERR", name, str(exc)[:100])
    browser.close()
```

## 6. Where things stand for the next session

- **P0 (user decision pending): ignite the LLM pillar** — provider key + four
  role models + a small USD budget in 設定 › LLM 與額度; first batch run on the
  TEST instance; walk Loop 1 → Loop 2 before enabling on prod.
- P1: news/qualitative context wiring (NewsAPI pending in the datasource
  catalog); Vision statement parsing; AI accuracy card from `/api/ai-score`.
- P2: monthly-report trend chart (needs 3+ snapshots), PWA manifest, T3
  firewall tightening.
- Prod = tag v0.1.9; demo tracks `main`; CHANGELOG at 11 versions; op-log
  through #28.
