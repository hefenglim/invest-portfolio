# Lessons Learned (PEM)

Post-error / post-mortem notes. **Before solving a problem that feels familiar, check
here first.** Each entry: date · context · what went wrong · the rule or fix that
prevents recurrence.

## Standing reminders (carried over)

- After any `CHANGELOG.md` edit, verify with `grep -c "^## \[v" CHANGELOG.md`
  (structural edits have corrupted it before).
- Prefer **bounded-section rewrites** over surgical in-place edits on structured docs.
- **Never load large reference files in full** — read bounded sections only.
- Version heading dates are **real delivery dates**, never placeholders.

## Domain reminders (this project)

- **No double counting:** dividends enter total return once (P&L uses original cost);
  FX gain/loss is an attribution breakdown of the reporting-currency XIRR, not additive.
- **Decimal, not float**, for money/price/rate; store full precision, quantize at
  settlement/display. MY sub-RM1 prices need 3 dp — do not truncate to 2 dp.
- **Average cost is computed on read** from `total_cost / shares`, never stored as an
  authoritative rounded value.

## Implementation lessons

- **`StrEnum` + Pydantic v2 serialization (2026-06-06):** `Currency`/`Market` are
  `enum.StrEnum` (ruff UP042 prefers this over `(str, Enum)` on 3.11+). A `StrEnum`
  member *is* a `str` (`isinstance` is `True`, SQLite binds it as TEXT, `json.dumps`
  and `model_dump(mode="json")`/`model_dump_json()` emit a bare string). **But**
  Pydantic v2 `model_dump()` in the default *python* mode returns the **member object**,
  not a bare string — so `type(x) is str` is `False` even though `isinstance(x, str)` is
  `True`. When serializing settings/models for the web layer, use json mode (or
  `isinstance`, never `type() is str`).
- **sqlite3 DDL is not transactional under the legacy isolation model (2026-06-06):**
  `shared/db.session()` commits/rolls back DML correctly, but Python's default
  `isolation_level=""` runs standalone DDL (CREATE/DROP TABLE, etc.) *outside* a
  transaction — a `rollback()` after pure DDL is a no-op and the schema change sticks.
  DML that follows DDL in the same session *is* transactional (Python 3.12 no longer
  auto-commits before DDL). Keep schema migrations out of plain DML sessions, or handle
  this explicitly.
- **Dev gates need the repo `.venv` interpreter (2026-06-09):** runtime deps + tooling live only in
  `.venv` (`./.venv/Scripts/python.exe`); the bare `python` resolves to a system interpreter without
  them, so `python -m pytest` / `-m mypy` report spurious missing-module / missing-stub errors that
  look like regressions. Always run gates via the venv; instruct subagents to do the same.
- **Fix the test, not the production code, when the test is wrong (2026-06-09):** a flawed budget
  test (far-future-dated usage rows vs. wall-clock reset timestamps) tempted an implementer to make
  `reset_budget` scan `llm_usage` and advance its timestamp — bending real behavior to satisfy a
  broken test. The correct fix was the opposite: keep `reset_budget` a plain `now()` event and make
  the test deterministic with explicit timestamps. When a test forces awkward production logic,
  suspect the test first.
