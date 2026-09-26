"""LiteLLM client: budget gate, role-based selection with fallback, vision, usage log."""

import base64
import json
import logging
import sqlite3
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import litellm as litellm  # re-exported so tests can monkeypatch llm_mod.litellm
from litellm import exceptions as litellm_errors
from pydantic import BaseModel, ValidationError

from portfolio_dash.shared import llm_fail_log as fail_log
from portfolio_dash.shared.clock import app_now
from portfolio_dash.shared.image_types import PNG, sniff_image_mime
from portfolio_dash.shared.llm_config import (
    AINotActivated,
    LLMBudgetExceeded,
    LLMError,
    LLMRole,
    LLMUnavailable,
    ModelConfig,
    check_budget,
    get_model,
    litellm_model_string,
    select_models,
    select_role_models,
)

logger = logging.getLogger(__name__)

# A role's fallback companion (spec 04.3): role selection tries the primary then this.
_ROLE_FALLBACK: dict[LLMRole, LLMRole] = {
    LLMRole.DEFAULT: LLMRole.DEFAULT_FALLBACK,
    LLMRole.VISION: LLMRole.VISION_FALLBACK,
    LLMRole.MASTER: LLMRole.MASTER_FALLBACK,
}


def _select_for(
    conn: sqlite3.Connection,
    *,
    role: LLMRole | None,
    vision: bool,
    model_override: str | None = None,
) -> list[ModelConfig]:
    """Resolve the candidate model chain for a call.

    When *role* is given it selects that role's [primary, fallback] pair (spec 04.3 master
    path); otherwise it falls back to the legacy vision/default selection. A role with no
    registered fallback companion uses the default-fallback slot.

    *model_override* (FU-D20 per-run model picker) names an explicit registry alias: when it
    resolves to an ENABLED model that model is placed at the HEAD of the chain and the
    role/vision chain remains as the fallback. A blank / unknown / disabled override is
    ignored (the API validates + rejects an invalid pick up front — this is a defensive
    no-op). When the override is the only usable model (the role chain is unset →
    :exc:`AINotActivated`) the override alone is returned rather than propagating the
    not-activated refusal. With ``model_override=None`` the path is byte-identical to before.
    """
    override = get_model(conn, model_override) if model_override else None
    if override is not None and not override.enabled:
        override = None
    try:
        if role is not None:
            base = select_role_models(
                conn, role, _ROLE_FALLBACK.get(role, LLMRole.DEFAULT_FALLBACK)
            )
        else:
            base = select_models(conn, vision=vision)
    except AINotActivated:
        if override is None:
            raise
        return [override]
    return [override, *base] if override is not None else base

__all__ = [
    "AINotActivated",
    "LLMBudgetExceeded",
    "LLMError",
    "LLMRole",
    "LLMUnavailable",
    "ModelPricing",
    "StructuredCompletion",
    "TextCompletion",
    "complete_structured",
    "complete_structured_meta",
    "complete_text",
    "cost_of",
    "is_transient",
    "log_usage",
    "provider_failure_zh",
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


def cached_tokens_of(usage: object) -> int:
    """Provider-reported cached prompt tokens from a LiteLLM usage object (0 when absent).

    LiteLLM normalizes OpenAI-style ``prompt_tokens_details.cached_tokens``; some
    providers expose ``cache_read_input_tokens`` instead. Both read defensively — a
    missing field is 0, never an error (the ledger must not break a call).
    """
    details = getattr(usage, "prompt_tokens_details", None)
    value = getattr(details, "cached_tokens", None) if details is not None else None
    if value is None:
        value = getattr(usage, "cache_read_input_tokens", None)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def log_usage(
    conn: sqlite3.Connection,
    *,
    model: str,
    agent: str,
    input_tokens: int,
    output_tokens: int,
    cost: Decimal,
    cache_tokens: int = 0,
) -> int:
    """Append one row to the ``llm_usage`` table, commit, and return its row id.

    The id is returned so a failure capture (:mod:`shared.llm_fail_log`) can point at the
    call it was billed for: a failed structured call is logged HERE before it is parsed,
    so every captured failure has a usage row and the two reconcile. Callers that do not
    need the link ignore the value.
    """
    cur = conn.execute(
        "INSERT INTO llm_usage (ts, model, agent, input_tokens, output_tokens, cost, "
        "cache_tokens) VALUES (?,?,?,?,?,?,?)",
        (app_now().isoformat(), model, agent, input_tokens, output_tokens, str(cost),
         cache_tokens),
    )
    conn.commit()
    usage_id = int(cur.lastrowid or 0)
    # Structured log of the LLM call (spec 19.4): one point covers both call paths
    # (complete_structured + complete_text) — same values written to the DB row, cost as
    # its canonical string. Logging only; no LLM behaviour or numbers change.
    logger.info(
        "llm_usage",
        extra={
            "agent": agent,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost": str(cost),
        },
    )
    return usage_id


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


def _json_instruction(schema: type[BaseModel]) -> str:
    """The provider-agnostic structured-output contract appended to every structured call.

    ``response_format`` is only sent when LiteLLM's capability map says the model supports
    it — which is ``False`` for every ``openrouter/*`` id — so the prompt itself must always
    carry the JSON-only contract (llm-insight.md: "return JSON only, no fences"). Redundant
    when response_format IS honoured; decisive when it is not.
    """
    return (
        "\n\n<output_format>\n"
        "Respond with ONLY one JSON object that conforms to the JSON Schema below.\n"
        "No markdown code fences, no commentary, nothing before or after the JSON object.\n"
        f"{json.dumps(schema.model_json_schema(), ensure_ascii=False)}\n"
        "</output_format>"
    )


def _extract_json(content: str) -> str:
    """Best-effort recovery of the JSON object from a non-conforming reply.

    Models occasionally ignore the no-fence instruction (``` fences, or prose around the
    object). Strip one outer fence pair, else slice from the first ``{`` to the last ``}``.
    Returns the input unchanged when no candidate is found; the caller treats a second
    parse failure as this attempt failed.
    """
    text = content.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        last_fence = text.rfind("```")
        if first_nl != -1 and last_fence > first_nl:
            text = text[first_nl + 1 : last_fence].strip()
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]
    return text


def _build_messages(prompt: str, images: list[bytes] | None) -> list[dict[str, object]]:
    """Assemble the chat messages; multimodal content when images are present.

    The data-URI's MIME comes from the bytes (:mod:`shared.image_types`), not from a constant.
    It was hardcoded ``image/png`` while the intake door had already sniffed the real format —
    so a pasted JPEG went out labelled as a PNG. Lenient providers sniff and shrug; a strict
    one rejects it, and the failure then surfaces as a vision/parse error nowhere near the
    mislabel. An unrecognised payload keeps PNG as the last resort: the door rejects non-images
    before they get here, so reaching this branch means a direct caller skipped that check, and
    a wrong label is still better than no message at all.
    """
    if not images:
        return [{"role": "user", "content": prompt}]
    content: list[dict[str, object]] = [{"type": "text", "text": prompt}]
    for img in images:
        b64 = base64.b64encode(img).decode("ascii")
        mime = sniff_image_mime(img) or PNG
        content.append(
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
        )
    return [{"role": "user", "content": content}]


# --- the ONE provider call + the owned retry (DEF-066, 2026-09-26) ---------------------
# litellm's own retry is OFF (``num_retries=0`` on every call). With ``num_retries > 0`` its
# wrapper sends ANY ``openai.APIError`` — a 401 and a 400 included — to
# ``litellm.completion_with_retries``, which imports ``tenacity`` at run time: a package this
# project never installed (and litellm does not require). The provider's real error was then
# replaced by 「tenacity import failed」, nothing was retried, and the fallback model failed
# the same way. The wrapper also rewrites the PROCESS-WIDE ``litellm.num_retries = None`` on
# that path. Rejected: adding ``tenacity`` as a dependency (``stack.md``: default answer no —
# and it would keep retrying requests that can never succeed). ⚠ ``0`` is falsy and litellm
# reads ``num_retries or litellm.num_retries``, so nothing may set that global either
# (``tests/shared/test_def066_llm_retry.py`` guards both).

#: Status codes worth a second try; every other 4xx is a request that will fail again.
_TRANSIENT_STATUS = frozenset({408, 425, 429})
_RETRY_BASE_DELAY_S = 0.5
_RETRY_MAX_DELAY_S = 4.0
#: The backoff sleeper — a module attribute so a test zeroes the wait (never real time).
_sleep: Callable[[float], None] = time.sleep


def _status_of(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


def is_transient(exc: BaseException) -> bool:
    """True for a failure a retry can fix: timeout, connection error, 408/425/429, 5xx."""
    if isinstance(exc, litellm_errors.Timeout | litellm_errors.APIConnectionError):
        return True
    status = _status_of(exc)
    return status is not None and (status in _TRANSIENT_STATUS or 500 <= status <= 599)


def provider_failure_zh(exc: BaseException) -> tuple[str, str | None]:
    """The owner-facing name of one provider failure — ``(name, code)`` — in Chinese.

    Never the provider's own text: it is English, and an auth error is free to echo the key
    it was sent. That text goes to the redacting fail log (:mod:`shared.llm_fail_log`)."""
    if isinstance(exc, litellm_errors.Timeout):
        return "回應逾時", None
    if isinstance(exc, litellm_errors.APIConnectionError):
        return "無法連線到供應商", None
    if isinstance(exc, litellm_errors.ContextWindowExceededError):
        return "輸入超過模型可處理的長度", "HTTP 400"
    status = _status_of(exc)
    if status is None:
        return "呼叫失敗", type(exc).__name__
    names = {
        400: "請求內容被拒", 401: "金鑰無效或未授權", 402: "供應商帳戶餘額不足",
        403: "沒有使用權限", 404: "找不到模型或端點", 408: "回應逾時",
        422: "請求內容被拒", 429: "請求過於頻繁",
    }
    name = names.get(status) or ("供應商服務異常" if status >= 500 else "供應商錯誤")
    return name, f"HTTP {status}"


class _ProviderCallFailed(Exception):
    """Every attempt of one model's provider call failed; carries each exception in order."""

    def __init__(self, errors: list[Exception]) -> None:
        super().__init__(repr(errors[-1]))
        self.errors = errors

    @property
    def last(self) -> Exception:
        return self.errors[-1]

    def reason_zh(self) -> str:
        """「供應商服務異常（HTTP 503，已重試 2 次）」 — the name, its code, the retries."""
        name, code = provider_failure_zh(self.last)
        parts = [p for p in (code,) if p]
        if len(self.errors) > 1:
            parts.append(f"已重試 {len(self.errors) - 1} 次")
        return f"{name}（{'，'.join(parts)}）" if parts else name

    def log_text(self) -> str:
        """Every attempt's ``repr`` for the fail log (redacted there), oldest first."""
        return " | ".join(repr(e) for e in self.errors)


def _call_provider(
    model: ModelConfig, messages: list[dict[str, object]], extra: dict[str, object]
) -> Any:
    """Call the provider once, retrying ONLY a transient failure, at most ``max_retries``.

    Backoff 0.5 s, 1 s, 2 s … capped at 4 s. Raises :class:`_ProviderCallFailed` with every
    attempt's exception when the last one fails (or the first non-transient one does).
    """
    retries = max(0, model.max_retries or 0)
    errors: list[Exception] = []
    for n in range(retries + 1):
        try:
            return litellm.completion(
                model=litellm_model_string(model),
                api_base=model.api_base or None,
                api_key=model.api_key or None,
                messages=messages,
                timeout=model.timeout_seconds,
                num_retries=0,  # DEF-066: the retry is owned here, never by litellm
                max_tokens=model.max_output_tokens,
                **extra,
            )
        except Exception as exc:  # noqa: BLE001 — classified below, never swallowed
            errors.append(exc)
            if n == retries or not is_transient(exc):
                raise _ProviderCallFailed(errors) from exc
            _sleep(min(_RETRY_BASE_DELAY_S * (2**n), _RETRY_MAX_DELAY_S))
    raise AssertionError("unreachable: the loop returns or raises")  # pragma: no cover


#: ``_parse_outcome`` -> the owner-facing name of a reply that could not be used.
_PARSE_ZH = {"invalid_json": "回應不是完整 JSON", "schema_mismatch": "回應欄位不符格式"}


def _parse_failures_zh(outcomes: list[str]) -> str:
    """「回應不是完整 JSON（2 次）」 — each parse-failure kind once, with its count."""
    counts: dict[str, int] = {}
    for o in outcomes:
        counts[o] = counts.get(o, 0) + 1
    return "、".join(f"{_PARSE_ZH.get(k, '回應無法解析')}（{n} 次）" for k, n in counts.items())


def _chain_failure_zh(failures: list[tuple[str, str]]) -> str:
    """「主模型 a：…；備援 b：…」 — every candidate tried, in order, with its own reason.

    Failover used to keep only the LAST exception, so the sentence named the fallback's
    failure and never the primary's — usually the one worth reading (DEF-066)."""
    many = len(failures) > 2
    parts = []
    for i, (alias, reason) in enumerate(failures):
        role = "主模型" if i == 0 else (f"備援 {i}" if many else "備援")
        parts.append(f"{role} {alias}：{reason}")
    return "；".join(parts)


class StructuredCompletion[T: BaseModel](BaseModel):
    """A parsed structured reply plus the metadata of the model that produced it.

    ``model`` is the model ALIAS (the user-facing registry name, e.g. ``claude-sonnet``),
    the value callers persist as the record's model column (spec 04 fix: the insights row's
    ``model`` is the model used, never a card field). ``cost`` is this single call's USD cost.
    """

    model_config = {"protected_namespaces": (), "arbitrary_types_allowed": True}

    value: T
    model: str
    cost: Decimal
    tokens_in: int = 0
    tokens_out: int = 0


def _parse_outcome(exc: Exception) -> str:
    """Tell "not JSON at all" from "JSON with the wrong fields".

    Do NOT switch on the exception class here. Pydantic v2 raises ``ValidationError`` for
    BOTH shapes — a malformed document arrives as an error whose ``type`` is
    ``json_invalid``, never as a bare ``json.JSONDecodeError`` (measured 2026-08-28, after
    the obvious ``isinstance`` version was written and would have labelled every bad reply
    a schema mismatch). The two are different defects and different training signals, so
    the classification reads the error's own type.
    """
    if isinstance(exc, ValidationError):
        if any(d.get("type") == "json_invalid" for d in exc.errors()):
            return "invalid_json"
        return "schema_mismatch"
    return "invalid_json"


def _complete_with_meta[T: BaseModel](
    model: ModelConfig,
    messages: list[dict[str, object]],
    schema: type[T],
    *,
    agent: str,
    conn: sqlite3.Connection,
    temperature: float | None = None,
) -> StructuredCompletion[T]:
    """Try one model: call, log usage, parse (retry once); return value + model alias + cost.

    When the provider supports it, a json_schema ``response_format`` derived from *schema*
    is sent to FORCE structured output (spec 04.10); unsupported providers fall back to the
    plain prompt+parse path. The schema-parse retry-once behaviour is unchanged. An explicit
    *temperature* (e.g. ``0`` for a deterministic classification/resolve call) is forwarded to
    the provider; ``None`` leaves the provider default untouched (byte-identical to before).
    Raises :exc:`LLMUnavailable` on a provider/parse failure.
    """
    extra: dict[str, object] = {}
    if _supports_response_format(model):
        extra["response_format"] = _response_format_for(schema)
    if temperature is not None:
        extra["temperature"] = temperature
    # Captured once per attempt (:mod:`shared.llm_fail_log`). Recorded HERE, at the
    # per-model layer, not at the chain layer above: failover keeps only the LAST
    # exception, so a chain-level capture would lose the primary model's failure
    # entirely — which is usually the one worth reading.
    prompt_text, image_count = fail_log.prompt_text_of(messages)
    # DEF-066: what went wrong on THIS model, in the owner's words, attempt by attempt —
    # the chain joins it with the other candidates' reasons into one sentence.
    parse_failures: list[str] = []
    for attempt in range(1, 3):
        try:
            resp = _call_provider(model, messages, extra)
        except _ProviderCallFailed as failed:
            fail_log.record(
                conn, agent=agent, outcome="provider_error", model=model.model_name,
                attempt=attempt, prompt=prompt_text, error_reason=failed.log_text(),
                image_count=image_count,
            )
            reasons = [r for r in (_parse_failures_zh(parse_failures),) if r]
            reasons.append(failed.reason_zh())
            raise LLMUnavailable("、".join(reasons)) from failed.last

        # `choices` / `usage` are read INSIDE the try deliberately. They used to sit
        # outside it, so a malformed provider envelope raised a bare AttributeError /
        # IndexError that never became LLMUnavailable: it escaped to the global catch-all
        # as an HTTP 500 instead of the intended 503 degrade (found 2026-08-28).
        try:
            content = resp.choices[0].message.content or ""
            usage = resp.usage
            tokens_in, tokens_out = usage.prompt_tokens, usage.completion_tokens
        except (AttributeError, IndexError, KeyError, TypeError) as exc:
            fail_log.record(
                conn, agent=agent, outcome="provider_error", model=model.model_name,
                attempt=attempt, prompt=prompt_text,
                error_reason=f"malformed response envelope: {exc!r}",
                image_count=image_count,
            )
            raise LLMUnavailable("供應商回應格式異常") from exc

        cost = cost_of(
            ModelPricing(
                model=model.model_name,
                input_price_per_mtok=model.input_price_per_mtok,
                output_price_per_mtok=model.output_price_per_mtok,
            ),
            tokens_in,
            tokens_out,
        )
        usage_id = log_usage(
            conn,
            model=model.model_name,
            agent=agent,
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            cost=cost,
            cache_tokens=cached_tokens_of(usage),
        )
        try:
            parsed = schema.model_validate_json(content)
        except (ValidationError, json.JSONDecodeError, ValueError):
            try:
                parsed = schema.model_validate_json(_extract_json(content))
            except (ValidationError, json.JSONDecodeError, ValueError) as exc:
                # The two shapes are recorded SEPARATELY (see `_parse_outcome`): the one
                # generic message they used to share could not tell a prompt author
                # whether the model had emitted garbage or the wrong fields.
                outcome = _parse_outcome(exc)
                parse_failures.append(outcome)
                fail_log.record(
                    conn, agent=agent, model=model.model_name, attempt=attempt,
                    outcome=outcome,
                    prompt=prompt_text, raw_output=content, error_reason=repr(exc),
                    image_count=image_count, usage_id=usage_id,
                )
                continue
        if fail_log.capture_all():
            fail_log.record(
                conn, agent=agent, outcome="ok", model=model.model_name,
                attempt=attempt, prompt=prompt_text, raw_output=content,
                image_count=image_count, usage_id=usage_id,
            )
        return StructuredCompletion(
            value=parsed, model=model.model_alias, cost=cost,
            tokens_in=tokens_in, tokens_out=tokens_out,
        )
    raise LLMUnavailable(_parse_failures_zh(parse_failures))


def _complete_with[T: BaseModel](
    model: ModelConfig,
    messages: list[dict[str, object]],
    schema: type[T],
    *,
    agent: str,
    conn: sqlite3.Connection,
) -> T:
    """Try one model and return only the parsed value (thin wrapper over the meta core)."""
    return _complete_with_meta(model, messages, schema, agent=agent, conn=conn).value


def complete_structured_meta[T: BaseModel](
    prompt: str,
    schema: type[T],
    *,
    agent: str,
    conn: sqlite3.Connection,
    images: list[bytes] | None = None,
    role: LLMRole | None = None,
    model_override: str | None = None,
    temperature: float | None = None,
) -> StructuredCompletion[T]:
    """Like :func:`complete_structured`, but also returns the model alias + this call's cost.

    Use this when the caller must persist WHICH model produced the card (insights.model)
    and/or attribute the per-call cost without a separate ``llm_usage`` lookup. Same gate /
    role-selection / failover / parse semantics; same exceptions.

    *model_override* (FU-D20) is an explicit registry alias put at the head of the candidate
    chain (the role/vision chain stays as fallback); ``None`` = the existing behaviour.
    *temperature* (e.g. ``0`` for a deterministic classify/resolve call) is forwarded to the
    provider on every candidate; ``None`` leaves the provider default.
    """
    # The pre-call gates are captured here rather than per-model: they refuse before any
    # candidate is chosen, so there is no model to attribute them to.
    try:
        check_budget(conn)
        candidates = _select_for(
            conn, role=role, vision=bool(images), model_override=model_override
        )
    except (LLMBudgetExceeded, AINotActivated) as exc:
        fail_log.record(
            conn, agent=agent, outcome=exc.kind, prompt=prompt,
            error_reason=repr(exc), image_count=len(images or ()),
        )
        raise
    messages = _build_messages(prompt + _json_instruction(schema), images)
    failures: list[tuple[str, str]] = []
    last: LLMUnavailable | None = None
    for model in candidates:
        try:
            return _complete_with_meta(
                model, messages, schema, agent=agent, conn=conn, temperature=temperature
            )
        except LLMUnavailable as exc:
            last = exc
            failures.append((model.model_alias, str(exc)))
    if last is None:
        raise LLMUnavailable("沒有可用的模型")
    # Every candidate's reason, not only the last one's (DEF-066); ``kind`` is unchanged.
    raise LLMUnavailable(_chain_failure_zh(failures)) from last


def complete_structured[T: BaseModel](
    prompt: str,
    schema: type[T],
    *,
    agent: str,
    conn: sqlite3.Connection,
    images: list[bytes] | None = None,
    role: LLMRole | None = None,
    model_override: str | None = None,
    temperature: float | None = None,
) -> T:
    """Call the configured LLM and parse the response into *schema*.

    Order: budget gate -> model selection (an explicit *model_override* at the head of the
    chain if given; then the *role* chain if given, else vision when *images*, else default)
    -> try each candidate model in order (failover on provider error) -> parse (retry once)
    -> log cost.

    *role* (spec 04.3) selects an alternate model chain (e.g. ``LLMRole.MASTER`` for
    scoring/calibration); omitting it preserves the existing default/vision behaviour.
    *model_override* (FU-D20) forces a specific enabled registry alias first; ``None`` = the
    existing behaviour. *temperature* (e.g. ``0``) is forwarded to the provider; ``None`` =
    the provider default.

    Raises :exc:`AINotActivated` (no model for the role), :exc:`LLMBudgetExceeded`
    (cap hit), or :exc:`LLMUnavailable` (all candidates failed). All subclass
    :exc:`LLMError`, so callers may catch the base for graceful degradation.
    """
    return complete_structured_meta(
        prompt, schema, agent=agent, conn=conn, images=images, role=role,
        model_override=model_override, temperature=temperature,
    ).value


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
    prompt_text, image_count = fail_log.prompt_text_of(messages)
    try:
        resp = _call_provider(model, messages, {})
    except _ProviderCallFailed as failed:
        fail_log.record(
            conn, agent=agent, outcome="provider_error", model=model.model_name,
            prompt=prompt_text, error_reason=failed.log_text(), image_count=image_count,
        )
        raise LLMUnavailable(failed.reason_zh()) from failed.last

    # Inside the try for the same reason as the structured path: a malformed envelope
    # must degrade as 503, not escape as a 500.
    try:
        content = resp.choices[0].message.content or ""
        usage = resp.usage
    except (AttributeError, IndexError, KeyError, TypeError) as exc:
        fail_log.record(
            conn, agent=agent, outcome="provider_error", model=model.model_name,
            prompt=prompt_text, error_reason=f"malformed response envelope: {exc!r}",
            image_count=image_count,
        )
        raise LLMUnavailable("供應商回應格式異常") from exc

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
        cache_tokens=cached_tokens_of(usage),
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
    try:
        check_budget(conn)
        candidates = _select_for(conn, role=role, vision=False)
    except (LLMBudgetExceeded, AINotActivated) as exc:
        fail_log.record(
            conn, agent=agent, outcome=exc.kind, prompt=prompt, error_reason=repr(exc),
        )
        raise
    messages: list[dict[str, object]] = []
    if system is not None:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    failures: list[tuple[str, str]] = []
    last: LLMUnavailable | None = None
    for model in candidates:
        try:
            return _text_with(model, messages, agent=agent, conn=conn)
        except LLMUnavailable as exc:
            last = exc
            failures.append((model.model_alias, str(exc)))
    if last is None:
        raise LLMUnavailable("沒有可用的模型")
    raise LLMUnavailable(_chain_failure_zh(failures)) from last
