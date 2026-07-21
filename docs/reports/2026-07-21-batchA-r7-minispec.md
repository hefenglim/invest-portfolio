# Batch A (round-7 follow-ups) — mini-spec & traceability (target v0.1.21)

Owner sign-off 2026-07-21 (「全部照建議」 on the Phase-0 report; model directive: all
agents Opus 4.8 xhigh). Phase-1 plan + Senior Review record:
`2026-07-21-batchA-phase1-plan.md`. Batch B (Moomoo account merge, FU pending) is a
separate future round — NOT in this batch.

## Decisions (FU-D55..D60)

- **FU-D55 — MY resolve hardening (W1).** Root cause was NOT the LLM alone: the MY
  prompt clause was one thin line (vs rich TW guidance) AND `resolved` required
  yfinance `.KL` verification, which lacks many Bursa counters — correct answers were
  demoted to candidates. Fix: prompt v2 (7 directory-verified name⇒code exemplars,
  ACE leading-zero rule, brand/mall→listed-parent rule; mirrored into the AI-input
  prompt, versions v2/v4, LIBRARY_VERSION official-v10) + baked
  `pricing/bursa_registry.py` (1,079 four-digit codes; provenance: klsescreener
  mirror after the Bursa primary WAF-blocked the dev IP; 7 exemplars byte-verified
  against Bursa) + MY offline lookup fallback in `lookup_instrument`. Accepted
  consequences: registry-verified symbols register price-less (stale until a provider
  covers them); the manual-trade auto-register (`force=False`) quote gate unchanged;
  registry is a dev-time snapshot (refresh = re-fetch + re-bake).
- **FU-D56 — news fetch strengthening (W2).** `FetchOutcome` classification (7
  statuses), single WARNING log seam, `fetch_status`/`fetch_attempts` columns,
  bounded retry queue (14 d / 3 attempts / 10 per run) that re-fetches empty-body
  rows after discovery stops surfacing them, browser headers + cookie opener,
  1.5 MB byte cap, extraction fallback chain (block-strip → JSON-LD/embedded-JSON →
  `<p>` cluster → salvaged). Zero new dependencies. FM10's CSS-soup case changes
  from silent discard to labeled `salvaged` (owner directive: over-fetch, LLM trims).
- **FU-D57 — 扣款後現金 (W3).** `manual_preview` emits `cash_after` = known pool
  balance + already-signed trade total, same quote ccy (dynamic label — USD for US
  instruments, never hardcoded TWD); null when the balance is unknown. Display-only.
- **FU-D58 — old-vs-new 試算 (W3).** `_position_preview` + `/api/whatif` emit
  `old_shares`/`old_original_avg`/`old_adjusted_avg` (+ `old_weight` and SELL
  `remaining_market_value`, floored at 0, on whatif). The detail-page 試算 drawer no
  longer computes money locally: debounced POST `/api/whatif`, old→new pairs,
  「試算暫不可用」 on error; `window.pdFeeTax` local fee mirror deleted (no
  consumers). Draft preview renders the same pairs. SELL avg pairs show old==old
  (averages unchanged by a sell — correct).
- **FU-D59 — clear-on-success unification (W4).** AI flow: checkboxes now REALLY
  filter (commit rebuilds CSV from checked rows only; newline-sanitize in
  `_drafts_to_csv` guarantees the row↔line invariant; 422-ack recommit reuses the
  filtered text; zero checked → 寫入 disabled); full success clears
  text/preview/csv/images with an in-pane `#ai-result` banner; partial keeps only
  unwritten rows. CSV flow: full success clears the paste + date-format; partial
  keeps the entire raw paste. Other six flows already compliant (audited).
- **FU-D60 — opening-inventory simplification (W4).** Contract inverted: required =
  `account,symbol,shares,original_cost_total,build_date`; `original_avg_cost`
  optional/legacy (only-avg → derived total + soft `opening_total_derived`; both
  disagreeing beyond max(1 minor unit, 0.5%×total) → soft `opening_cost_mismatch`).
  The stored avg column is DROPPED — the repo's first destructive migration
  (idempotent `_drop_column_if_present`, required because the legacy column was NOT
  NULL); `OpeningInventory.original_avg` is a computed read-only property; all 9
  reader modules + the stress-audit oracle + raw-SQL tests updated; `GET
  /ledgers/openings` `avg` + symbol open-event `price` wire fields now computed on
  read; `edit_opening` PUT takes authoritative `total` (legacy `avg` accepted,
  derived). Form: `#o-symbol` wired to the `#m-symbols` datalist; 均價 is a live
  read-only hint (`#o-avg-view`); required = shares + total.

## Traceability (owner item → unit → tests)

| Owner item (2026-07-21) | Unit | Key tests |
| --- | --- | --- |
| ② MY 辨識度低 | W1 / FU-D55 | `test_bursa_registry.py`, `test_my_lookup_registry.py` (5, incl. demotion-fix proof), template drift pins |
| ③ 新聞缺內文 | W2 / FU-D56 | `tests/news` 76 (statuses, fallback chain, retry queue, migration idempotency, compat) |
| ④ 扣款後現金 + 均價盤查 | W3 / FU-D57 (+ Phase-0 audit: identical averages = correct domain behavior, pinned by existing 550.4275/547.9275 divergence test) | `test_input_manual_api.py` (cash_after exact strings, old_* incl. divergence case) |
| ⑤ 試算舊vs新 | W3 / FU-D58 | `test_whatif_api.py`, `test_whatif.py`, e2e `test_whatif_drawer_flow.py` |
| ⑥ 輸入成功清空 | W4 / FU-D59 | `test_agents.py` (newline-sanitize), e2e `test_input_clear_on_success_flow.py` (3: filtering, full-clear, partial-keep) |
| ⑦ 期初庫存簡化 | W4 / FU-D60 | `test_opening_inventory.py` (contract+migration), `test_ledgers_mutations_api.py` (PUT new+legacy), e2e `test_opening_input_flow.py` |
| ① Moomoo 合併 | — | Deferred to its own round (Batch B; impact map in the Phase-1 plan's Phase-0 evidence) |

## Gates (Phase 3)

1. Full pytest — detached gate. 2. Bare `mypy --no-incremental` full scope (527
files; W2's 3 test-file errors fixed centrally — the v0.1.20 lesson held again).
3. `ruff check .` — clean. 4. `/stress-audit` (FU-D60 touches opening money-of-record
source data). 5. Contract/template/golden suites green. 6. id-contract sweep — no
new dead zones (`#ai-result`, `#o-avg-view` wired; remaining MISSING = pre-existing
guarded baseline). 7. This traceability matrix. 8. Demo deploy + behavior probe +
`verify_live`.
