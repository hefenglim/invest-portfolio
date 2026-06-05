# Rule: LLM Insight Generation

The LLM produces **qualitative synthesis** — insight cards / reports — from the
portfolio's computed numbers plus fetched news/sector/market information. It is a
narrator, not a calculator.

## Hard rules

1. **Batch only.** Insights are generated on a **manual trigger** or by the
   **scheduler**. Never called synchronously during a dashboard page render.
2. **Cache everything.** Persist output in the `insights` table keyed on an input
   fingerprint (portfolio snapshot + source articles + prompt version). The
   dashboard renders the cached result; it does not re-call the LLM to display.
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
