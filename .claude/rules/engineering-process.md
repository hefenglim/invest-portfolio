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
