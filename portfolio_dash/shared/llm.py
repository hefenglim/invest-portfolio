"""LiteLLM client: structured output, retry, and usage/cost logging."""

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

import litellm as litellm  # re-exported so tests can monkeypatch litellm_mod.litellm
from pydantic import BaseModel, ValidationError

from portfolio_dash.shared.config import get_settings


class LLMUnavailable(Exception):
    """Raised when the LLM provider fails or returns unusable output."""


class ModelPricing(BaseModel):
    """Per-model token pricing (prices in USD per million tokens)."""

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
        (
            datetime.now(UTC).isoformat(),
            model,
            agent,
            input_tokens,
            output_tokens,
            str(cost),
        ),
    )
    conn.commit()


def complete_structured[T: BaseModel](
    prompt: str,
    schema: type[T],
    *,
    agent: str,
    conn: sqlite3.Connection | None = None,
    pricing: ModelPricing | None = None,
) -> T:
    """Call the configured LLM and parse the response into *schema*.

    Retries once on a parse failure.  Raises :exc:`LLMUnavailable` if the
    provider errors or two consecutive responses cannot be parsed.

    Usage is logged to *conn* (``llm_usage`` table) whenever both *conn* and
    *pricing* are supplied.
    """
    settings = get_settings()
    last_content = ""

    for _attempt in range(2):
        try:
            resp = litellm.completion(
                model=settings.llm_active_model or "gpt-4o-mini",
                api_base=settings.llm_endpoint or None,
                api_key=settings.llm_api_key or None,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # noqa: BLE001
            raise LLMUnavailable(f"provider error: {exc}") from exc

        last_content = resp.choices[0].message.content or ""

        if conn is not None and pricing is not None:
            usage = resp.usage
            log_usage(
                conn,
                model=settings.llm_active_model,
                agent=agent,
                input_tokens=usage.prompt_tokens,
                output_tokens=usage.completion_tokens,
                cost=cost_of(pricing, usage.prompt_tokens, usage.completion_tokens),
            )

        try:
            return schema.model_validate_json(last_content)
        except (ValidationError, json.JSONDecodeError, ValueError):
            continue

    raise LLMUnavailable(f"invalid structured output: {last_content[:200]}")
