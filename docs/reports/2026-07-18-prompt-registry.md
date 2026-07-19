# Site-wide AI prompt registry (FU-D30)

Date: 2026-07-18 · Branch: `feat/v0119-followups` · Wave W-A
Spec: `docs/reports/2026-07-18-v0119-followups-r4-minispec.md` → FU-D30 (owner 需求三)

## Goal

One authoritative index of **every** prompt the app sends to an LLM, over the two legitimate
tiers — (a) code-owned versioned defaults in the official library and (b) user-editable DB
prompts whose defaults come from tier (a). No feature may hardcode a prompt outside the
registry. This document is the inventory + the architecture note; the mechanism is
`portfolio_dash/llm_insight/official_templates.py::PROMPT_REGISTRY` guarded by
`tests/llm_insight/test_prompt_registry.py`.

## Where prompts live (the mechanism)

- **`llm_insight/official_templates.py` is the single code-owned library.** Every code-owned
  prompt constant lives there with a sibling `*_VERSION` tag. `LIBRARY_VERSION` (now
  `official-v7 (2026-07-18)`) tags the shipped default content as a whole.
- **`PROMPT_REGISTRY`** (typed `list[PromptRegistryEntry]` in the same module) is the
  enumerable index. Each entry declares `{key, feature, tier, version, agent,
  default_constant, storage, call_site}`, so the two-tier reality is explicit:
  code-owned entries point at a library constant (`storage=""`); user-editable / runtime
  entries name their DB table/row and (for editable) the library constant that seeds it.
- **User-editable prompts keep their live value in the DB.** The library constant is only the
  DEFAULT/seed; "reset to official" restores it. The DB row is the value of record — that IS
  the mechanism, so these defaults are NOT flattened into code.

## How features fetch a prompt

| Tier | Fetch path |
| --- | --- |
| code-owned | import the constant from `official_templates` and use it directly |
| user-editable | read the DB row via its accessor (`get_system_prompt`, `get_news_prompt`, composer `get_strategy`); the accessor seeds from the library constant on first touch |
| runtime-generated | the master writes the calibration body into `calibration_prompts`; `assemble_layers` appends the active version |

## Inventory — every LLM completion call site

Grep patterns used to find call sites (in `portfolio_dash/`):
`complete_text` · `complete_structured` · `complete_structured_meta` · `litellm.completion`.
The registry-completeness test scans for these identifiers (matching the identifier, not only
a literal `name(` call, because a site may reach the LLM through an injected `completer`).

| Feature | Registry key | Prompt / version | Tier | Source (default constant) | Live storage | Call site |
| --- | --- | --- | --- | --- | --- | --- |
| AI transaction-input parse | `ai_input` | `AI_INPUT_PROMPT_VERSION` (v2) | code-owned | `AI_INPUT_PROMPT_BODY` | — | `data_ingestion/agents.py:ai_agents_input` (injected `complete_structured`) |
| News organizer | `news_organizer` | `NEWS_ORGANIZER_PROMPT_VERSION` (v2) | user-editable | `NEWS_ORGANIZER_PROMPT` | `news_prompt_config` (id=1) | `news/organizer.py:organize` |
| Insight system prompt | `insight_system` | `SYSTEM_PROMPT_VERSION` (v2) | user-editable | `SYSTEM_PROMPT_BODY` | `system_prompt_config` (id=1) | `assemble.py:assemble_layers` (system layer) → `generate.py`; also `/api/prompts/test` |
| Insight strategy prompts | `insight_strategy` | per `STRATEGY_TEMPLATES[].version` (週報 v2.1 / 健檢 v2.5 / 市場 v1.1) | user-editable | `STRATEGY_TEMPLATES` | `strategy_prompts` / `insight_type_strategies` | `assemble.py:assemble_layers` (template layers) → `generate.py` |
| Insight self-correct calibration | `insight_calibration` | per `calibration_prompts.version` | runtime-generated | — (master-written) | `calibration_prompts` | `assemble.py:assemble_layers` (calibration layer) → `generate.py` |
| On-alert insight addendum | `insight_on_alert_note` | `ON_ALERT_NOTE_VERSION` (v1) | code-owned | `ON_ALERT_NOTE` | — | `llm_insight/generate.py:run_insight_type` |
| Master narrative score | `master_score` | `MASTER_SCORE_PROMPT_VERSION` (v2) | code-owned | `MASTER_SCORE_SYSTEM` | — | `llm_insight/master.py:score_narrative` |
| Master calibration generation | `master_calibrate` | `MASTER_CALIBRATION_PROMPT_VERSION` (v1) | code-owned | `MASTER_CALIBRATION_SYSTEM` | — | `llm_insight/master.py:generate_calibration` |
| Master calibration validator | `master_validate` | `MASTER_VALIDATE_PROMPT_VERSION` (v1) | code-owned | `MASTER_VALIDATE_SYSTEM` | — | `llm_insight/master.py:validate_calibration` |
| Digest daily one-liner | `digest_note` | `DIGEST_NOTE_PROMPT_VERSION` (digest-daily-note-v1) | code-owned | `DIGEST_NOTE_PROMPT_BODY` | — | `api/digest_service.py:_llm_note` |

### Call sites with no prompt content of their own (documented exemptions)

These reach `litellm` but do not introduce a shippable prompt, so they carry no registry
entry (recorded in the completeness test's `EXPECTED_CALL_SITES` as `EXEMPT`):

- **`shared/llm.py`** — the completion-helper definitions themselves; no prompt content.
- **`api/routers/prompts.py`** (`/api/prompts/test`) — the prompt tester. The USER body is
  runtime input (not a shipped prompt); the SYSTEM prompt it sends is the registered
  `insight_system` prompt.
- **`api/routers/llm_settings.py`** (`/api/llm/models/{alias}/test`) — a connectivity probe
  that sends the literal `"ping"`; not a prompt of record.

## Migration performed (stray literals → library)

All prompt content is byte-identical to its pre-migration form; only the home changed.

| Prompt | Was | Now |
| --- | --- | --- |
| Digest one-liner | inline literal in `digest_service._note_prompt` | `DIGEST_NOTE_PROMPT_BODY` (+ `_VERSION`); `_note_prompt` calls `.format(numbers=…)`; `_LLM_PROMPT_VERSION` aliases the library version |
| On-alert addendum | `_ON_ALERT_NOTE` literal in `generate.py` | `ON_ALERT_NOTE` (+ `_VERSION`); `generate.py` imports it as `_ON_ALERT_NOTE` |
| Master score / calibrate / validate systems | `_SCORE_SYSTEM` / `_CALIBRATION_SYSTEM` / `_VALIDATE_SYSTEM` literals in `master.py` | `MASTER_SCORE_SYSTEM` / `MASTER_CALIBRATION_SYSTEM` / `MASTER_VALIDATE_SYSTEM` (+ `_VERSION`s); `master.py` imports them under the same private names |

**Master-prompt placement decision:** the three master-role system prompts were MOVED into
`official_templates.py` (not merely referenced from `master.py`) for single-file
consolidation. No import cycle results: `official_templates` imports only `typing`, and
`master.py` already sits above it in the dependency order — verified `mypy --strict` clean.

## How to add a prompt (rules)

1. Add the prompt body to `official_templates.py` as a module constant WITH a sibling
   `*_VERSION` tag. Never inline a prompt literal at the call site.
2. If the prompt is **user-editable**, keep its live value in a DB row; the library constant
   is that row's default/seed (wire a `get_/set_/reset_` accessor that seeds from the
   constant). If **code-owned**, import the constant at the call site directly.
3. Register it in `PROMPT_REGISTRY` (fill every field). Bump `LIBRARY_VERSION` when default
   CONTENT changes.
4. If the prompt is reached through a NEW file/call site, add that file to
   `EXPECTED_CALL_SITES` in `tests/llm_insight/test_prompt_registry.py` (mapped to the new
   key, or an `EXEMPT:` reason for a probe/tester).
5. Run `pytest tests/llm_insight/test_prompt_registry.py` — it fails until every call site
   traces to a registry entry and every entry is well-formed.

A pointer comment at the top of `official_templates.py` mirrors these rules.

## Reserved slot — FU-D31 (wave W-F)

Round-4 wave **W-F** (FU-D31 sector pack) will add an `AI_SECTOR_PROMPT` for the
「AI 偵測產業類別」 button (`POST /api/instruments/ai-sector`). A clearly-marked reserved slot
is left in `PROMPT_REGISTRY` (see the `FU-D31 RESERVED SLOT` comment) with the exact entry to
fill in: `key="ai_sector"`, `tier="code-owned"`, `default_constant="AI_SECTOR_PROMPT"`,
`agent="instruments_ai_sector"`, `call_site="api/routers/instruments.py:ai_sector"`. W-F must
also add the new call-site file to `EXPECTED_CALL_SITES` in the completeness test.
