# Design: `forex/` — Currency-Exchange Ledger & FX P&L (sub-project ②)

- **Date:** 2026-06-07
- **Status:** Approved (design); pending spec review
- **Module:** `portfolio_dash/forex/`
- **Author:** Claude Code (from human spec, brainstorming flow)
- **Depends on:** `shared/` (enums, models, fx.convert) and ① `portfolio/` *results passed in*
  (foreign stock market value); does **not** import `portfolio/`.

## Context & purpose

Sub-project ② computes **realized and unrealized FX (換匯) P&L** per account, as an
**attribution decomposition** of ①'s reporting-currency return — never an extra gain
added on top. It answers "of my home-currency return, how much came from the asset
moving in its own currency vs the exchange rate moving," and surfaces decision
indicators (foreign cash balance, average acquisition rate vs spot, realized vs
unrealized, stock vs cash split). Pure functions over inputs with fixture tests.

## Decisions (settled in brainstorming, 2026-06-07)

1. **Exposure = whole foreign exposure (cash + stocks)** — option A. Unrealized FX is
   computed on both foreign-denominated holdings and idle foreign cash.
2. **Acquisition rate = FXConversion (home→foreign) conversions only.** Foreign currency
   arriving from stock sales / dividends increases the cash balance but does **not**
   change the pool's weighted-average acquisition rate (it was not a conversion).
3. **Decomposition, never additive.** `forex/` outputs FX P&L numbers; the *consumer*
   (dashboard/orchestration) assembles the decomposition against ①:
   - `stock_FX = unrealized_fx_stocks` (rolled to reporting)
   - `cash_FX  = realized_fx + unrealized_fx_cash` (rolled to reporting)
   - `total_FX = stock_FX + cash_FX`
   - `asset_pnl  = ①.reporting_total_return − stock_FX`
   - `grand_total = ①.reporting_total_return + cash_FX`   (① ignores foreign cash)
   - `asset_pnl + total_FX = grand_total`.
   FX is **never** added on top of ①'s number as if it were extra profit.
4. **Pure functions; no `portfolio/` import.** Foreign stock market value per account is
   passed in (computed by the orchestrator from ①'s `value_holdings`). Foreign cash is
   reconstructed by `forex/` from the shared ledgers.

## Scope

Produces FX P&L only for accounts whose holdings' quote currency differs from the
account's funding currency:
- **Charles Schwab (US):** USD exposure anchored in **TWD** (funding).
- **Moomoo MY (US):** USD exposure anchored in **MYR** (funding).
- TW broker (TWD/TWD) and Moomoo MY (MY) (MYR/MYR) have no FX exposure → no result rows.

The home/anchor currency for an account's pool is its `Account.funding_ccy`; the foreign
currency is the quote currency of its non-funding holdings (USD here).

## Inputs (passed in; pure)

- `accounts: dict[str, Account]` — for `funding_ccy` (home anchor).
- `instruments: dict[str, Instrument]` — for each symbol's `quote_ccy`.
- `transactions: list[Transaction]`, `dividends: list[Dividend]`,
  `fx_conversions: list[FXConversion]` — the source ledgers.
- `foreign_stock_value: dict[str, Decimal]` — per account, current market value of that
  account's foreign-denominated holdings **in the foreign currency** (orchestrator sums
  ①'s valued `Holding.market_value` for holdings whose `quote_ccy != funding_ccy`).
- `current_spot: Callable[[Currency, Currency], Decimal]` — spot rate (foreign→home and
  home→reporting). Returns `Decimal("1")` for identity.
- `reporting: Currency` — reporting currency for the rollup.

## Per-account model (for each FX-exposed account)

Let `home = funding_ccy`, `foreign = the foreign quote ccy` (USD).

### Pool weighted-average acquisition rate
From `fx_conversions` for the account where `from_ccy == home and to_ccy == foreign`:
`avg_rate = Σ from_amount(home) / Σ to_amount(foreign)`  (home per 1 foreign).
If the account has no such conversions, `avg_rate = None` (FX P&L not computable for it;
surfaced, not faked).

### Foreign cash balance (reconstructed, in foreign ccy)
```
foreign_cash =
    + Σ fx_conversions.to_amount   where to_ccy == foreign            (home→foreign in)
    + Σ (qty*price − fees − tax)   for SELL of foreign-quoted symbols  (sale proceeds)
    + Σ dividends.net             for CASH dividends on foreign symbols
    − Σ (qty*price + fees + tax)   for BUY of foreign-quoted symbols   (purchases)
    − Σ fx_conversions.from_amount where from_ccy == foreign           (foreign→home out)
```
DRIP/STOCK dividends touch no cash (DRIP nets to zero: paid then reinvested) → excluded.

### Realized FX P&L (home ccy)
Over reconversions (`fx_conversions` where `from_ccy == foreign and to_ccy == home`):
`realized_fx = Σ [ to_amount(home) − from_amount(foreign) × avg_rate ]`.

### Unrealized FX P&L (home ccy), split
```
spot = current_spot(foreign, home)
unrealized_fx_stocks = foreign_stock_value[account] × (spot − avg_rate)
unrealized_fx_cash   = foreign_cash               × (spot − avg_rate)
```

## Output models (`forex/results.py`)

- **`AccountFXResult`**: `account_id`, `home_ccy: Currency`, `foreign_ccy: Currency`,
  `avg_rate: Decimal | None`, `current_spot: Decimal | None`, `foreign_cash: Decimal`
  (foreign), `foreign_stock_value: Decimal` (foreign), `realized_fx: Decimal | None`,
  `unrealized_fx_stocks: Decimal | None`, `unrealized_fx_cash: Decimal | None`
  (FX figures in `home_ccy`; `realized_fx` is `None` when `avg_rate` is `None`;
  `unrealized_*` are `None` when `avg_rate` or `current_spot` is unavailable).
- **`FXSummary`**: `by_account: dict[str, AccountFXResult]`, `reporting_currency: Currency`,
  `reporting_realized_fx: Decimal`, `reporting_unrealized_fx: Decimal`
  (each account's home-ccy FX converted to reporting at current spot, summed).

## Module layout

```
portfolio_dash/forex/
  __init__.py
  results.py    # AccountFXResult, FXSummary
  pools.py      # avg acquisition rate + foreign cash reconstruction (per account)
  fx_pnl.py     # realized + unrealized FX per account -> AccountFXResult; rollup -> FXSummary
```

`forex/` imports only `shared/*`. The orchestrator runs ① then computes
`foreign_stock_value` from ①'s valued holdings and passes it to ②, then assembles the
decomposition (§Decisions 3) for display.

## Error handling (loud, never guess)

- **No conversions for an FX-exposed account** → `avg_rate = None`; FX figures `None`
  (cannot establish a cost basis); surfaced in the result, not fabricated.
- **Missing spot rate** for an account's pair → `current_spot = None`, unrealized `None`
  (realized still computable from the ledger).
- **Unknown instrument / account** (referenced by a ledger row) → raise `KeyError`.
- All money is `Decimal`; non-finite rejected at model boundaries. FX conversion to
  reporting uses the single `shared.fx.convert`. `avg_rate` may exceed/precede spot
  freely (gains or losses); `foreign_cash` may be small or zero.

## Testing strategy (TDD, fixtures)

- Schwab scenario: TWD→USD conversion(s) → buy US stock → (stock appreciates) → assert
  `avg_rate`, `foreign_cash`, `unrealized_fx_stocks`, `unrealized_fx_cash`. Reconvert
  some USD→TWD → assert `realized_fx`.
- Moomoo MY-US scenario: MYR→USD anchor.
- **Decomposition identity:** with a fixture also run through ①, assert
  `asset_pnl + stock_FX + cash_FX == grand_total` and `asset_pnl = ① − stock_FX`.
- **No-double-count:** FX is never added on top of ①; the worked example (320,000 TWD →
  10,000 USD @32; buy 9,000 USD; spot 33) yields stock_FX 10,800, cash_FX 1,000,
  total_FX 11,800, asset 57,600, grand_total 69,400 TWD.
- Multi-currency rollup to reporting; weighted-avg over multiple conversions; cash
  reconstruction across buys/sells/cash-dividends/reconversions; DRIP excluded from cash.
- Edge: account with no conversions → `avg_rate None`; missing spot → unrealized `None`,
  realized still computed.

## Decision indicators surfaced (for the dashboard)

Per account: average acquisition rate vs current spot (cost-vs-market FX), foreign cash
balance, realized vs unrealized FX, stock-FX vs cash-FX split, and the asset-vs-FX
contribution to total return — answering "how much of my return is currency," and "is
now a good time to reconvert idle foreign cash."

## Out of scope (explicit)

`pricing/` (live spot fetch — spot is passed in); `data_ingestion` (entry/import —
fixtures here); persistence; `web_ui` rendering of the decomposition; the orchestration
combiner itself (this spec defines the formula; the combiner lands with the web layer).

## Follow-up flagged in final review (for the web/orchestration layer)

Deferred — lands with the decomposition combiner / dashboard (same cross-cutting concern
as ①'s coverage contract):
- **Rollup coverage signal.** `FXSummary` rolls each account's realized FX up always, but
  drops an account's unrealized FX when its spot is missing (stale) — so
  `reporting_unrealized_fx` can be a *partial* total with no signal distinguishing "truly
  0" from "some accounts excluded for stale FX." When the web layer is built, add a
  coverage indicator (e.g. excluded-account list / `unrealized_complete` flag) to the
  combined view so staleness is shown, never silently degraded. Pairs with ①'s deferred
  per-aggregate coverage signal — design them together.
