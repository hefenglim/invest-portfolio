# v0.1.31 reference payloads

The settlement-store program (PERF, `docs/superpowers/plans/2026-10-18-settlement-store.md`,
item W0.2 / decision P9) must keep every money figure **byte-identical to v0.1.31**. These files
are that reference. They were written by **v0.1.31's own code** — commit `473fe5a`, the
calculation code of the v0.1.31 release — never by the code under test and never by hand, so
a reference cannot drift with the change it is meant to catch.

`tests/reference/test_reference_payloads.py` re-renders every file with the current code and
compares bytes. `manifest.json` records, per corpus, the clock, the reporting currency, the
corpus fingerprint, the month ends and tax years, and the sha256 of every file.

## How they are made

```
python scripts/perf/make_reference_payloads.py            # (re)write this directory
python scripts/perf/make_reference_payloads.py --check    # compare only; exit 1 on a difference
python scripts/perf/make_reference_payloads.py --here --out <dir outside the repo>
```

1. `git worktree add --detach <temp dir outside the repo> 473fe5a`;
2. `tests/reference/{__init__,corpora,payload_gen}.py` of the current checkout are copied into
   that worktree (they use only APIs that exist at 473fe5a);
3. a child `python -c …` runs with `cwd` = the worktree and `PYTHONPATH` = the worktree (never
   `-I`), asserts that `portfolio_dash.__file__` lies under the worktree, and renders every
   corpus with `PD_DISABLE_SCHEDULER=1` and a throwaway `DB_PATH` (as `tests/conftest.py::_safe_db`);
4. the files and `manifest.json` are written here; the temporary worktree is removed.

`--here` renders with the current checkout's code instead — the self-test below.

**When to regenerate:** only when a corpus DEFINITION changes (a seed, a clock, a currency),
and then still from `473fe5a`. A payload difference produced by the code under test is a
regression to fix, never a reason to regenerate. The intended differences of the PERF program
are listed in the plan's invariant 1 (the labelled stale range, the additive `/api/snapshots`
fields, a teardown failure answering 500) — none of them is in these files.

The corpus seeds are read from `tests/conftest.py` (`_seed_golden`, `init_golden_base`,
`GOLDEN_NOW`) and `scripts/seed_demo.py`: the generator uses the 473fe5a copies of those two
files, the test uses the current ones. Each corpus's **fingerprint** (sha256 over its ledger,
price and FX rows, `corpora.FINGERPRINT_COLUMNS`) is checked first, so an edited seed fails as
"the corpus changed", not as a money difference. Schema additions (new tables, triggers, new
columns) do not move it.

## The corpora (`tests/reference/corpora.py`)

| Corpus | `now` (Asia/Taipei) | Reporting | Seed |
| --- | --- | --- | --- |
| `golden` | 2026-06-11 14:30 (`GOLDEN_NOW`) | TWD | `_seed_golden` over `init_golden_base` |
| `demo` | 2026-08-11 14:30 (`DEMO_NOW`) | TWD | `scripts/seed_demo.py::seed` over `init_golden_base`, its clock globals `_NOW` / `_TODAY` pinned to `DEMO_NOW` |
| `doubled_demo` | 2026-08-11 14:30 | TWD | `demo` + a +1-day copy of every transaction and dividend dated before `now` |
| `usd_reporting` | 2026-06-11 14:30 | USD | `golden` + ONE USD/TWD rate: 31.9 on 2026-01-02 |
| `unquoted_ccy_cash` | 2026-06-11 14:30 | TWD | `golden` + MYR/TWD 7.05 on 2026-03-02 + a 50 MYR `REBATE` on `moomoo_my`, 2026-03-16 |

- **`DEMO_NOW`**: `seed_demo`'s price paths are lists aligned to the first of each month from
  January 2026 plus "today" (nine points for ORBT and KEMB = January..August + today; three for
  VRTA / KEMG = July 1, August 1, today) — the seed was written for a "today" in August 2026 and
  landed on 2026-08-11 (commit cca111e). That day gives every path its designed last point and
  seven complete month ends.
- **`doubled_demo`**: copies keep every business field (fees, tax, snapshot, note, flags, DRIP
  terms; an ex-date moves with its row) and carry no provenance (`import_batch_id` NULL). They
  are written after every original, so a copy sharing a day with an original replays after it
  (DEF-012). A copy that would land on or after `now` is REFUSED (`double_before` raises) rather
  than moved or dropped — the demo has none (its latest copied row is 2026-05-08). Every row is
  a buy or a dividend, so the duplication cannot oversell; `test_corpora.py` replays it strictly
  and on every event day. Cash movements and FX conversions are NOT doubled, so some cash pools
  run negative — a valid ledger state the cash series simply carries.
- **`usd_reporting`** deviates from "golden reporting in USD" by one rate row, on purpose: golden's
  first flow (the 2330 buy, 2026-01-05) is in TWD and its first USD/TWD row is 2026-01-08, so
  golden in USD has no trend, no B and no XIRR (「查無 2026-01-05（含）之前的 TWD/USD 匯率」), and the
  28-digit inverse rates would reach the KPIs only. With the 2026-01-02 row every USD figure is
  computable, 1 / 31.9 does not terminate, and `net_invested` carries 28 significant digits
  (`test_corpora.py::test_usd_reporting_carries_28_digit_amounts_into_the_trend`).
- **`unquoted_ccy_cash`** is suspected defect PERF-F2's shape (spec §8 item 5). What v0.1.31 does
  on it (`test_corpora.py::test_record_perf_f2_in_v0131`): XIRR counts the rebate (0.5677 →
  0.5699), but the trend loads only the ledger's quote currencies (TWD, USD), finds no MYR rate
  for the rebate, and the WHOLE trend is unavailable — `trend = {"points": [], "available":
  false}`, `freshness.trend_unavailable_reason` 「帳本中有交易日期缺少匯率資料」, no net worth,
  `total_return_fx_complete` / `principal_fx_effect` / `trading_financing_cost` null with
  `fx_complete_reason` 「尚無趨勢資料，無法計算含匯兌損益」, and the benchmark unavailable
  (「部分投入流量缺少當日匯率，無法換算成報告幣，基準對照暫不可用」). The January and February month ends,
  before the rebate, still have a trend. Recorded, not fixed.

## The files of each corpus

- **`dashboard.json`** — the exact response body bytes of `GET /api/dashboard` at the corpus's
  `now`, through `create_app()` with `get_conn` / `get_now` / `get_reporting` overridden and no
  lifespan, exactly as `tests/conftest.py::api_client`.
- **`tax_<year>/`** — every member of `POST /api/export/tax-package` (`{"year": <year>}`):
  `realized_gains_<year>.csv`, `dividends_<year>.csv`, `fx_realized_<year>.csv` (BOM + CRLF, as
  `export/artifact.py::csv_blob` writes them) and `summary.md`. The zip itself is not stored: its
  member timestamps are the wall clock. A refusal would be stored as `response.json` (status +
  body); none of the five corpora is refused. Years: every calendar year from the first ledger
  date's to `now`'s (all five corpora: 2026 only).
- **`month_end/<YYYY-MM>.json`** — the "truncated world" at every month end D with
  `first ledger date <= D < now.date()` (the first ledger date is the earliest date on any
  transaction, dividend or ex-date, opening, cash movement, FX conversion or corporate action):
  a COPY of the database with every `prices` and every `fx_rates` row dated after D deleted, and
  `GET /api/dashboard` at **D 23:59 Asia/Taipei** (so every ledger cut is D's: `valued_as_of(D)`,
  `valued_rows_as_of(…, D)`). This is PERF-S7's definition of a period close's inputs: the
  latest close on or before D (re-expressed through the cut `ActionIndex`), direct-else-inverse
  FX on or before D, no age cut-off on the latest price, options at their last trade on or
  before D. The response is reduced to this **exact subset** (`payload_gen.period_subset`):
  - every top-level key, in payload order, EXCEPT `alerts`, `insights` and `llm_quota` (rules
    over the whole page and AI tables — not period figures). Kept: `as_of`,
    `reporting_currency`, `kpis`, `holdings`, `holdings_subtotals`, `realized`, `returns`,
    `allocation`, `currency_view`, `fx`, `dividends`, `ex_dividend_calendar`, `trend`,
    `benchmark`, `freshness`, `dividend_projection`, `unapplied_actions` and, on a ledger with an
    option, `options`;
  - each `holdings` row with every key (all the 待釐清 flags included) EXCEPT `spark_30d` (a
    30-day series);
  - `trend.points` reduced to the ONE point dated D (`date`, `total_value`, `net_invested`,
    `incomplete`, `net_worth` — the settled day D; empty when the trend is unavailable), the
    other `trend` keys kept. The net-worth line is that point's `net_worth`; `benchmark` is
    already a single lifetime figure, kept whole;
  - every value is the server's own string, re-serialised as `json.dumps(…, ensure_ascii=False,
    indent=1)` + LF.

`.gitattributes` here is `* -text`: the files are compared byte for byte, and with
`core.autocrlf=true` git would otherwise rewrite the CSVs' CRLF or the JSON's LF.

## Determinism (verified 2026-10-18)

Byte-identical across: two generator runs at 473fe5a; a `--here` run on the branch tip `24cd098`
(calculation code identical to 473fe5a; only `manifest.json`'s `source` line differs); in-process
renders with the wall clock frozen (freezegun) at 2031-02-03 04:05 and at 2025-01-01 00:30; and
every run with a fresh random `PYTHONHASHSEED`. No field depends on the wall clock or on
randomness. The things that WOULD have — and how they are pinned rather than post-processed:
`demo`'s seed reads `datetime.now()` at import (its `_NOW` / `_TODAY` globals are set to
`DEMO_NOW` before `seed` runs); the tax zip's member timestamps (only the members are stored);
`freshness.last_backup_at` reads the backup directory beside `DB_PATH` (a throwaway path: no
backup, `null`); `freshness.scheduler_running` reads the process (no lifespan: `false`).

## Size and time

581 KB in all (golden 58 KB, demo 153 KB, doubled_demo 154 KB, usd_reporting 64 KB,
unquoted_ccy_cash 41 KB). The full month-end sweep runs by default — rendering all five corpora
takes about 10 s on the dev machine, so there is no sampling switch; add one (and say so here) if
a corpus grows enough to need it.
