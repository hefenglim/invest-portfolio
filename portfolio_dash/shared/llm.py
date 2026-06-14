"""LiteLLM client: budget gate, role-based selection with fallback, vision, usage log."""

import base64
import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

import litellm as litellm  # re-exported so tests can monkeypatch llm_mod.litellm
from pydantic import BaseModel, ValidationError

from portfolio_dash.shared.llm_config import (
    AINotActivated,
    LLMBudgetExceeded,
    LLMError,
    LLMRole,
    LLMUnavailable,
    ModelConfig,
    check_budget,
    litellm_model_string,
    select_models,
    select_role_models,
)

# A role's fallback companion (spec 04.3): role selection tries the primary then this.
_ROLE_FALLBACK: dict[LLMRole, LLMRole] = {
    LLMRole.DEFAULT: LLMRole.DEFAULT_FALLBACK,
    LLMRole.VISION: LLMRole.VISION_FALLBACK,
    LLMRole.MASTER: LLMRole.MASTER_FALLBACK,
}


def _select_for(
    conn: sqlite3.Connection, *, role: LLMRole | None, vision: bool
) -> list[ModelConfig]:
    """Resolve the candidate model chain for a call.

    When *role* is given it selects that role's [primary, fallback] pair (spec 04.3 master
    path); otherwise it falls back to the legacy vision/default selection. A role with no
    registered fallback companion uses the default-fallback slot.
    """
    if role is not None:
        return select_role_models(conn, role, _ROLE_FALLBACK.get(role, LLMRole.DEFAULT_FALLBACK))
    return select_models(conn, vision=vision)

__all__ = [
    "AINotActivated",
    "LLMBudgetExceeded",
    "LLMError",
    "LLMRole",
    "LLMUnavailable",
    "ModelPricing",
    "TextCompletion",
    "complete_structured",
    "complete_text",
    "cost_of",
    "log_usage",
]


class ModelPricing(BaseModel):
    """Per-model token pricing (USD per million tokens)."""

    model_config = {"protected_namespaces": ()}

    model: str
    input_price_per_mtok: Decimal
    output_price_per_mtok: Decimal


def cost_of(pricing: ModelPricing, input_tokens: int, output_tokens: int) -> Decimal:
    """Return total USD cost for a single completion given token counts."""
    return (
        Decimal(input_tokens) * pricing.input_price_per_mtok
        + Decimal(output_tokens) * pricing.output_price_per_mtok
    ) / Decimal("1000000")


def log_usage(
    conn: sqlite3.Connection,
    *,
    model: str,
    agent: str,
    input_tokens: int,
    output_tokens: int,
    cost: Decimal,
) -> None:
    """Append one row to the ``llm_usage`` table and commit."""
    conn.execute(
        "INSERT INTO llm_usage (ts, model, agent, input_tokens, output_tokens, cost) "
        "VALUES (?,?,?,?,?,?)",
        (datetime.now(UTC).isoformat(), model, agent, input_tokens, output_tokens, str(cost)),
    )
    conn.commit()


def _response_format_for(schema: type[BaseModel]) -> dict[str, object]:
    """Build an OpenAI-style ``json_schema`` response_format from a Pydantic model.

    Used to FORCE structured output on providers that support it (spec 04.10). The schema
    name is the model's class name; the JSON schema is its ``model_json_schema()``.
    """
    return {
        "type": "json_schema",
        "json_schema": {
            "name": schema.__name__,
            "schema": schema.model_json_schema(),
        },
    }


def _supports_response_format(model: ModelConfig) -> bool:
    """True when the model's provider accepts a ``response_format`` json_schema.

    Probes ``litellm.supports_response_schema`` (capability lookup, not a network call);
    any probe failure is treated as "unsupported" so we degrade to plain prompt+parse
    rather than crash (graceful, spec 04.10).
    """
    try:
        return bool(litellm.supports_response_schema(model=litellm_model_string(model)))
    except Exception:  # noqa: BLE001 — an unclassifiable model degrades to no rf
        return False


def _build_messages(prompt: str, images: list[bytes] | None) -> list[dict[str, object]]:
    """Assemble the chat messages; multimodal content when images are present."""
    if not images:
        return [{"role": "user", "content": prompt}]
    content: list[dict[str, object]] = [{"type": "text", "text": prompt}]
    for img in images:
        b64 = base64.b64encode(img).decode("ascii")
        content.append(
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
        )
    return [{"role": "user", "content": content}]


def _complete_with[T: BaseModel](
    model: ModelConfig,
    messages: list[dict[str, object]],
    schema: type[T],
    *,
    agent: str,
    conn: sqlite3.Connection,
) -> T:
    """Try one model: call, log usage, parse (retry once). Raise LLMUnavailable on failure.

    When the provider supports it, a json_schema ``response_format`` derived from *schema*
    is sent to FORCE structured output (spec 04.10); unsupported providers fall back to the
    plain prompt+parse path. The schema-parse retry-once behaviour is unchanged.
    """
    extra: dict[str, object] = {}
    if _supports_response_format(model):
        extra["response_format"] = _response_format_for(schema)
    for _attempt in range(2):
        try:
            resp = litellm.completion(
                model=litellm_model_string(model),
                api_base=model.api_base or None,
                api_key=model.api_key or None,
                messages=messages,
                timeout=model.timeout_seconds,
                num_retries=model.max_retries or 0,
                max_tokens=model.max_output_tokens,
                **extra,
            )
        except Exception as exc:  # noqa: BLE001
            raise LLMUnavailable(f"provider error ({model.id}): {exc}") from exc

        content = resp.choices[0].message.content or ""
        usage = resp.usage
        log_usage(
            conn,
            model=model.model_name,
            agent=agent,
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
            cost=cost_of(
                ModelPricing(
                    model=model.model_name,
                    input_price_per_mtok=model.input_price_per_mtok,
                    output_price_per_mtok=model.output_price_per_mtok,
                ),
                usage.prompt_tokens,
                usage.completion_tokens,
            ),
        )
        try:
            return schema.model_validate_json(content)
        except (ValidationError, json.JSONDecodeError, ValueError):
            continue
    raise LLMUnavailable(f"invalid structured output from {model.id}")


def complete_structured[T: BaseModel](
    prompt: str,
    schema: type[T],
    *,
    agent: str,
    conn: sqlite3.Connection,
    images: list[bytes] | None = None,
    role: LLMRole | None = None,
) -> T:
    """Call the configured LLM and parse the response into *schema*.

    Order: budget gate -> role selection (the explicit *role* chain if given, else vision
    when *images*, else default) -> try each candidate model in order (failover on provider
    error) -> parse (retry once) -> log cost.

    *role* (spec 04.3) selects an alternate model chain (e.g. ``LLMRole.MASTER`` for
    scoring/calibration); omitting it preserves the existing default/vision behaviour.

    Raises :exc:`AINotActivated` (no model for the role), :exc:`LLMBudgetExceeded`
    (cap hit), or :exc:`LLMUnavailable` (all candidates failed). All subclass
    :exc:`LLMError`, so callers may catch the base for graceful degradation.
    """
    check_budget(conn)
    candidates = _select_for(conn, role=role, vision=bool(images))
    messages = _build_messages(prompt, images)
    last: LLMUnavailable | None = None
    for model in candidates:
        try:
            return _complete_with(model, messages, schema, agent=agent, conn=conn)
        except LLMUnavailable as exc:
            last = exc
    raise last or LLMUnavailable("no model produced valid output")


class TextCompletion(BaseModel):
    """A free-text LLM reply plus its usage/cost (no JSON schema parsing)."""

    model_config = {"protected_namespaces": ()}

    reply: str
    model: str
    tokens_in: int
    tokens_out: int
    cost: Decimal


def _text_with(
    model: ModelConfig,
    messages: list[dict[str, object]],
    *,
    agent: str,
    conn: sqlite3.Connection,
) -> TextCompletion:
    """Try one model for a free-text reply: call, log usage, return content + cost.

    Mirrors :func:`_complete_with` minus the JSON parse / retry (there is no schema to
    validate). Raises :exc:`LLMUnavailable` on a provider error.
    """
    try:
        resp = litellm.completion(
            model=litellm_model_string(model),
            api_base=model.api_base or None,
            api_key=model.api_key or None,
            messages=messages,
            timeout=model.timeout_seconds,
            num_retries=model.max_retries or 0,
            max_tokens=model.max_output_tokens,
        )
    except Exception as exc:  # noqa: BLE001
        raise LLMUnavailable(f"provider error ({model.id}): {exc}") from exc

    content = resp.choices[0].message.content or ""
    usage = resp.usage
    cost = cost_of(
        ModelPricing(
            model=model.model_name,
            input_price_per_mtok=model.input_price_per_mtok,
            output_price_per_mtok=model.output_price_per_mtok,
        ),
        usage.prompt_tokens,
        usage.completion_tokens,
    )
    log_usage(
        conn,
        model=model.model_name,
        agent=agent,
        input_tokens=usage.prompt_tokens,
        output_tokens=usage.completion_tokens,
        cost=cost,
    )
    return TextCompletion(
        reply=content,
        model=model.model_alias,
        tokens_in=usage.prompt_tokens,
        tokens_out=usage.completion_tokens,
        cost=cost,
    )


def complete_text(
    prompt: str,
    *,
    agent: str,
    conn: sqlite3.Connection,
    system: str | None = None,
    role: LLMRole | None = None,
) -> TextCompletion:
    """Free-text completion (no JSON schema) via the configured text model.

    Order: budget gate -> role selection (the explicit *role* chain if given, else the
    default text role) -> try each candidate (failover on provider error) -> log cost ->
    return reply + usage. An optional *system* message is prepended. *role* (spec 04.3)
    selects an alternate chain (e.g. the master review pass). Raises :exc:`AINotActivated`
    (no model), :exc:`LLMBudgetExceeded` (cap hit), or :exc:`LLMUnavailable` (all
    candidates failed) — callers map these to 402 / 409 / 503 via the global handlers.
    """
    check_budget(conn)
    candidates = _select_for(conn, role=role, vision=False)
    messages: list[dict[str, object]] = []
    if system is not None:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    last: LLMUnavailable | None = None
    for model in candidates:
        try:
            return _text_with(model, messages, agent=agent, conn=conn)
        except LLMUnavailable as exc:
            last = exc
    raise last or LLMUnavailable("no model produced a reply")
