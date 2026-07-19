# Mini-spec — follow-ups round 4 (owner spec: 7 requirements + approved site-review picks)

Date: 2026-07-18 · Branch: `feat/v0119-followups` (on top of verified r3 @fcead48)
Source: the owner's formal spec (7 requirements, decisions locked in its 附錄 A; assumptions in
附錄 B accepted as-is) + 4 approved research-pack picks (sector normalization, account naming,
dividend income surfaces, multi-user cheap prep).

## Decisions

- **FU-D30 — site-wide prompt registry (需求三).** Inventory EVERY system prompt an AI feature
  uses (function → prompt → code/DB location). Unified mechanism = a single registry index over
  the TWO legitimate tiers: (a) code-owned versioned defaults in the official library
  (`llm_insight/official_templates.py`, already hosting news-organizer / system-prompt default /
  AI-input); (b) user-editable DB prompts (`system_prompt_config`, `news_prompt_config`,
  per-insight strategy prompts) whose DEFAULTS come from tier (a). No feature may hardcode a
  prompt outside the registry. Deliverables: inventory table + architecture note (where prompts
  live, how features fetch them, how to add one) in `docs/reports/2026-07-18-prompt-registry.md`;
  all stray prompts migrated.
- **FU-D31 — sector canonical vocabulary + AI detect (P1① + 需求一, merged).** New
  `shared/sectors.py`: canonical sector vocabulary (zh-TW display + stable keys) + a synonym →
  canonical read-time map (Tech/Technology/資訊科技…) applied at the TWO grouping seams (dashboard
  sector allocation + sector_weight alert input) and used to seed the sector <select> (canonical
  list + current value if off-list) in the quick-add dialog and the instruments edit form. New
  「AI 偵測產業類別」 button beside the field (BOTH forms — the shared dialog gives both entries):
  POST /api/instruments/ai-sector {symbol, name, market} → default-role structured completion,
  prompt (from the FU-D30 registry) enumerates the canonical vocabulary; reply must map to a
  canonical key (auto-fills the select; unmappable → leave selection unchanged + zh notice);
  failures/timeouts toast without blocking the form. No per-call model picker (owner 假設 1).
- **FU-D32 — deletion tiers (需求二).** Dialog offers 取消 / 移除(隱藏) / 永久移除. Hide =
  FU-D18 soft delete (unchanged). Permanent = route the retained `store.delete_instrument`
  (full cleanup, no orphans) via a new DELETE …?mode=purge (or /purge endpoint): ANY ledger
  history (incl. closed positions) → 422 `has_history` with the owner's explanatory copy
  (回溯/XIRR/股利/已實現報表引用); never-traded only; strong confirm = user must TYPE the symbol.
  Guard: if the symbol is also a benchmark key (pricing/benchmarks.py), purge the registry row
  but SKIP market-data deletion (prices/dividend_events) so the TWR benchmark series survives.
- **FU-D33 — AI-input inline quick registration (需求四).** The AI preview's unregistered-symbol
  rows gain an inline 立即註冊 action opening the SAME `inst-quickadd.js` dialog (with FU-D31's
  sector select + AI-detect button); on success the AI flow resumes automatically (re-preview the
  same text/images) — no re-entry.
- **FU-D34 — FX center balance + hard oversell block (需求五).** On account/from-ccy selection
  the 換匯中心 form shows that pool's current balance (from GET /api/cash). Sell amount must be
  ≤ balance: live frontend validation + backend hard 422 (`fx_insufficient_balance`). The
  ack_negative override is REMOVED for /api/cash/fx (spec: no financing/overdraft/negative
  conversion — this supersedes the prior verified override behavior for FX ONLY; movement
  withdrawals keep their existing semantics). Tests rewritten accordingly.
- **FU-D35 — dividend symbol picker (需求六).** New light endpoint (input_center):
  per-account {held:[{symbol,name}], closed:[{symbol,name}]} derived from the ledger. The 股利
  pane's 代號 field becomes a picker listing HELD symbols of the chosen account; a
  「顯示已清倉標的」 toggle adds historically-held-now-closed ones (owner 假設 2). Manual typing
  stays possible (fallback).
- **FU-D36 — scheduler run-now status feedback (需求七).** ALL schedule rows' 立即執行 get:
  trigger toast → per-row live status 已排入 → 執行中 → 成功/失敗 with last-run time + short
  result message (failure shows the error), fed from `job_runs` (+ in-flight state) via a status
  endpoint; frontend polls only while something is queued/running and stops when idle (owner
  假設 3).
- **FU-D37 — account display-name resolver (P1② pick).** New `web/names.js` single resolver
  (fed once from /api/input/context-style data or a static map served by ONE source); replace
  the three drifting per-file ACCOUNT_ZH-style maps. No golden-payload churn (frontend only);
  server-side display_name deferred to a future golden re-baseline.
- **FU-D38 — dividend income surfaces (P4 pick).** Dashboard gains: TTM dividend card + yearly
  received bar chart + ex-dividend calendar list — consuming the ALREADY-SERVED payload fields
  (DividendSummary.by_year, DividendProjection, ex_dividend_calendar — verify exact names);
  projections carry forecast-only labeling (rebate precedent). Frontend-only; display-only
  attribution (never feeds returns).
- **FU-D39 — multi-user prep.** THIS batch: Phase 0 only — the connection-audit guardrail test
  (pin the DB-open surface) + an architecture note. The market.db physical split (Phase 1) is
  DEFERRED to its own dedicated batch after v0.1.20 ships: it touches the pricing-read seams that
  feed valuation, and bundling it with a 9-item feature batch is a risk mismatch. (The only
  re-sequencing of the owner's approved picks; rationale recorded here.)

## Waves (file ownership disjoint per phase; agents never commit)

- Wave 1 (parallel): **W-A** FU-D30 prompts · **W-B** FU-D36 scheduler status · **W-C** FU-D34
  FX guard (owns cash.js/cash.py router) · **W-D** FU-D38 dividend surfaces (owns index.html +
  new card JS; not app.js) · **W-E** FU-D37 names resolver (owns names.js + map sweep; NOT
  cash.js, NOT index.html).
- Wave 2 (after W-A): **W-F** FU-D31 sector pack ∥ **W-G** FU-D35 dividend picker.
- Wave 3 (after W-F/W-G): **W-H** FU-D32 + FU-D33 (instruments + AI-input surfaces).
- Wave 4: **W-I** FU-D39 Phase 0.

## Acceptance

Full pytest, bare mypy --no-incremental (whole scope), ruff, stress phase 1 with-UI fail=0,
id-contract sweep, demo deploy + verify_live + browser probe; orchestrator deep coordinated
review before the owner report.
