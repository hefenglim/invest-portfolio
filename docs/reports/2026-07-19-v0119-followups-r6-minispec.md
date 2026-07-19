# v0.1.19 follow-ups — round 6 mini-spec (FU-D49..D54)

Date: 2026-07-19 · Owner sign-off: **「全部照建議」** on the orchestrator's Phase-0
analysis (Q1–Q6) covering the owner's r6 feedback + the formal spec
「交易輸入 — 新增標的 AI 智能判讀 需求規格書」. Branch: `feat/v0119-followups`.
Workflow: Phase-0 analysis → sign-off → DAG dispatch (≤3 concurrent, file-ownership
partitioned) → per-agent internal senior review → orchestrator audit → central gates.

## Root-cause finding that reframed the request

The reported bugs 「⚠ 2303 視為 2330（模糊比對）」 and 「⚠ 2883 視為 2882」 were NOT
LLM errors — the AI-input prompt (v3) returned the correct local codes. The local
resolver (`data_ingestion/resolve.py`) fuzzy-matched unregistered symbols against
registered instruments via `difflib.SequenceMatcher` at threshold 0.75, and any two
4-digit codes differing in ONE digit score exactly `2*3/8 = 0.75` — so unrelated
companies coerced. Fix direction (owner-signed Q1): exact-only for codes, kill the
coercion class, route unregistered input to the register-first / AI-resolve flow.

## Signed decisions

| ID | Decision |
| --- | --- |
| **FU-D49** (Q1) | Symbol resolution is **EXACT-only for code-shaped input** (any market format). `ResolutionStatus.FUZZY` and the 「視為」 soft-confirm coercion are removed. Name-shaped input yields ≤5 **non-binding** NAME-similarity suggestions (threshold 0.6), `instrument=None` for every non-exact outcome. New single source `shared/symbol_format.py`: TW `^\d{4,6}[A-Z]{0,2}$` · US `^[A-Z]{1,5}(\.[A-Z])?$` · MY `^\d{4}$` (consumed by agents.py format warnings, resolve gating, and the AI-resolve gate). Contract change: manual/CSV/AI drafts for near-miss codes are now hard `symbol_unresolved` blocks (register first), never auto-coerced. |
| **FU-D50** (spec + Q3/Q4/Q5) | **One unified AI instrument-resolve service**: single LLM call (temperature=0) returns `{symbol, name, gics_sector, gics_industry?, confidence, candidates[], not_found}`; market-conditional prompt (local-exchange-code mandate carried from AI-input v3); GICS-11 list embedded from `GICS_SECTOR_KEYS` (drift-guarded); prompt `ai_instrument_resolve` v1 in the unified registry, superseding `ai_sector` v1 + `ai_symbol_resolve` v1; LIBRARY_VERSION → official-v9. **Confidence gating** (Q3): high → auto-fill (editable); medium/low → 2–5 candidates; not_found → honest empty, never fabricate. **Provider verification** (Q5): even high-confidence codes must pass a real quote lookup before auto-fill; failure downgrades to candidates. **Single implementation, four entry points** (Q4): quick-add dialog (manual trade / AI-input row / CSV row) auto-triggers on format-fail or registry+lookup miss; the watchlist 「AI 偵測產業類別」 button re-points to the same service (sector/industry only). |
| **FU-D51** (Q2a/b/c) | **GICS 2023, 11 sectors** replace the FU-D31 15-key vocabulary. (a) Semiconductors → Information Technology, Shipping → Industrials — donut + `sector_weight` alert regroup accordingly (owner accepts, incl. a likely new 資訊科技 concentration alert on real data); GICS **industry** kept as an optional auxiliary column (`instruments.industry`, nullable). (b) **ETF** stays a special non-GICS bucket. (c) Stored sector values **actually migrated** (idempotent boot-seam rewrite, synonym-driven; blank/NULL deliberately left for the AI to fill; `alert_events` history intentionally not rewritten). English keys stored, zh dual labels in dropdowns; zh-everywhere still deferred to the server display_name phase. Golden re-baselined; churn = slice merges + one consequent sector_weight alert, arithmetic verified slice-by-slice. |
| **FU-D52** | FX form UX: account switch clears both amount fields; sell/buy currencies can never be equal (auto-flip both directions); single-currency accounts (tw_broker, moomoo_my_my) disable the whole form with 「無可換匯幣別 — 換匯需帳戶具備兩種以上幣別」. Invariant `confirm.disabled = over ∨ single` re-asserted at every `updFxBalance` seam (orchestrator-audit fix: late async callers previously re-enabled 確認). |
| **FU-D53** (Q6) | Manual-entry 草稿預覽 adds SERVER-computed `position_preview` (sell: 調整成本移除 / 已實現損益 / 剩餘股數; buy: 新持股 / 新原始均價 / 新調整均價 — same information model as the drawer 試算, but computed on the preview response via the build_book replay seam, not the frontend fee-mirror) + `account_cash` line for the trade's quote-ccy pool. **Display-only** (Q6): no new cash gating; booking behavior untouched. Additive contract only. |
| **FU-D54** | Multi-user Phase-0 blueprint revised (owner): per-user **folder** `user_trade/<UserLoginID>/ledger.db` — the folder is the unit of backup/restore for all user-derived data; `control.db` maps user → folder. Compatible with the existing `db_path.parent` derivation convention (no new path logic). Physical splits remain deferred (Phase 1 = market.db, own batch). Docs-only this round. |

## Known accepted consequences (record, do not "fix")

- MY format tightened to `^\d{4}$`: non-4-digit Bursa codes (ETF/warrant suffixes)
  now get the soft format warning; real resolution is handled by FU-D50's AI +
  provider verification. Warning-only, never rewrites.
- `looks_like_market_code("APPLE")` is True (US 5-letter false positive) — such input
  routes to the AI-resolve flow instead of name suggestions, per the formal spec.
- Between wave 1 and wave 2 on this branch, the transitional ai_sector prompt named
  old vocabulary keys; replies degrade gracefully (`mapped:false`). Resolved by FU-D50.

## Gates (round exit criteria)

1. Traceability: FU-D49..D54 each covered by tests named in the wave reports.
2. All wave senior-review + orchestrator-audit findings closed (audit fixes: W-D
   confirm re-enable leak; W-A/W-C accepted with recorded deviations).
3. Central: full pytest 0F/0E · bare `mypy --strict` (no cache, whole scope) · ruff ·
   stress with-UI fail=0 · golden churn justified per-slice.
4. Demo deploy + `verify_live` ALL PASS + live probe incl. the 2303/2883 regression
   cases against the real LLM; prod untouched (tag-pinned).
