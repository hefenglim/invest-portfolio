# Rule: LLM Insight Generation

The LLM produces **qualitative synthesis** — insight cards / reports — from the
portfolio's computed numbers plus fetched news/sector/market information. It is a
narrator, not a calculator.

## Hard rules

1. **Batch only.** Insights are generated on a **manual trigger** or by the
   **scheduler**. Never called synchronously during a dashboard page render.
2. **Cache everything.** Persist output in the `insights` table keyed on an input
   fingerprint — `sha256(insight_type_id + assembled prompt + input-snapshot digest +
   prompt_version)` (`llm_insight/insights_store.py`). News and every other qualitative
   input reach the key only through the assembled prompt, and the prompt is **day-anchored**,
   so re-triggering the same inputs on the same day is a cache hit (zero LLM calls) while a
   new trading day is a new key. The dashboard renders the cached result; it does not
   re-call the LLM to display. *(Wording corrected 2026-09-10 — it said 「snapshot + source
   articles + prompt version」, which named an input that is not a separate key component.)*
3. **The LLM never emits numbers of record.** Prices, P&L, returns, and weights are
   computed by `portfolio/` and passed *into* the prompt. The model reasons about
   them; it does not invent or recompute them.
4. **Graceful degradation.** If the LLM/provider is unavailable, the dashboard shows
   the last cached insight (with a timestamp) or an empty state — never an error page,
   never a fabricated card.

## Provider access via LiteLLM

- All calls go through **LiteLLM** using the OpenAI-compatible interface.
- Providers — OpenRouter / OpenAI-compatible / Anthropic — are selected by **config**
  (env/settings), with optional fallback ordering. Switching providers or models
  must require **no code change**.
- Keep model IDs, base URLs, and keys in settings, never hard-coded.
- **Retries are owned by `shared/llm.py`, never by LiteLLM** (DEF-066, 2026-09-26). Every
  `litellm.completion` call passes `num_retries=0` and nothing sets `litellm.num_retries`:
  LiteLLM reads `0 or litellm.num_retries`, and its retry path imports `tenacity` — not a
  dependency — at the moment a provider fails, so the retry raised
  「tenacity import failed」, hid the provider's real error, and the fallback model failed the
  same way (demo, 2026-09-25). It would also have retried every `openai.APIError`, 401 and
  400 included. `_call_provider` retries only transient failures (timeout, connection error,
  408/425/429, 5xx), at most the model's `max_retries` times, 0.5 s·2ⁿ backoff capped at 4 s.
  An `LLMUnavailable` names every candidate and its zh failure class
  (「主模型 X：…；備援 Y：…」); the provider's raw text goes only to the redacting fail log
  (owner, 2026-09-26). The settings ping deliberately does not retry: a connection test
  reports the first failure. Guarded by `tests/shared/test_def066_llm_retry.py`, which drives
  the REAL LiteLLM wrapper through its exception path with `mock_response=<Exception>` —
  every earlier LLM test replaced `litellm.completion` wholesale, so a dependency missing on
  a path that only runs on error could not be seen.

## Self-correction: shadow versions and promotion (spec 04 §4.6)

- **Shadow cards are never user-facing** (DEF-069). Every list read of `insights` goes
  through `insights_store._filters`, which excludes `is_shadow = 1` unless the caller passes
  `include_shadow=True`. Only the battle record (ai-score rows, the ai_predictions export)
  shows shadow rows, labelled 影子.
- **`max_shadows` caps TASKS concurrently in their shadow period** (owner ruling ⑧ = A,
  2026-09-26; DEF-070), never shadow cards ever stored. A task is in its period while its
  latest live calibration version is not the active one AND that version has fewer than
  `shadow_batches` scored evaluations of its OWN (`evaluations_store.version_score`;
  `shadow_batches` counts scored evaluations, owner 2026-09-26). A per_symbol batch is one
  slot. Promotion, archiving the shadow, the active version catching up, or a decided
  win/loss frees the slot; a task that finds the cap full queues (「影子排隊中（目前 N／上限
  M）」). Promotion compares the shadow version's own record against the active version's own
  record — never pooled history (owner, 2026-09-26). With one version and no active one, v1
  is adopted by hand (「設為生效」 on the task drawer's ④ 校正版本鏈, DEF-071), not shadowed.
- **Loop 3 writes a version from the ACTIVE version, and spends its evidence once**
  (DEF-079 / DEF-080, owner 2026-09-26). The trigger window
  (`evaluations_store.calibration_window`) is the active version's own scored evaluations
  (none active = cards with no calibration layer — their `calibration_version` is NULL, never
  1) scored after the task's newest calibration version, archived included, was written: the
  evaluations before that moment were that version's basis. The misses in the window are
  exactly what the master model is given, and the base text is the active version's body
  (none active = empty) — never the newest body, which may be an unadopted or losing shadow.
  No new version is written while the newest one is still shadow-evaluated or has won and
  awaits 設為生效. Before the rule the first version was written from 0 samples (8 misses
  scored under NULL, sampled under v1) and the same misses re-triggered a version every week.
- **User-visible text says 「AI」, never 「LLM」** (DEF-076, owner 2026-09-26): quota, cost,
  service, settings, action-log labels. 「LLM」 stays in code, comments, identifiers and
  docstrings; 「LiteLLM」 is a product name. Old action-log rows keep the words they were
  written with (ruling ④ a). Guarded by `tests/contract/test_def076_ai_wording_not_llm.py`.

## Structured output

- Define the insight card / report shape as a **Pydantic model**. Prompt the model
  to return **JSON only** (no prose, no Markdown fences); parse and validate against
  the model; on validation failure, retry once then fall back to cached/empty.
- Prompts use **XML-tagged structure**, explicit quantitative anchors, and a
  **one-shot JSON example** of the target schema. (Reuse the structured-prompting
  approach already proven in prior work.)
- Version the prompt; include the prompt version in the cache fingerprint so a prompt
  change invalidates stale cards.

## Inputs to a generation run

- Computed portfolio summary (holdings, weights, realized/unrealized P&L, returns) —
  from `portfolio/`.
- Qualitative context (sector/news/market info) — fetched separately and passed in.
  This is where web/news retrieval belongs, **not** for price numbers.

## Cost & latency discipline

- One generation run produces a batch of cards/sections; do not fan out into many
  small per-widget calls.
- Bound context: pass a compact computed summary, not raw transaction history or
  full article bodies — extract/trim first.
- Log token usage per run for cost visibility.
