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

## Intended differences since v0.1.31

**PERF-F2** (fixed after v0.1.32, owner ruling 2026-10-19; one owner:
`tests/reference/perf_f2.py`). The trend's FX pair set now holds the currency of every cash
movement it converts (`portfolio/timeseries.py::trend_fx_currencies`), so on `unquoted_ccy_cash`
the 50 MYR rebate is converted and the trend exists. These files are NOT regenerated: they stay
v0.1.31's record (`test_corpora.py::test_record_perf_f2_in_v0131` still reads them).
`test_reference_payloads.py` compares the current code against `perf_f2.expected_bytes`, which
is each file itself everywhere except:

| File of `unquoted_ccy_cash` | Paths that change (nothing else may) |
| --- | --- |
| `dashboard.json` | `kpis.total_return_fx_complete`, `kpis.principal_fx_effect`, `kpis.trading_financing_cost`, `kpis.fx_complete_reason`, `trend.points`, `trend.available`, `benchmark.reason`, `benchmark.uncovered_markets`, `benchmark.uncovered_ratio`, `freshness.trend_unavailable_reason` |
| `month_end/2026-03.json`, `2026-04`, `2026-05` (D on or after the rebate) | the same minus the three B figures, which a month end withholds in both ledgers (a held price is missing at D) |

Each new value is DERIVED, not stored: from `golden`'s own v0.1.31 file of the same name (the
same ledger without the rebate). The trend is golden's, its `net_invested` lowered from
2026-03-16 by the rebate at its own date's rate (50 × 7.05 = 352.50) and its `net_worth` raised by
the 50 MYR pool at each day's rate (7.05, then golden's 7 from 2026-06-09); B and
交易與融資成本 are golden's plus 352.50; 本金匯率效果 is `B − A − 交易與融資成本` (AI-D48) with this
corpus's own A, which the fix does not touch (111600) — so 112952.50 − 111600 − 352.50 =
1000.00, golden's 1000 at the exponent of the operands; the benchmark block, `fx_complete_reason` and
`trend_unavailable_reason` are golden's (a cash movement is no flow of the counterfactual,
AI-D49, and golden stores no index closes). The January and February month ends and the tax
package are byte-identical. `test_perf_f2_changes_exactly_the_listed_paths` checks the path
list both ways. `make_reference_payloads.py --here` now differs from these files on exactly
those paths.

**The trend points' `pnl`** (owner rulings 2026-10-22; one owner: `tests/reference/trend_pnl.py`).
The trend tooltip's 累計總損益（含匯兌） was `total_value − net_invested` subtracted in JS floats;
the server now sends it. Every served trend point gains ONE key, `pnl`, appended after
`net_worth`: its own `total_value − net_invested` at the reporting currency's minor unit (TWD 0
dp, USD / MYR 2 dp), ROUND_HALF_UP, a rounded-away loss reading `0` (never `-0`), and `null` on a
point marked `incomplete` (an unreliable figure is not shown, as the KPI B gate withholds B).
These files are NOT regenerated. `trend_pnl.expected_bytes` composes with PERF-F2: it starts from
`perf_f2.expected_bytes` and adds `pnl` to every point of `trend.points` in `dashboard.json` and
in every `month_end/<YYYY-MM>.json` (whose one point D carries it too), derived from that point's
own strings and `incomplete` flag and the file's own `trend.reporting_currency` with a
transcription of the rounding rule, never a call into the code under test. Nothing else changes: the tax package and every
other byte are PERF-F2's expectation (`test_trend_pnl_adds_exactly_one_key_to_every_point`).
The tests that compare what the API SERVES (`test_reference_payloads`, the store's read path,
a result-cache hit) read `trend_pnl.expected_bytes`; the tests that compare what the settlement
WORKER stores keep `perf_f2.expected_bytes`, because `pnl` is derived when a series is served
and is never stored. `make_reference_payloads.py --here` now also differs on every trend point
by that key.

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
| `rich` | 2026-06-10 14:30 (`RICH_NOW`) | TWD | `seed_rich`: every risky shape WITHOUT a refusal (below) |
| `rich_refusals` | 2026-06-10 14:30 | USD | `seed_rich_refusals`: `rich` + every refusal the replay records |
| `epoch1990` | 1990-03-15 14:30 (`EPOCH_NOW`) | TWD | `seed_epoch1990`: an opening built 1988, index closes from 1987 |

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
  before the rebate, still have a trend. Recorded here as v0.1.31 had it; fixed after v0.1.32
  — see "Intended differences since v0.1.31" above.
- **The rich corpora** (added after the wave-1 review of package A: the five above never held the
  shapes the new engine is most likely to get wrong). Three, not one, because one ledger cannot
  hold them all honestly:
  - **`rich`** — a declared short opened and covered; options: a bought put that EXPIRES (a
    price-0 close row), a sold put (sell-to-open) ADJUSTED 1 → 2 contracts and partly BOUGHT
    BACK (one contract open at the clock, valued at its last trade); a DRIP and a 配股 counted
    from its ex-date; SPLIT + EXCHANGE + SPINOFF; two same-day buys; a buy and a dividend
    dated after the clock; an FX gap (2026-02-02..02-20 on every pair, MYR/TWD derived); a
    stale held price (1155 stops 2026-06-01); a foreign deposit with its cost, two conversions
    and the three XIRR cash kinds. No refusal, so XIRR, B and the benchmark are all computed
    over these shapes (B − A is withheld here: the adjusted put is still open at the clock,
    OPT-D20);
  - **`rich_refusals`** — `rich`, reporting USD, plus a 賣超, a dividend with no position, a
    dividend paid on the open declared short, a close row with nothing to close, a readable
    action the replay refuses (a SPLIT on a symbol never held) and an UNREADABLE stored action
    (ratio 0), and a holding never priced. These withhold XIRR and B ledger-wide — which is
    why they are not in `rich`. Its tax package is REFUSED (the strict replay meets the
    unreadable row), so both years are `response.json` (422). Its 28-digit inverse rates make
    the trend's `net_invested` take the exactness guard's fallback by itself;
  - **`epoch1990`** — an AAPL opening built 1988-03-01, `^GSPC` closes from 1987-01-02 and ONE
    USD/TWD rate (1987-12-31), and a buy on 1990-02-05. v0.1.31 reads benchmark closes from
    1990-01-01 only, so the 1988 flow is uncovered and the 1990 buy is placed
    (`uncovered_markets ["US"]`). Its own corpus because its clock is in 1990: under a 2026
    clock the trend would run 38 years and the month-end sweep ~460 builds.

## The files of each corpus

- **`dashboard.json`** — the exact response body bytes of `GET /api/dashboard` at the corpus's
  `now`, through `create_app()` with `get_conn` / `get_now` / `get_reporting` overridden and no
  lifespan, exactly as `tests/conftest.py::api_client`.
- **`tax_<year>/`** — every member of `POST /api/export/tax-package` (`{"year": <year>}`):
  `realized_gains_<year>.csv`, `dividends_<year>.csv`, `fx_realized_<year>.csv` (BOM + CRLF, as
  `export/artifact.py::csv_blob` writes them) and `summary.md`. The zip itself is not stored: its
  member timestamps are the wall clock. A refusal would be stored as `response.json` (status +
  body); only `rich_refusals` is refused. Years: every calendar year from the first ledger
  date's to `now`'s (the first five corpora: 2026 only; `rich*`: 2025 and 2026; `epoch1990`:
  1988 to 1990).
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

1,065 KB in all (golden 58 KB, demo 153 KB, doubled_demo 154 KB, usd_reporting 64 KB,
unquoted_ccy_cash 41 KB, rich 173 KB, rich_refusals 201 KB, epoch1990 194 KB). The full
month-end sweep runs by default — `test_reference_payloads.py` renders all eight corpora in
about 15 s on the dev machine (the three rich ones about 8 s of it), so there is no sampling
switch; add one (and say so here) if a corpus grows enough to need it.
