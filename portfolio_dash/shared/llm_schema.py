"""The JSON Schema a structured call SENDS as ``response_format`` — the portable subset.

Why this exists (DEF-083, 2026-09-29)
-------------------------------------
``shared.llm`` forces structured output by sending ``response_format = {"type":
"json_schema", ...}`` whenever LiteLLM's capability map says the model supports it — and it
says so for ``openrouter/anthropic/*`` and ``openrouter/google/*`` alike. The schema used to
be ``model_json_schema()`` verbatim, which pydantic writes for pydantic's own validator, not
for a provider's grammar compiler:

* every ``Decimal`` field carries ``pattern: ^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$`` — a negative
  LOOKAHEAD. Anthropic (direct, Azure, Google-hosted) answers HTTP 400 「Invalid regex in
  pattern field: Quantifier '?' without preceding element」;
* the AI door's discriminated union is ``oneOf`` + ``discriminator`` — Amazon Bedrock
  answers 400 「Schema type 'oneOf' is not supported」.

So every structured call routed to the fallback model (haiku-4.5 behind OpenRouter, the
fallback of all three roles on the demo) was refused before the model saw it: a primary
failure could never be rescued (G-09, run #221), and the AI door silently skipped a model the
owner had picked by hand (#520).

The subset
----------
Anthropic's structured outputs are the strictest route this app reaches, so their documented
limits define the subset (platform.claude.com › structured outputs › JSON Schema
limitations, read 2026-09-29): basic types; ``enum`` of primitives; ``const``; ``anyOf`` /
``allOf``; string ``format`` from a fixed list; ``required``; ``additionalProperties`` that
MUST be ``false`` on every object; ``minItems`` of 0 or 1 only. NOT supported: ``pattern``
and every other string/number constraint, ``maxItems``, ``oneOf``, ``not``, ``type`` arrays,
recursive schemas. Bedrock's refusal of ``oneOf`` falls inside the same line. On top of that,
references are INLINED — ``allOf`` beside a ``$ref`` is refused, and a reference-free schema
cannot differ between providers in how it resolves one.

What is lost, and why it is not lost
------------------------------------
Nothing is weakened where it matters: the prompt still carries the FULL schema as text
(``shared.llm._json_instruction`` — the model reads every constraint), and the reply is
validated against the full pydantic model after it returns, exactly as before. The provider
is only told the part it can enforce. This is the same division Anthropic's own SDKs make
(strip what the API cannot compile, validate the original client-side).

Two translations are semantic, not just deletions:

* ``oneOf`` → ``anyOf``. The branches of a pydantic discriminated union are disjoint by their
  ``const`` tag, so "exactly one" and "at least one" accept the same documents.
* the ``discriminator.propertyName`` becomes ``required`` in every branch. Pydantic refuses a
  union member whose tag is missing (``union_tag_not_found``) even when the tag field has a
  default — so a schema that let the model omit it would invite a reply the validator rejects.

``schema_violations`` is the executable form of the subset: the contract test
(``tests/shared/test_def083_portable_schema.py``) runs it over the schema of EVERY structured
call site, found by scan, so a new model with a new keyword fails there — not at a provider.
"""

from typing import Any

from pydantic import BaseModel

#: String formats every route accepts (Anthropic's list); any other ``format`` is dropped.
SUPPORTED_FORMATS = frozenset({
    "date-time", "time", "date", "duration", "email", "hostname", "uri", "ipv4", "ipv6",
    "uuid",
})

#: Keywords copied through unchanged (their values are scalars or primitive lists).
_COPIED = frozenset({"type", "description", "enum", "const", "required"})

#: Every keyword the portable schema may contain; anything else is a violation. Spelled out
#: on its own, NOT derived from ``_COPIED``: the checker is the transform's judge, and a
#: judge built from the defendant's constant is blinded by the very edit it must catch
#: (mutation-tested: adding ``pattern`` to ``_COPIED`` passed a derived checker).
_ALLOWED = frozenset({
    "type", "description", "enum", "const", "required", "properties", "items", "anyOf",
    "allOf", "format", "additionalProperties", "minItems",
})

_PRIMITIVE = (str, int, float, bool, type(None))


def portable_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """*schema*'s JSON Schema reduced to the subset every provider route compiles.

    Raises ``ValueError`` for a recursive model — no route accepts one, so a recursive
    structured schema is a programming error, not a degradable request.
    """
    raw = schema.model_json_schema()
    defs: dict[str, Any] = raw.get("$defs", {})
    return _portable(raw, defs, ())


def _deref(node: dict[str, Any], defs: dict[str, Any], stack: tuple[str, ...]) -> tuple[
    dict[str, Any], tuple[str, ...]
]:
    """Inline one ``$ref`` (its siblings override the target's keys, as pydantic intends)."""
    ref = node["$ref"]
    name = ref.removeprefix("#/$defs/")
    if name == ref or name not in defs:
        raise ValueError(f"unresolvable schema reference: {ref}")
    if name in stack:
        raise ValueError(f"recursive schema: {' → '.join((*stack, name))}")
    merged = {**defs[name], **{k: v for k, v in node.items() if k != "$ref"}}
    return merged, (*stack, name)


def _portable(node: dict[str, Any], defs: dict[str, Any], stack: tuple[str, ...]) -> dict[
    str, Any
]:
    while "$ref" in node:
        node, stack = _deref(node, defs, stack)
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _COPIED:
            out[key] = list(value) if isinstance(value, list) else value
        elif key == "properties":
            out[key] = {name: _portable(sub, defs, stack) for name, sub in value.items()}
        elif key == "items" and isinstance(value, dict):
            out[key] = _portable(value, defs, stack)
        elif key in ("anyOf", "allOf", "oneOf"):
            target = "anyOf" if key == "oneOf" else key
            if target in out:
                raise ValueError(f"schema has both oneOf and anyOf at one node: {sorted(node)}")
            out[target] = [_portable(sub, defs, stack) for sub in value]
        elif key == "format" and value in SUPPORTED_FORMATS:
            out[key] = value
        elif key == "minItems" and value in (0, 1):
            out[key] = value
        elif key == "additionalProperties" and isinstance(value, dict):
            # A ``dict[str, X]`` field: its keys are free, and the subset forces ``false``.
            # Forcing it would make the provider emit ``{}`` for the whole field.
            raise ValueError(f"a mapping field cannot be sent as a structured schema: {value}")
        # Everything else is dropped: pattern, minimum/maximum/exclusive*, min/maxLength,
        # maxItems, title, default, discriminator, $defs, and any keyword pydantic adds later
        # (an allowlist, so a new keyword fails closed — it is dropped, never forwarded).
    discriminator = node.get("discriminator")
    tag = discriminator.get("propertyName") if isinstance(discriminator, dict) else None
    if tag:
        for branch in out.get("anyOf", []):
            required = branch.setdefault("required", [])
            if tag not in required:
                required.append(tag)
    if out.get("type") == "object":
        out["additionalProperties"] = False
    return out


def schema_violations(schema: Any, path: str = "#") -> list[str]:
    """Every construct in *schema* outside the portable subset, as ``"<path>: <why>"``.

    Empty means every route compiles it. Used by the contract test over every structured call
    site, and usable as an assertion anywhere a schema is about to be sent.
    """
    found: list[str] = []
    if not isinstance(schema, dict):
        return [f"{path}: not a schema object"]
    for key, value in schema.items():
        where = f"{path}/{key}"
        if key not in _ALLOWED:
            found.append(f"{where}: keyword not supported")
            # Still descend, so a pattern nested under $defs / oneOf is counted too.
            if key == "$defs":
                for name, sub in value.items():
                    found += schema_violations(sub, f"{where}/{name}")
            elif key == "oneOf":
                for i, sub in enumerate(value):
                    found += schema_violations(sub, f"{where}/{i}")
        elif key == "type" and not isinstance(value, str):
            found.append(f"{where}: type arrays are not supported")
        elif key == "enum" and not all(isinstance(v, _PRIMITIVE) for v in value):
            found.append(f"{where}: enum values must be primitives")
        elif key == "format" and value not in SUPPORTED_FORMATS:
            found.append(f"{where}: format {value!r} not supported")
        elif key == "minItems" and value not in (0, 1):
            found.append(f"{where}: minItems must be 0 or 1")
        elif key == "additionalProperties" and value is not False:
            found.append(f"{where}: must be false")
        elif key == "properties":
            for name, sub in value.items():
                found += schema_violations(sub, f"{where}/{name}")
        elif key == "items":
            found += schema_violations(value, where)
        elif key in ("anyOf", "allOf"):
            for i, sub in enumerate(value):
                found += schema_violations(sub, f"{where}/{i}")
    if schema.get("type") == "object" and schema.get("additionalProperties") is not False:
        found.append(f"{path}: object without additionalProperties false")
    return found
