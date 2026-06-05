# Rule: Markets & Fee / Tax Rule Sets

Market trading rules (lot, tick) are fixed by the exchange. Fee/tax **numbers** are
configurable per account and **must be confirmed in the data-source probe (Q11)** —
the structures below are the schema; exact rates are filled/verified at setup.

## Market trading rules

### TW — TWSE / TPEx
- **Lot:** 1 張 = 1,000 股. Record the unit as **shares (股)**. Odd lot (零股) = integer
  shares.
- **Price tick (股票):** <10 → 0.01 · 10–50 → 0.05 · 50–100 → 0.1 · 100–500 → 0.5 ·
  500–1000 → 1 · ≥1000 → 5.
- **Securities transaction tax (sell side):** 現股 0.3% · 當沖 0.15% · ETF 0.1%.
  Buy side: none.
- **Brokerage fee:** 0.1425% (configurable) + discount rate + **min NT$20**. Fee and
  tax **rounded to integer NT$** (四捨五入).

### US — NYSE / NASDAQ
- **Lot:** 1 share (fractional shares deferred).
- **Price:** 2 decimals (for ≥ US$1).
- **Tax:** no transaction tax; sell-side tiny SEC/TAF regulatory fees (configurable,
  may be ~0).
- **Dividend:** 30% US withholding (W-8BEN), applies to both Schwab and Moomoo US.

### MY — Bursa Malaysia (verified at bursamalaysia.com)
- **Lot:** board lot = **100 units**; odd lot = 1–99 units.
- **Share price tick:** < RM1 → **0.005** · RM1–9.99 → 0.01 · RM10–99.98 → 0.02 ·
  ≥ RM100 → 0.10.
- **ETF price tick:** < RM1 → **0.001** · RM1–2.995 → 0.005 · ≥ RM3 → 0.01.
- ⚠️ MY prices therefore need **up to 3 decimal places** (sub-RM1 shares at 0.005,
  ETFs at 0.001). See the precision rule in `data-and-pricing.md` — do **not** truncate
  MY prices to 2 dp.
- **Fees (structure; verify exact rates in probe):** brokerage (negotiable) +
  **clearing fee 0.03%** (cap RM1,000) + **stamp duty** (per contract note, cap
  applies) + **SST** on brokerage.
- **Dividend:** cash; Malaysian single-tier system. Record net received (Q10); confirm
  any high-income dividend surtax in the probe.

## Per-account fee rule sets (configurable; bind to ACCOUNT, not market)

| Account | Market | Settle ccy | Funding ccy | Fee/tax skeleton |
| --- | --- | --- | --- | --- |
| TW broker | TW | TWD | TWD | 0.1425% + discount + min NT$20; tax 0.3% / 0.15% / 0.1% |
| Charles Schwab (Intl) | US | USD | TWD | ~US$0 commission + tiny sell-side reg fee |
| Moomoo MY (US) | US | USD | MYR | commission + platform fee + FX spread on MYR→USD |
| Moomoo MY (MY) | MY | MYR | MYR | brokerage + clearing 0.03% + stamp duty + SST |

The US market spans **Schwab** and **Moomoo MY** with different cost structures —
this is exactly why fee rules are per-account. Each account references one rule set;
rates live in config and are versioned (a rate change is a config change, recorded in
`CHANGELOG.md`).
