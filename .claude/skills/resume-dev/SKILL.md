---
name: resume-dev
description: "Resume development on portfolio-dash at the start of a session. Use this to load just enough context to continue work: read CLAUDE.md, the head of CHANGELOG.md, and only the rule file(s) relevant to the current task — without re-reading the whole repo or loading large files. Invoke with /resume-dev or when starting a new working session on this project."
---

# Resume Development

Goal: get oriented with the **minimum** context needed to continue, honoring the
large-file discipline in `engineering-process.md`.

Steps:

1. Read `CLAUDE.md` (root index: locked decisions, module map, core invariants).
2. Read the **top** of `CHANGELOG.md` — the `[Unreleased]` section and the latest
   version entry. Do not read the whole history.
3. Identify the task at hand, then read **only** the relevant rule file(s):
   - calculation / ledger / returns / FX → `.claude/rules/domain-ledger.md`
   - tick / lot / fees / tax → `.claude/rules/markets-and-fees.md`
   - DB schema / quotes / precision → `.claude/rules/data-and-pricing.md`
   - module boundaries / new route → `.claude/rules/architecture.md`
   - LLM / prompts → `.claude/rules/llm-insight.md`
   - dashboard visuals / Claude Design handoff → `.claude/rules/design-handoff.md`
   - library choice → `.claude/rules/stack.md`
4. Check `LESSONS_LEARNED.md` if the task resembles a past problem.
5. **Do not** re-read the entire repository, and **do not** load large reference files
   or datasets in full. Read bounded sections.
6. Confirm the spec for the task before implementing (spec-first). State assumptions.

Output a short orientation: what the current task is, which rule files apply, and the
first concrete step. Then proceed under TDD.
