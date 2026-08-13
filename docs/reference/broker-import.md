# Importing a broker statement

How to get a broker's own transaction export into this ledger. One broker supported today:
**Charles Schwab**.

The authority for what is correct is `portfolio_dash/data_ingestion/broker/` and its tests —
`tests/data_ingestion/test_broker_adapter.py`, `test_broker_reconcile.py`, and
`tests/scripts/test_schwab_convert.py`, which re-parses the converter's output through the
**production** import builders. This page explains the workflow; the tests decide the rules.

---

## The workflow

```powershell
.venv\Scripts\python scripts\schwab_convert.py `
    --export <the export CSV, or a directory of them> `
    --out    <an empty output directory> `
    --account schwab
```

Neither path has a default and both are required — this reads real financial history, and a
defaulted path is a path that gets read by accident.

Then: read the report, fill in what it says it cannot know, and upload the CSVs through
**輸入 → CSV 匯入** (`trades.html#tab-csv`), one kind at a time.

### What comes out

| File | What to do with it |
| --- | --- |
| `import_transactions.csv` · `import_dividends.csv` · `import_cash.csv` · `import_corporate_actions.csv` · `import_fx.csv` | Upload as-is. (A Schwab export contains no currency conversions, so the FX file is normally header-only.) |
| `corporate_actions_TO_COMPLETE.csv` | **Fill in the blanks first.** Each row's `note` says what is missing. |
| `openings_TO_COMPLETE.csv` | **Fill in `original_cost_total` first.** These are positions older than the export window. |
| `conversion_report.txt` | Read it. It names every row that was dropped, parked, or could not be converted, by `file:line`. |

The two `*_TO_COMPLETE` files are rejected by the importer until they are filled in. That is
intended, not an obstacle: a corporate action with a guessed ratio and an opening position with
a cost of zero both import cleanly and are both silently wrong afterwards.

---

## It refuses rather than half-converts

If the reconciler finds a **blocking** issue, **no CSV is written** — only the report. There is
no flag to import the part that worked. A partial import of a file whose arithmetic contradicts
itself leaves a ledger nobody can rebuild.

Blocking issues seen on a real export, and what each means:

| Code | What it means | How to clear it |
| --- | --- | --- |
| `cusip_unresolved` | A row identifies a security only by its CUSIP. Imported as-is it becomes a **second instrument**, and any corporate action against it then replays as a no-op — silently, with every screen still looking right. | The message quotes the row's description, which carries the company name. Re-run with `--alias <the CUSIP>=<the ticker>` (repeatable). |
| `priced_row_no_cash` | A trade with a stated price that moved no money — a **free position**. Observed once: a when-issued ADR re-badged regular-way, whose cancelling row carries no symbol to pair it with. | Look at the two lines the report names and decide. Usually one of them should be deleted from the export. |
| `priced_row_mismatch` | `quantity × price ± fees` disagrees with the row's own amount. | This is either a broker oddity or a parsing bug — send the report. |
| `cash_not_conserved` · `shares_not_conserved` · `suppressed_not_zero` · `rows_lost` | The conversion itself lost or invented something. | A defect in this code, not in your file. Send the report. |

**Advisories never block.** Option legs (recognised, not supported — P3), positions older than
the export window, a row that appears in two overlapping exports, and rows classified as noise
whose arithmetic disagreed are all listed individually by `file:line` and left for you.

---

## What is safe to paste

**stdout is pasteable.** It carries fixed labels, integer counts, issue *codes*, and symbols
masked by `scripts/privacy.py` — no amount, no share count, no account name, no file name.

**stderr and `conversion_report.txt` are not.** stderr interpolates file names, and a broker's
export filename embeds a fragment of the account number; the report carries the amounts on
purpose, because it sits in `--out` beside the converted ledger anyway.

So: paste the summary, not the whole terminal, and not the report.

---

## Things it deliberately does not do

- **It never invents a corporate-action ratio.** A one-leg split states only its share delta.
  Recovering the ratio by replaying the file was implemented and then removed: measured against
  two real one-leg splits it produced `4:1` for one (correct) and `109:24` for a 3-for-1 — both
  with the same confidence, because the wrong one's position predated the export and a replay
  cannot know it is incomplete.
- **It never books an option leg.** They are recognised so that an unmapped-row error does not
  fire on a statement that legitimately contains them, and then listed as unimported. The 100×
  contract multiplier is an assumption to remove from every money formula, not a field to add.
- **It never guesses at an unrecognised row.** An unmapped `(action, description)` pair stops
  the run with the pair quoted. There is no catch-all: a default bucket is the same defect
  wearing a name.
- **Cash in lieu has no ledger row** and is reported for manual entry — it is neither a trade
  nor a cash movement.

---

## Adding a second broker

Add a module beside `schwab.py` that maps that broker's rows onto the **existing**
`EventKind` vocabulary, and register it in `registry.py`. Everything downstream — the folds,
the reconciler, the CSV writers — is broker-neutral and should need no change. If it does need
one, that is worth a conversation: the vocabulary being closed is what makes "this row is
unmapped" something the code can detect at all.
