# Multi-user prep — Phase 0 (DB-open guardrail + target shape)

Date: 2026-07-18 · Decision: **FU-D39** (r4 mini-spec) · Source: research pack
`docs/reports/2026-07-17-r3-research-pack.md` §R-1. · **Revised 2026-07-19 (owner
sign-off, r6):** per-user data lives in a per-user FOLDER, not a flat ledger file —
see the target-shape table.

This batch implements **Phase 0 only**: a guardrail that pins today's connection-open
surface, plus this note. The physical `market.db` split (**Phase 1**) is **deferred** to
its own dedicated batch after v0.1.20 — it touches the pricing-read seams that feed
valuation, and bundling it with a large feature batch is a risk mismatch. **No behaviour
changes here** (tests + docs only).

## Target three-DB shape (design; not built here)

Per §R-1, today's single SQLite file splits into distinct files, each with one clear
writer, alongside the `news.db` that **already** lives in its own file:

| File | Role | Contents (indicative) | Writer |
| --- | --- | --- | --- |
| `control.db` (central) | user registry / routing | `auth_users`, `auth_sessions`, user→**folder** map, shared operational keys | admin/auth |
| `user_trade/<UserLoginID>/ledger.db` (personal) | one **folder** per user | `transactions`, `dividends`, `fx_conversions`, `opening_inventory`, `cash_movements`, `accounts`, `instruments`, AI records, system logs, snapshots, most config | that user's ledger flows |
| `market.db` (shared, one copy) | market data fetched once | `prices`, `fx_rates`, `dividend_events`, `external_snapshots` | the scheduler (single writer) |
| `news.db` (shared — **already exists**) | organized news | `organized_news`, `news_mentions` | news pipeline |

**Owner revision (2026-07-19, r6 sign-off) — per-user FOLDER, not a flat file.** Each
user's ledger and every user-derived artifact (cost calculations, extended per-user
records, logs, backups of that ledger) live together under `user_trade/<UserLoginID>/`,
so the folder is the **unit of backup and restore** — copying one directory captures one
user completely, and no cross-user data can hide outside it. This slots cleanly into the
existing convention that logs/backups derive from `db_path.parent`: point a user's
`db_path` at `user_trade/<id>/ledger.db` and the per-user folder layout falls out of the
current code with no new path logic. `control.db` maps user → folder (not user → file).

Load-bearing corrections from the study: `auth_*` must live in **`control.db`**, not in a
per-user ledger (you need the user to find the ledger to find the user — a chicken-and-egg
deadlock). Reads that need both personal + market data use **separate connections joined in
Python** (the calc layer already takes prices/FX as inputs), **not** SQLite `ATTACH` — that
keeps the "market.db is read-only for readers" guarantee crisp. `news/store.py` is the
existing precedent proving the separate-file pattern works.

## What Phase 0 pins, and why

The split stays cheap only while the set of places that *open a DB connection* is tiny and
known. Today the entire direct-open surface is **5 opens across 3 files**:

| File | Direct opens | Justification |
| --- | --- | --- |
| `portfolio_dash/shared/db.py` | 1 | THE choke-point. `get_connection()` / `session()` open the per-request personal connection; all personal DB access routes here. |
| `portfolio_dash/news/store.py` | 1 | The separate-DB precedent (`news.db` opened lazily). This is the template Phase 1's `market.db` accessor will mirror. |
| `portfolio_dash/ops/backup.py` | 3 | Low-level backup/integrity utility (stdlib + `shared` only). Uses the sqlite3 **online-backup API** (`Connection.backup`, needs a raw src + dst handle) and `PRAGMA integrity_check` over an arbitrary `db_path`, so it legitimately opens raw handles and cannot route through `session()`. `TODO(market-db-split)`: a later phase adds `market.db`/`control.db` as additive backup targets. |

The guardrail — `tests/architecture/test_db_open_surface.py` — statically **tokenizes**
every `portfolio_dash/**/*.py` module and counts the qualified opener 4-grams
`sqlite3.connect(` and `sqlite3.Connection(`, asserting the `(file, count)` set matches
that pinned allow-list exactly. Tokenizing (rather than plain text search) means the
literal `sqlite3.connect(` appearing in a **comment or string/docstring is ignored by
construction**, and a bare type annotation `sqlite3.Connection` (no call parens) never
matches — the scan has zero false positives on the current tree and is not brittle to
prose. A NEW direct open anywhere else fails the test with an actionable message pointing
the developer back to the session helpers (or to a conscious, justified allow-list
extension). Each allow-list entry carries a one-line justification inline.

**Scope note (kept honest in the test):** only the qualified `sqlite3.connect/Connection`
forms exist today. If a future change introduces another opener — a bare
`from sqlite3 import connect` alias, or a different DBAPI library (`aiosqlite`, `apsw`) —
the scan's `_OPENER_ATTRS` must be extended and this note updated.

## The invariant new code must uphold

> Every **personal** database access goes through `portfolio_dash.shared.db`
> (`get_connection` / `session`) or `api.deps.get_conn`; **news** goes through
> `portfolio_dash.news.store` (`news_session`). No module opens a raw
> `sqlite3.connect(...)` outside the pinned allow-list.

Keeping the surface at this single personal choke-point is exactly what makes Phase 1/2
cheap: a `db_path_for(request)` resolver (Phase 2) then has **one** seam to route through,
and the `market.db` accessor (Phase 1) is added as a second, `news/store.py`-shaped opener
rather than as a scattered rewrite.

## Deferred — Phase 1 (next dedicated batch)

Physically split the market tables (`prices`, `fx_rates`, `dividend_events`,
`external_snapshots`) into `market.db` via a new module-local accessor modelled on
`news/store.py` — `market_db_path()` + `market_session()` + a `create_tables` mirroring
`pricing/schema.py` — initially pointing at the *same* file so the change is a path change,
not a query rewrite. Dashboard/valuation reads then open **both** the personal ledger and
`market.db` (separate connections, market opened read-only), joining in Python. Still
single-user; highest-value / lowest-risk step, useful even for one user (smaller personal
DB, independent market-data backup/retention). Phases 2–4 (`control.db` + `db_path_for`
resolver; multi-ledger + scheduler union-fetch fan-out; hardening) remain design-on-paper
until a real second user exists — see §R-1 for the full phased path and risk register.
