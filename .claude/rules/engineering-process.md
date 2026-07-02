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
1. Iterate on the **test** checkout (edit code on a branch, not `main`).
2. Deploy to the **test** instance; run the gate **on the site** (`pytest` / `mypy --strict` /
   `ruff`) + behaviour-verify against the test URL. The test instance's scheduler is disabled,
   so its data is deterministic and regressions are reproducible.
3. Fix → repeat until the gate is green.
4. **Promote only when green:** merge to `main`, cut a version + tag (`/ship-version`), and
   deploy **that tag** to prod. Prod only ever moves forward to a validated tag — experiments
   never reach it.

**Invariants (never violate):**
- Prod runs a released **tag**; the test site tracks a branch / WIP commits. Never point prod at
  an untested branch.
- Test data is **synthetic** (`scripts/seed_demo.py`). NEVER copy real data into the test set;
  NEVER point the test `DB_PATH` at the prod data folder.
- Keep prod and the test site **physically separate** (checkout + venv + data folder) so a
  restart/crash of one can't pick up the other's code or data.
- Concrete host paths, URLs, ports, systemd units, and Tailscale node names for BOTH instances
  live in the git-ignored `docs/human_noted/` deployment note — **never commit real host
  details** (public docs use placeholders).

## `ship-version` checklist

1. Tests green (`pytest`).
2. `mypy --strict` clean.
3. New/changed behavior covered by tests.
4. `CHANGELOG.md` entry added; `grep -c "^## \[v"` count verified; date is the real
   delivery date.
5. `LESSONS_LEARNED.md` updated if anything was learned the hard way.
6. Self-review pass complete.
7. Conversational summary to the human in **Traditional Chinese**; all artifacts in
   **English**.

## `resume-dev` (session start)

Read `CLAUDE.md` + the head of `CHANGELOG.md` + only the rule file(s) relevant to
the task. Do not re-read the whole repo or load large files to "get context".
