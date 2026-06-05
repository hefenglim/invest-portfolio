# Rule: Claude Design → Claude Code Handoff

How the dashboard's visual layer is produced and integrated. Read before building or
integrating any `web_ui/` template that originates from a Claude Design export.

## What Claude Design does (and does not) do here

- It produces **vanilla HTML / CSS / JS** for the dashboard's look (layout, styling,
  ECharts visuals). **Front-end only.**
- It does **not** touch the backend, ledger, calculation, or data fetching. The hard
  parts of this app (`portfolio/`, `forex/`, pricing, multi-account, XIRR) are out of
  its scope entirely.

## Sequencing

1. **Backend / calc core first** — build and test `portfolio/`, `forex/`, the data
   layer, and computed result shapes. This is the correctness foundation.
2. **Design** — produce the dashboard visual from the report sections (holdings,
   realized/unrealized P&L, dividends, sector allocation, FX, insight cards,
   ex-dividend calendar). Style: dense, data-first, thousands separators.
3. **Integrate** — hand the export to Claude Code and re-express it in the locked
   stack.

## Integration rules (on handoff)

- Convert the static HTML into **Jinja2 templates** under `web_ui/`.
- Wire **HTMX** endpoints + **Alpine.js** + **ECharts** to the **real computed data**
  from the backend. The web layer stays **thin** — no calculation or data-fetching
  logic in templates (`architecture.md`).
- Map Design's color/typography/spacing into the project's CSS / design tokens once,
  then reuse.

## Guardrail — do not let the stack drift

- Design output being plain HTML/JS is **not** a license to introduce **React / Next /
  any SPA framework** or a build step. Keep the locked **Jinja2 + HTMX + Alpine + ECharts
  (CDN)** stack (`stack.md`).
- Treat the Design export as a **starting template, not final code** (it is a research
  preview with known quirks). Refactor it to fit the stack, rules, and real data.
