---
name: demo-cycle
description: "Spec-by-demo development loop for portfolio-dash: propose a feature, audit the whole site, fix what the audit found, and demo where the fix diverged from the guess — each phase delivering ONE self-contained interactive HTML the owner can click. Use to propose/spec a new feature, run a full-site functional + wiring + money + layout + system audit, review or debug a reported defect, or produce before/after evidence for a fix. Invoke with /demo-cycle [propose|audit|fix|rootcause]."
---

# Demo Cycle

The owner does not read specifications; the owner clicks demos. Every phase of this
loop ends in **one self-contained HTML file** whose claims are measured or checked in
front of the reader — never asserted in prose. Prose is the annotation; the demo is the
spec.

The loop exists because of a measured failure: the 2026-07-25 audit got the **symptom**
right on all 13 findings and the **root cause** wrong on four, because those four were
derived by reading code. `tools/probe_live.py` is the correction — it measures the
running app instead.

> **Owner-facing manual: `manual.html`** (open it in a browser) — the same material as
> this file plus five runnable labs: a phase picker, a live cascade upset, the wiring
> verdict ladder, overflow-vs-legitimate-scroll, and the lying-verdict trap. Rebuild it
> after editing `manual.body.html` / `manual.demo.js`:
>
> ```powershell
> .\.venv\Scripts\python.exe .claude\skills\demo-cycle\tools\new_report.py --kind manual `
>   --slug manual --title "/demo-cycle 使用手冊" `
>   --body .claude\skills\demo-cycle\manual.body.html `
>   --demo-js .claude\skills\demo-cycle\manual.demo.js `
>   --out .claude\skills\demo-cycle\manual.html --force
> .\.venv\Scripts\python.exe .claude\skills\demo-cycle\tools\verify_report.py `
>   .claude\skills\demo-cycle\manual.html
> ```

## Phases

| Phase | What it answers | Artifact (committed) |
| --- | --- | --- |
| `propose` | *Should we build this, and exactly what?* | `docs/spec/<date>-<slug>.html` |
| `audit` | *What is broken across the whole site?* | `docs/audit/<date>-<slug>.html` |
| `fix` | *Is it actually fixed, provably?* | `docs/audit/<date>-<slug>.html` |
| `rootcause` | *Where did the fix diverge from the audit's guess?* | `docs/audit/<date>-<slug>.html` |

Run a phase on its own. `audit → fix → rootcause` is the usual chain; `rootcause` is
**skipped when nothing diverged** (and the `fix` report then says so explicitly — an
empty divergence section is information, not an omission).

`propose` never proceeds to implementation until the owner has confirmed the demo. That
confirmation is the acceptance list.

## Model gate — do this first

The owner's standing instruction is **reviewer = Opus 5, highest reasoning**. This skill
cannot switch models itself, so at the start of any phase:

1. State the current model and effort. If it is not Opus 5 at `xhigh`, ask the owner to
   run `/model` before continuing — do not audit at a lower tier and call it an audit.
2. `--fanout` (opt-in, `audit` only): dispatch one subagent per audit dimension via the
   Agent tool with an explicit `model:` override, then synthesise. Without `--fanout`,
   the main session does every dimension serially. Never fan out unasked.

## Setup — a disposable instance to measure

Never probe prod, and never probe your working DB. Seed a throwaway one (synthetic data,
`scripts/seed_demo.py` refuses to double-seed):

```powershell
$w = "$env:TEMP\dc"; New-Item -ItemType Directory -Force $w | Out-Null
$env:DB_PATH = "$w\probe.db"; $env:PD_DISABLE_SCHEDULER = "1"; $env:PYTHONIOENCODING = "utf-8"
.\.venv\Scripts\python.exe scripts\seed_demo.py

$p = Start-Process -PassThru -WindowStyle Hidden -WorkingDirectory (Get-Location) `
  -FilePath ".\.venv\Scripts\python.exe" `
  -ArgumentList "-m","uvicorn","portfolio_dash.api.app:create_app","--factory","--host","127.0.0.1","--port","8477" `
  -RedirectStandardOutput "$w\uvicorn.out.log" -RedirectStandardError "$w\uvicorn.err.log"
$p.Id | Set-Content "$w\uvicorn.pid"

# Poll — do NOT `Start-Sleep 3` and assume. First boot bootstraps the DB and takes
# longer than a fixed guess; a bare request right after start gets "connection refused".
for ($i=0; $i -lt 40; $i++) {
  try { (Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8477/api/health).Content; break }
  catch { Start-Sleep -Milliseconds 500 }
}   # -> {"status":"ok","version":"0.1.24",...}
```

Stop it with `Stop-Process -Id (Get-Content "$w\uvicorn.pid") -Force`. Start it
**detached** like this, not as a harness background job — long-lived jobs get killed
mid-run (`LESSONS_LEARNED.md`, 2026-07-24).

## Measure — never guess (`tools/probe_live.py`)

Three modes, all read-only except `--click`. Run from the repo root:

```powershell
# overflow / clipped text / console errors / full-page screenshots, per page × width
.\.venv\Scripts\python.exe .claude\skills\demo-cycle\tools\probe_live.py layout `
    http://127.0.0.1:8477 --pages "/,/data-center.html,/ledger.html" --widths 390,1257 --out $w\probe

# which CSS declaration ACTUALLY wins — the answer no amount of reading produces
.\.venv\Scripts\python.exe .claude\skills\demo-cycle\tools\probe_live.py css `
    http://127.0.0.1:8477 --selector ".kpi-band" --prop grid-template-columns --width 1257

# every interactive control vs whether any JS addresses it; --click makes it decisive
.\.venv\Scripts\python.exe .claude\skills\demo-cycle\tools\probe_live.py wiring `
    http://127.0.0.1:8477 --pages "/ledger.html" --click
```

`css` prints every matching declaration in source order with its specificity, marks the
winner, flags rules whose `@media` condition is false at that width, and names the trap
when it applies (*"loses on SOURCE ORDER — editing it cannot fix this"*, *"media queries
add no specificity"*). `wiring` grades each control **wired / weak / SUSPECT** from the
JS sources, and `--click` resolves the leads by clicking each on a fresh page and
recording navigation, requests, DOM mutations and dialogs — `DEAD? … nothing happened`
is the only evidence that justifies calling a button dead. `--click` **mutates state**:
disposable instances only.

A finding goes in a report only with its measurement attached. Anything derived by
reading code is labelled **推測待驗** in the report, so the owner knows before approving
which conclusions may deform during implementation.

## Build the report (`tools/new_report.py` → `tools/verify_report.py`)

```powershell
.\.venv\Scripts\python.exe .claude\skills\demo-cycle\tools\new_report.py `
    --kind spec --slug drip-preview --title "DRIP 預覽 — 規格 demo"

.\.venv\Scripts\python.exe .claude\skills\demo-cycle\tools\verify_report.py `
    docs\spec\2026-07-27-drip-preview.html --shots $w\shots
```

`new_report.py` emits the phase's section skeleton plus **one lab that already runs** —
a live iframe sandbox with a before/after switch, a width slider, measured readouts and a
verdict — and inlines `web/format.js`, so a demo formats money with the app's own exact
Decimal-string formatter. Edit the working lab; never assemble one from prose.

`verify_report.py` opens the file in chromium, clicks **every mode of every lab**, and
fails on: console/page errors, a failing `PD.check()`, a readout left unmeasured (the
sandbox never posted back), an unclickable mode, an `http(s)://` asset reference, page
overflow at 390px or 1440px, or a dead TOC anchor. A report that has not passed this is
not shown to the owner.

### Rules a demo must satisfy

1. **Real mechanism, not a picture.** CSS questions run in an iframe so the browser
   resolves the cascade; behavioural questions run the app's own JS. Screenshots are
   supporting evidence, never the demo.
2. **Before *and* after, switchable.** The owner must be able to flip between them and
   watch the measured numbers change.
3. **Self-check anything recomputed offline.** A lab that reimplements app logic must
   `PD.checkEq(name, mine, engineValue)` against a figure the **real engine** produced
   (an `/api/*` response, a pytest fixture, a stress-audit oracle line). The strip
   renders in the report; `verify_report.py` fails on a false check.
4. **Money is never a JS float** (CLAUDE.md invariant 3). Print backend Decimal strings,
   or compute in integer minor units and self-check the result.
5. **The verdict depends on the mode, not only on the measurement** — otherwise a width
   that happens not to trigger the defect makes the "before" state announce success.
6. **One file.** A folder of files is allowed only when a single document genuinely
   cannot carry the demo; say why in the report.

## Audit coverage (the `audit` phase checklist)

The owner's standing scope — every item gets a measured verdict, including the ones that
pass:

- **功能流程** — each user flow end to end on the running instance.
- **按鈕接線** — every button/control: `wiring` + `--click` on anything not decisively wired.
- **金額與成本計算** — cost basis, realized/unrealized P&L, fees/tax, dividends, FX pool,
  returns/XIRR reconciled against an independent oracle. This is `/stress-audit`'s job —
  **run it, do not re-derive it here**, and cite its `fail=0`.
- **前端顯示數字** — every displayed figure traced to an API Decimal string; sign,
  percent base, and currency label checked (v0.1.24 H1 was a sign flip).
- **排版** — `layout` at 390/768/1257/1440; distinguish real overflow from a table
  legitimately scrolling inside `overflow-x:auto`.
- **系統級** — console/page errors, graceful degradation on stale/missing data, `mypy
  --strict`, `ruff`, the regression suite.

Close every audit with **passing items** as well as gaps; a report that lists only
problems misrepresents the system.

## Gotchas

- **Run these tools from PowerShell.** Git Bash (MSYS) rewrites a leading-`/` argument
  into a Windows path — `--pages /` arrived as `/C:/Program Files/Git/` and silently
  probed the wrong URL. `MSYS_NO_PATHCONV=1` also works.
- **`PYTHONIOENCODING=utf-8` or the output dies.** Traditional Chinese through the
  default cp1252 console raises `UnicodeEncodeError` mid-run.
- **`@media` rules are in `sheet.cssRules` whether or not they apply.** The first version
  of `css` mode reported a 760px rule as the winner at 1257px. It now filters on
  `matchMedia`. Any tool you write against `styleSheets` has this bug until it doesn't.
- **A wiring haystack must exclude markup.** Searching `*.html` too made every control
  match its own `id="…"` — 100% wired, zero signal. Only `.js` files and inline
  `<script>` bodies count as behaviour.
- **Runtime `dataset` writes fake a wiring hit.** `el.dataset.seen = '1'` made every
  button look dispatched-on; short values are now ignored and the key must be read in code.
- **Equal specificity, later rule wins — including inside this kit.** `lab.css`'s light
  `.stage` override sat in the token block at the top and was silently overridden by the
  base rule 40 lines below. The comment there says keep them adjacent; believe it.
- **Computed selectors defeat any literal search — by design, not by accident.** This
  app binds its tabs with `$('#tab-' + t)` (`web/input.js`), so `'tab-csv'` appears
  nowhere and all five tabs come back SUSPECT. That is the *expected* output, and it is
  precisely why `--click` exists: it cleared five of the six instantly.
- **An already-active tab or toggle legitimately reports `NO EFFECT`.** `tab-manual` is
  the default pane, so clicking it re-applies the same classes and mutates nothing.
  Before reporting a `DEAD?`, switch away and click it again.
- **Detection power is not optional.** `wiring` claimed a clean sweep until it was run
  against a fixture with two deliberately dead buttons. Build the fixture before you
  trust the sweep.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `UnicodeEncodeError: 'charmap'` | `$env:PYTHONIOENCODING="utf-8"` |
| Probe reports a page that isn't yours (`/C:/Program Files/Git/`) | Use PowerShell, not Git Bash |
| `selector matched nothing` | The element is rendered by JS after load — check the page path, or probe a selector that exists at rest |
| `verify_report.py`: *readout never measured* | The lab's `probe` expression threw inside the iframe; it is swallowed by design. Simplify it and re-verify |
| `verify_report.py`: *external asset reference* | Inline it (base64 the image, paste the CSS). Reports must open offline |
| uvicorn exits immediately | Read `$w\uvicorn.err.log`; usually the port is taken or `DB_PATH` points somewhere unwritable |
| Every control comes back `wired` | The haystack is wrong (see gotchas) — verify against a dead-button fixture before believing it |

## Closing a phase

- `propose` → owner confirms on the demo; the acceptance table becomes the spec.
- `audit` → findings ranked H/M/L with measured symptoms and labelled root-cause confidence.
- `fix` → every item carries evidence; gates green; divergences recorded in the report,
  in `CHANGELOG.md` (a "differs from the audit's guess" note), and — when the divergence
  is a *class* of mistake, not an instance — one entry in `LESSONS_LEARNED.md`.
- Then `/ship-version`. Money-of-record changes also require `/stress-audit` green.
