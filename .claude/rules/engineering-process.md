# Rule: Engineering Process

Carried over from prior project discipline. These are process invariants for an
AI-implemented codebase.

## Spec-first

The human provides requirements, plan, and spec. Claude Code **confirms
understanding before implementing**. No implementation ahead of an acknowledged spec.

## Test-driven

- Write/extend the test suite **before or alongside** implementation, not after.
- Pure calculation (`portfolio/`, `strategy/`) must have fixed-fixture unit tests.
- External boundaries (`pricing/`, `llm_insight/`) are tested against mocks: parsing,
  idempotency, caching, graceful degradation.
- Route layer (`web_ui/`) tested with httpx, including HTML-fragment assertions for
  HTMX endpoints.

## Type safety gate

- Full type hints everywhere. `mypy` runs in **strict** mode and must be clean
  before a version ships. Treat a mypy error as a build failure.

## CHANGELOG integrity

- Every shipped version gets a `CHANGELOG.md` entry.
- After **any** edit to `CHANGELOG.md`, verify structure:
  `grep -c "^## \[v" CHANGELOG.md` — the count must match the number of versions.
- Prefer **bounded-section rewrites** over surgical in-place string edits to the
  CHANGELOG (surgical edits have corrupted it before).
- Version heading dates are **real delivery dates**, never placeholders or guesses.

## Lessons learned (PEM)

- Record post-error / post-mortem lessons in `LESSONS_LEARNED.md` as they occur.
- Before solving a problem that feels familiar, check this file first.

## Large-file discipline

- **Never load large reference files (specs, datasets, generated reports) in full.**
  Read bounded sections. This applies to data files and to long docs alike.

## Self-review pass

Before declaring a version done, do a dedicated self-review pass over the diff:
correctness, boundary adherence (`architecture.md`), money-type discipline
(`data-and-pricing.md`), and test coverage of the change.

## Two-environment loop-engineering (test site → promote to prod)

The live deployment runs **two isolated instances of the app on one host** — a **prod**
instance (real ledger, login-gated, pinned to a released **tag**) and a **test/demo** instance
(synthetic data, tracks work-in-progress). They are isolated at **three levels** so iterating
on the test site never touches prod:

- **separate code checkout** (own git working tree),
- **separate venv** — the test venv installs `.[dev]` so the regression suite runs **on the
  site**; prod's venv is prod-deps only (`pip install -e .`),
- **separate data folder** — own `DB_PATH`; db + logs + backups all derive from `db_path.parent`.

**The loop (AI self-iteration / regression):**
1. Iterate on a branch (not `main`); run the **heavy gates on the dev machine**
   (`pytest` incl. browser e2e / `mypy --strict` / `ruff`) — it is orders of magnitude
   faster than the host and doesn't disturb the live instances.
2. Deploy the branch to the **test** instance. The deploy itself is the first
   environment gate (`pip install -e .` + service start + `/api/health`).
3. **Behaviour-verify from outside** against the test URL: `scripts/verify_live.py
   <test-url> [--refresh]`, plus a real-browser click-through of any changed frontend
   flow. This exercises the REAL stack (uvicorn, SQLite file, live providers, TLS) —
   things the hermetic suite deliberately cannot see. The test instance's scheduler is
   disabled, so its data stays deterministic between verifications.
4. Fix → repeat until everything is green.
5. **Promote only when green — as a queue, demo first (owner directive 2026-08-01):** merge
   to `main`, cut a version + tag (`/ship-version`), then deploy **that tag** to the **test
   site first**, verify it, and only then to prod. Both instances end the release on the SAME
   tag; two sites on different commits is an unfinished release. Verify each with
   `verify_live.py <url> --expect-version X.Y.Z` (bare version), and verify **external
   reachability** of each public URL from outside the host — a healthy `127.0.0.1` port says
   nothing about whether the site is reachable (2026-07-29: the demo funnel's DNS registration
   vanished from the control plane while the node was online and the service healthy).
   Prod only ever moves forward to a validated tag — experiments never reach it.
6. **Abort protocol — data first, diagnosis second.** Any anomaly stops the sequence on the
   spot; never roll forward through it. Establish that user data (**prod above all**) is
   intact and recoverable *before* investigating; if it is at risk, restoring from the
   pre-deploy backup outranks understanding the bug. Re-pinning prod to the previous released
   tag is always a safe action. Then diagnose, fix on a branch, and re-run the whole
   checklist. Full sequence + priority order: `/ship-version` Part B/C.

**Gate placement (decided 2026-07-02, human sign-off):** do NOT run the full suite or
mypy on the host — measured on the `e2-micro`: pytest ~25 min (identical verdict to the
local run = zero new signal), mypy >20 min, and both compete with the LIVE instances for
the 1 GB of RAM. Environment-specific risk is covered by step 2 (install/boot failures
surface there — that is where the lxml/py3.13 class of problem appears) and step 3.
**Escape hatch:** for platform-sensitive changes (dependency bumps, path/FS handling,
provider adapters), run a *targeted* on-site subset — e.g. `pytest tests/pricing -q
--ignore=tests/e2e --ignore=tests/probe` — minutes, not the whole suite; never heavy
tooling on the box that serves prod.

**Invariants (never violate):**
- Prod runs a released **tag**; the test site tracks a branch / WIP commits **between** releases
  and is moved onto the tag as part of shipping one. Never point prod at an untested branch.
- **Back up the DB of every instance you are about to deploy to — the test site included.** Its
  synthetic ledger is the accumulating stress-test corpus (owner ruling 2026-07-31: test data is
  kept, not reset); re-seeding it silently destroys coverage built up over releases.
- **Never deploy prod while the test site is red**, and never deploy prod without a fresh prod
  backup taken in that same session.
- Test data is **synthetic** (`scripts/seed_demo.py`). NEVER copy real data into the test set;
  NEVER point the test `DB_PATH` at the prod data folder.
- Keep prod and the test site **physically separate** (checkout + venv + data folder) so a
  restart/crash of one can't pick up the other's code or data.
- Concrete host paths, URLs, ports, systemd units, and Tailscale node names for BOTH instances
  live in the git-ignored `docs/human_noted/` deployment note — **never commit real host
  details** (public docs use placeholders).
- **Every operation against the VM is logged — run it through `scripts/vm_exec.py`, not
  `gcloud` directly.** The operation log (`docs/human_noted/vm-operation-log.md`, git-ignored,
  append-only) is the VM's audit trail, and read-only inspections count. Owner directive
  2026-08-01, after the log went silent for 15 released versions and the v0.1.25 deploy issued
  five remote commands but recorded one narrative summary: a rule that depends on remembering
  is not a control, so the wrapper writes the entry as part of running the command. Use
  `--log-only` to record an operation performed some other way (console action, human-run
  command) rather than leaving a hole. Corrections are **new entries**; never rewrite the log.

## `ship-version` checklist

1. Tests green (`pytest`).
2. `mypy --strict` clean.
3. New/changed behavior covered by tests.
4. `CHANGELOG.md` entry added; `grep -c "^## \[v"` count verified; date is the real
   delivery date.
5. `LESSONS_LEARNED.md` updated if anything was learned the hard way.
6. Self-review pass complete.
7. **Staged deploy:** back up both DBs → tag to the **test site**, verified → then the same
   tag to **prod**, verified → **external reachability of both public URLs** → both sites on
   the same tag. Abort on any anomaly, data first (`/ship-version` Part B/C).
8. Conversational summary to the human in **Traditional Chinese**; all artifacts in
   **English**.

## `resume-dev` (session start)

Read `CLAUDE.md` + the head of `CHANGELOG.md` + only the rule file(s) relevant to
the task. Do not re-read the whole repo or load large files to "get context".
