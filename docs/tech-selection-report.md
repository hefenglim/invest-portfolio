> **⚠️ HISTORICAL SNAPSHOT — SUPERSEDED.**
> This was the technology-selection report at the point of first review (2026-06-04).
> It predates the multi-account / multi-currency finalization (decisions D-1…D-5 and
> Q9…Q13) and the FX-conversion ledger. **The authoritative, current source of truth
> is `CLAUDE.md` + `.claude/rules/`** (especially `domain-ledger.md` and
> `markets-and-fees.md`). Kept only as a record of how the design was reasoned out.

---

# Technology Selection & Environment Readiness Report
### Project: Personal Stock Portfolio Dashboard (`portfolio-dash`)
### Status: For review — tech selection NOT yet confirmed
### Date: 2026-06-04

---

## 1. Purpose

This report consolidates the technology selection, architecture, precision model,
hosting evaluation, and development-environment readiness for review. Its goal is a
single sign-off gate: once the open decisions in §10 are confirmed, the project
enters the specification phase. Nothing is implemented before that confirmation.

---

## 2. Requirements (recap)

| Aspect | Decision |
| --- | --- |
| Users | 1–2 (personal) |
| Volume | < 200 transactions/month (< ~2,400 rows/year) |
| Markets | US equities + TW equities |
| Core metrics | Cost basis, realized/unrealized P&L, return rate, sector allocation |
| Input | Manual transaction entry (+ optional CSV import) |
| Data fetch | System fetches market quotes + qualitative (sector/news/market) info |
| LLM role | **Batch** (manual or scheduled) generation of structured insight cards/reports |
| Strategy | User-defined strategy logic |
| Presentation | Visual dashboard |
| Implementation model | AI-implemented by Claude Code from human specs; spec-first |

---

## 3. Technology selection (proposed — locked pending sign-off)

Single-language **Python 3.12 monolith**. No frontend/backend split.

| Concern | Choice | Rationale |
| --- | --- | --- |
| Language | Python 3.12 | Most idiomatic language for LLM code-gen; one language = smallest error surface |
| Web framework | FastAPI | Async, Pydantic-native, low boilerplate |
| Templating | Jinja2 | Server-rendered HTML, no build step |
| HTML interactivity | HTMX | Server round-trips; no SPA state to drift |
| Client micro-interaction | Alpine.js | Tabs/filters/toggles without a framework |
| Charts | ECharts (via CDN) | Visual quality = chart lib + CSS, framework-independent |
| Storage | SQLite | Tiny data volume; zero-ops; single file |
| Math / dataframes | pandas, numpy | Idiomatic financial computation |
| Returns | numpy-financial + XIRR helper | Periodic IRR built-in; irregular cashflows need XIRR |
| Money type | `decimal.Decimal` | Never float for currency (see §5) |
| LLM gateway | LiteLLM | One OpenAI-format call across all providers |
| Scheduling | APScheduler (in-process) | No broker/extra service needed at this scale |
| Models / validation | Pydantic v2 | Shared across API, DB layer, LLM I/O |
| Type checking | mypy (strict) | Compile-time guardrail for an AI-implemented codebase |
| Tests | pytest + httpx | Unit + route-level; HTML assertions for HTMX |
| Packaging | single Docker image | Small footprint; runs on 1 GB VM or NAS |

### Settled "why not"
- **Not React/Next.js/SPA.** Dashboard is read-heavy + periodic refresh + form input
  + **batch** insight cards — not a real-time terminal. "Visually rich" comes from
  ECharts + CSS, which need no JS framework. Only reopen if the LLM feature becomes a
  *streaming chat as the primary surface* (it is not).
- **Not DuckDB yet.** SQLite covers this volume; adding DuckDB now is a spare part.
- **Not Celery/Redis.** APScheduler in-process suffices for 1–2 users.
- **Not Postgres.** Single instance, single writer, tiny data — SQLite wins on ops.

---

## 4. Architecture & module decomposition

Monolith, internally layered. One-way dependency direction:

```
web_ui  ─┐
         ├─►  portfolio  ──►  pricing  ──►  shared
strategy ─┘        │           │             ▲
                   └─►  data_ingestion  ──────┘
llm_insight ──► portfolio (reads computed numbers) ──► shared
scheduler  ──► pricing, llm_insight  (triggers only)
```

| Module | Responsibility |
| --- | --- |
| `shared/` | settings, DB session, Pydantic models, Decimal/currency + FX helpers |
| `data_ingestion/` | manual + CSV transaction entry, validation/normalization |
| `pricing/` | quotes + FX → SQLite, idempotent upserts, scheduled refresh |
| `portfolio/` | **core calc**: cost basis, P&L, returns, sector mix (pure, testable) |
| `strategy/` | user strategies as parameterized Python modules (pure, pytest) |
| `llm_insight/` | LiteLLM orchestration → structured cards, cached |
| `web_ui/` | FastAPI routes + Jinja2/HTMX/Alpine/ECharts (thin; renders, never computes) |
| `scheduler/` | APScheduler jobs only (no business logic) |

Invariant: **calculation lives in `portfolio/`** — never in routes or templates.
Lower layers never import `web_ui`.

---

## 5. Numeric & currency precision model  ⚠️ NEEDS YOUR SIGN-OFF

**Principle: `Decimal` end-to-end, never `float`.** Beyond that, "decimal places"
must be split into two independent concepts — conflating them loses precision:

### 5a. Monetary amount precision (per currency minor unit)
Applies to cost, market value, P&L, fees, taxes.

| Currency | Minor unit | Decimals |
| --- | --- | --- |
| USD | cent | **2** |
| TWD | dollar (元) | **0** (whole NT$) |

TWD amounts settle to whole NT dollars — gross consideration, brokerage fee
(0.1425%, NT$20 min), and securities transaction tax (0.3%) all round to integer NT$.
**This matches your statement and is confirmed.**

### 5b. Price (quote) precision (per market tick) — CORRECTION
**TW stock *prices* are NOT integers.** TWSE uses a multi-tier tick scale:

| TWD price range | Tick | Decimal places |
| --- | --- | --- |
| < 10 | 0.01 | 2 |
| 10 – 50 | 0.05 | 2 |
| 50 – 100 | 0.10 | 1 |
| 100 – 500 | 0.50 | 1 |
| 500 – 1000 | 1 | 0 |
| ≥ 1000 | 5 | 0 |

Most TW stocks trade in the 5–50 range (tick 0.05), so a price like `23.45` or
`38.50` is normal. Storing TW price as an integer would corrupt the recorded price
and therefore every cost/value/P&L derived from it. ETFs use a finer scale (0.01
under 50). US prices use 2 decimals.

### 5c. Resulting storage rule (proposed)
- Store all numerics as `Decimal` (serialized TEXT or scaled integer — one convention
  per column, documented).
- **Price** columns keep market-appropriate decimals (US 2dp; TW up to 2dp).
- **Amount** columns quantize to the currency minor unit (USD 2dp; TWD 0dp) with an
  explicit, stated rounding mode (e.g. NT$ amounts rounded per settlement convention).
- Share **quantity** = integer (US fractional shares deferred unless required).
- All FX conversion goes through the single `shared/` helper; every figure states its
  currency.

> **Decision needed (D-1):** confirm this split — TWD *amounts* = whole-dollar (your
> spec), but TW *prices* retain tick-level decimals (my correction).

---

## 6. Hosting / server evaluation (recap)

| Option | Verdict |
| --- | --- |
| Synology NAS + Cloudflare Tunnel / Tailscale | **Preferred** — owned hardware, more RAM, no egress meter, data stays home, tunnel avoids raw port-forward risk, CGNAT, and dynamic IP |
| Synology NAS + raw port forwarding | Not recommended — NAS are ransomware targets; fails entirely under CGNAT |
| GCP e2-micro | Fallback — clean isolation from home network, but 1 GB RAM, US-only free region (latency from TW), 1 GB/month free egress, and external IPv4 now incurs a small monthly charge |

The Python monolith keeps **both** options viable (low footprint). Final choice is
gated on one input:

> **Decision needed (D-2):** does your home broadband get a routable public IP, or are
> you behind CGNAT? (Cloudflare Tunnel/Tailscale make this moot if you accept a tunnel.)

---

## 7. Development environment & prerequisites

### 7a. Existing environment (continuation, not new setup)
- OS: Windows 11 · Python 3.12 · timezone UTC+8
- LiteLLM already installed and in use (OpenRouter / OpenAI-compatible / Anthropic)
- Claude Code workflow established (`resume-dev` / `ship-version` conventions)

### 7b. Project scaffolding — DELIVERED
- `CLAUDE.md` (root index, locked decisions, module map)
- `.claude/rules/` — `stack.md`, `architecture.md`, `data-and-pricing.md`,
  `llm-insight.md`, `engineering-process.md`

### 7c. Proposed dependency set (to install at project init)
Runtime:
- `fastapi`, `uvicorn[standard]`, `jinja2`, `python-multipart`
- `pydantic`, `pydantic-settings`
- `pandas`, `numpy`, `numpy-financial`, plus an XIRR helper (`pyxirr` or `scipy`)
- `litellm`
- `apscheduler`
- `httpx`
- pricing client(s): `yfinance` and/or `FinMind` — *pending §8 validation*

Dev:
- `mypy` (strict), `ruff` (lint+format), `pytest`, `pytest-asyncio`

Frontend assets:
- ECharts via CDN (no Python dependency, no build step)

### 7d. Environment notes
- All deps ship Windows wheels (numpy/pandas included) — no compiler needed.
- **Timezone discipline:** dev box is UTC+8, but markets span US/Eastern and TW.
  Store timestamps in UTC; convert at display; the scheduler must be market-timezone
  aware (US post-market ≠ TW post-market).
- Single `Dockerfile` for the chosen deploy target (NAS Container Manager or e2-micro).

---

## 8. Data source plan (pending validation before lock)

| Need | Candidate(s) | Note |
| --- | --- | --- |
| US prices | yfinance → keyed API fallback (Finnhub/Alpha Vantage/Polygon) | yfinance reliability to be verified |
| TW prices | yfinance `2330.TW` / FinMind / TWSE+TPEx OpenAPI | confirm coverage & decimals per §5b |
| FX (USD/TWD) | same provider where possible, else dedicated FX feed | needed for combined view |

> **Decision needed (D-3):** lock the data sources after a short availability/reliability
> probe (recommended as the first spec-phase task — quotes are the correctness foundation).

---

## 9. Readiness status

| Item | Status |
| --- | --- |
| Stack selection | ✅ Defined (sign-off pending) |
| Architecture & module map | ✅ Defined |
| `CLAUDE.md` + `.claude/rules/` bundle | ✅ Delivered |
| Precision model | ⚠️ Proposed — needs D-1 |
| Hosting choice | ⏳ Pending D-2 |
| Data sources | ⏳ Pending D-3 (validation) |
| Dev dependencies | ✅ Specified, not yet installed |
| Cost-basis method default | ⏳ Pending D-4 |
| Reporting currency | ⏳ Pending D-5 |

---

## 10. Open decisions requiring your sign-off

- **D-1 — Precision model (§5):** confirm TWD *amounts* = whole-dollar, TW *prices*
  retain tick-level decimals.
- **D-2 — Hosting (§6):** public IP vs CGNAT; accept a tunnel (Cloudflare/Tailscale)?
- **D-3 — Data sources (§8):** approve a validation probe as the first spec task.
- **D-4 — Cost-basis method:** default FIFO or weighted-average? (TW practice commonly
  uses weighted-average / 移動平均成本; US brokers often FIFO or specific-lot.)
  Per-holding override?
- **D-5 — Reporting currency:** combined view in TWD, USD, or both (toggle)?

---

## 11. Recommended next step

1. You confirm D-1…D-5 above.
2. Fold confirmed decisions into `.claude/rules/data-and-pricing.md` (precision model)
   and `CLAUDE.md`.
3. Enter spec phase starting with **`portfolio/`**: cost-basis and return definitions
   (FIFO/avg, realized/unrealized, XIRR) — the correctness core of the whole app —
   followed immediately by the §8 data-source validation probe.

_No implementation begins until §10 is signed off._
