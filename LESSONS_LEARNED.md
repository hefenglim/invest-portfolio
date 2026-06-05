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

_No project-specific incident lessons recorded yet._
