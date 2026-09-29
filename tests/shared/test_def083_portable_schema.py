"""DEF-083: a structured call sends only the JSON Schema every provider route compiles.

Root cause (3a35454): ``shared/llm.py:175-187`` ``_response_format_for`` sent
``schema.model_json_schema()`` verbatim as ``response_format``, and ``:453-454`` sends it to
every model LiteLLM says supports one — including ``openrouter/anthropic/*``. Pydantic writes
that schema for its own validator: every ``Decimal`` gets a lookahead ``pattern``
(``llm_insight/cards.py:33`` ``target_pct``; ten fields of the AI door's drafts) and the AI
door's union is ``oneOf`` + ``discriminator``. Anthropic (direct, Azure, Google-hosted)
refused the pattern with HTTP 400, Bedrock refused ``oneOf`` — so haiku-4.5, the fallback of
all three roles, never saw a single structured request (G-09 run #221; AI door #520).

Why no test caught it: every LLM test replaces ``litellm.completion`` with a fake that
accepts any schema, and the one test about ``response_format``
(``tests/shared/test_llm.py::test_response_format_passed_when_supported``) asserted a
property NAME was present — never what a provider would refuse. The only validator of the
schema was the real provider, which no test and no fake endpoint reached (the verifier's R6
re-check of DEF-066 used a fake service that does not validate schemas either).

The guard: ``shared.llm_schema.schema_violations`` encodes the providers' documented limits,
and this file runs it over the schema of EVERY structured call site — found by AST scan, so
a new call site with a new model is checked without anyone remembering to list it.
"""

import ast
import importlib
import json
import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from litellm import exceptions as litellm_errors
from pydantic import BaseModel

import portfolio_dash
from portfolio_dash.data_ingestion.agents import AiDraftList, ai_agents_input
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import upsert_instrument
from portfolio_dash.data_ingestion.validate import CashPool
from portfolio_dash.shared import llm as llm_mod
from portfolio_dash.shared import llm_fail_log as fail_log
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.llm import _response_format_for, complete_structured_meta
from portfolio_dash.shared.llm_config import (
    LLMRole,
    ModelConfig,
    add_topup,
    ensure_llm_seeded,
    set_role,
    upsert_model,
)
from portfolio_dash.shared.llm_schema import portable_schema, schema_violations
from portfolio_dash.shared.models.assets import Instrument

_PKG = Path(portfolio_dash.__file__).resolve().parent

#: The completion seams a structured schema is handed to. ``completer`` is the AI door's
#: injected name for ``complete_structured_meta`` (``data_ingestion/agents.py``). Wrappers
#: that forward their own ``schema`` parameter (``master._master_structured``) are found by
#: the fixpoint below, not listed.
_SEAMS = {"complete_structured", "complete_structured_meta", "completer"}

#: What the scan must find today — pinned so a scanner that silently stops matching fails
#: here instead of passing over an empty set. A NEW structured call site fails this pin: add
#: its schema here (and it is already checked by the parametrised tests below).
_EXPECTED = {
    "InsightCard", "AiDraftList", "NewsExtract", "AiInstrumentResolveReply",
    "_NarrativeScore", "_Calibration", "_Review",
}


def _call_name(node: ast.Call) -> str | None:
    f = node.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None


def _schema_arg(node: ast.Call) -> ast.expr | None:
    for kw in node.keywords:
        if kw.arg == "schema":
            return kw.value
    return node.args[1] if len(node.args) > 1 else None


class _Scan(ast.NodeVisitor):
    def __init__(self, module: Any, seams: set[str]) -> None:
        self.module, self.seams = module, seams
        self.stack: list[ast.FunctionDef] = []
        self.sites: list[tuple[int, type[BaseModel]]] = []
        self.wrappers: set[str] = set()
        self.unresolved: list[str] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(node)
        self.generic_visit(node)
        self.stack.pop()

    def visit_Call(self, node: ast.Call) -> None:
        if _call_name(node) in self.seams:
            arg = _schema_arg(node)
            target = getattr(self.module, arg.id, None) if isinstance(arg, ast.Name) else None
            params = {a.arg for f in self.stack for a in (*f.args.args, *f.args.kwonlyargs)}
            if isinstance(target, type) and issubclass(target, BaseModel):
                self.sites.append((node.lineno, target))
            elif isinstance(arg, ast.Name) and arg.id in params and self.stack:
                self.wrappers.add(self.stack[-1].name)   # forwards its caller's schema
            else:
                self.unresolved.append(f"{self.module.__name__}:{node.lineno}")
        self.generic_visit(node)


def _structured_call_sites() -> dict[str, type[BaseModel]]:
    """``{"<file>:<line>": schema}`` for every structured call in the package."""
    seams = set(_SEAMS)
    while True:
        sites: dict[str, type[BaseModel]] = {}
        wrappers: set[str] = set()
        unresolved: list[str] = []
        for path in sorted(_PKG.rglob("*.py")):
            rel = path.relative_to(_PKG).as_posix()
            if rel == "shared/llm.py":   # the seam's own definitions
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            if not any(isinstance(n, ast.Call) and _call_name(n) in seams
                       for n in ast.walk(tree)):
                continue
            mod = importlib.import_module(
                "portfolio_dash." + rel.removesuffix(".py").replace("/", "."))
            scan = _Scan(mod, seams)
            scan.visit(tree)
            sites.update({f"{rel}:{line}": s for line, s in scan.sites})
            wrappers |= scan.wrappers
            unresolved += scan.unresolved
        if wrappers <= seams:
            assert not unresolved, f"structured calls whose schema cannot be resolved: {unresolved}"
            return sites
        seams |= wrappers


_SITES = _structured_call_sites()
_SCHEMAS = sorted({s for s in _SITES.values()}, key=lambda s: s.__name__)


def test_scan_finds_every_structured_call_site() -> None:
    assert {s.__name__ for s in _SITES.values()} == _EXPECTED, _SITES
    assert len(_SITES) >= 7, _SITES   # 7 call sites today: 1 each + master's 3


@pytest.mark.parametrize("schema", _SCHEMAS, ids=lambda s: s.__name__)
def test_every_structured_schema_is_sent_portable(schema: type[BaseModel]) -> None:
    sent = _response_format_for(schema)["json_schema"]
    assert isinstance(sent, dict)
    assert schema_violations(sent["schema"]) == []


def test_the_raw_schemas_held_what_the_providers_refused() -> None:
    """The checker is not vacuous: it finds the exact constructs of #517 / #520."""
    from portfolio_dash.llm_insight.cards import InsightCard

    card = schema_violations(InsightCard.model_json_schema())
    assert "#/$defs/Prediction/properties/target_pct/anyOf/1/pattern: keyword not supported" \
        in card
    drafts = schema_violations(AiDraftList.model_json_schema())
    assert "#/properties/rows/items/oneOf: keyword not supported" in drafts
    patterns = sum(1 for s in _SCHEMAS for v in schema_violations(s.model_json_schema())
                   if v.endswith("/pattern: keyword not supported"))
    assert patterns == 11   # 1 on the card's target_pct + 10 Decimal fields on the drafts
    # …and the lookahead itself is what Anthropic's regex compiler refused.
    assert "(?!" in json.dumps(InsightCard.model_json_schema())
    assert "(?!" not in json.dumps(_response_format_for(InsightCard))


def _objects(node: Any, path: str = "#") -> dict[str, tuple[set[str], set[str]]]:
    """``{path: (property names, required)}`` for every object in a dereferenced schema."""
    out: dict[str, tuple[set[str], set[str]]] = {}
    if isinstance(node, dict):
        if "properties" in node:
            out[path] = (set(node["properties"]), set(node.get("required", [])))
        for key, value in node.items():
            if key == "properties":
                for name, sub in value.items():
                    out |= _objects(sub, f"{path}/{name}")
            elif key in ("items",):
                out |= _objects(value, f"{path}/[]")
            elif key in ("anyOf", "oneOf", "allOf"):
                for i, sub in enumerate(value):
                    out |= _objects(sub, f"{path}/|{i}")
    return out


def _deref(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        if "$ref" in node:
            target = defs[node["$ref"].removeprefix("#/$defs/")]
            return _deref({**target, **{k: v for k, v in node.items() if k != "$ref"}}, defs)
        return {k: _deref(v, defs) for k, v in node.items() if k != "$defs"}
    if isinstance(node, list):
        return [_deref(v, defs) for v in node]
    return node


@pytest.mark.parametrize("schema", _SCHEMAS, ids=lambda s: s.__name__)
def test_portable_schema_keeps_every_field_and_requirement(schema: type[BaseModel]) -> None:
    """Only constraints are dropped: every object keeps every property and every
    requirement; the only requirement ADDED is a union's discriminator tag."""
    raw = schema.model_json_schema()
    before = _objects(_deref(raw, raw.get("$defs", {})))
    after = _objects(portable_schema(schema))
    assert before.keys() == after.keys()
    for path, (props, required) in before.items():
        assert after[path][0] == props, path
        assert required <= after[path][1], path
        assert after[path][1] - required <= {"kind"}, path


def test_union_branches_require_their_tag_and_are_any_of() -> None:
    rows = portable_schema(AiDraftList)["properties"]["rows"]["items"]
    assert "oneOf" not in rows and "discriminator" not in rows
    tags = []
    for branch in rows["anyOf"]:
        assert "kind" in branch["required"]
        tags.append(branch["properties"]["kind"]["const"])
    assert sorted(tags) == ["cash", "div", "txn"]


def test_a_schema_no_route_can_compile_fails_at_the_source() -> None:
    class Node(BaseModel):
        children: list["Node"] = []

    class Mapping(BaseModel):
        weights: dict[str, int]

    with pytest.raises(ValueError, match="recursive"):
        portable_schema(Node)
    with pytest.raises(ValueError, match="mapping field"):
        portable_schema(Mapping)


@pytest.mark.parametrize("schema, why", [
    ({"type": "string", "pattern": "^a$"}, "#/pattern: keyword not supported"),
    ({"oneOf": [{"type": "string"}]}, "#/oneOf: keyword not supported"),
    ({"type": "integer", "minimum": 0}, "#/minimum: keyword not supported"),
    ({"type": "array", "items": {"type": "string"}, "maxItems": 3},
     "#/maxItems: keyword not supported"),
    ({"type": "array", "items": {"type": "string"}, "minItems": 2},
     "#/minItems: minItems must be 0 or 1"),
    ({"type": ["string", "null"]}, "#/type: type arrays are not supported"),
    ({"type": "string", "format": "decimal"}, "#/format: format 'decimal' not supported"),
    ({"type": "object", "properties": {}}, "#: object without additionalProperties false"),
    ({"not": {"type": "null"}}, "#/not: keyword not supported"),
])
def test_the_checker_flags_each_refused_construct(schema: dict[str, Any], why: str) -> None:
    assert why in schema_violations(schema)


# --- the seam itself --------------------------------------------------------------------

class _Resp:
    def __init__(self, content: str) -> None:
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]
        self.usage = type("U", (), {"prompt_tokens": 10, "completion_tokens": 5})()


def _model(alias: str) -> ModelConfig:
    return ModelConfig(
        id=alias, model_alias=alias, provider="openrouter", model_name=f"vendor/{alias}",
        api_key="test-key-not-a-credential", max_retries=0,
        input_price_per_mtok=Decimal("1"), output_price_per_mtok=Decimal("2"),
    )


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    from portfolio_dash.bootstrap import bootstrap_db

    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    ensure_llm_seeded(c)
    fail_log.ensure_table(c)
    for alias in ("gemini-m", "haiku-m"):
        upsert_model(c, _model(alias))
    set_role(c, LLMRole.DEFAULT, "gemini-m")
    set_role(c, LLMRole.DEFAULT_FALLBACK, "haiku-m")
    add_topup(c, Decimal("10"))
    seed_accounts(c)
    upsert_instrument(c, Instrument(symbol="2884", market=Market.TW, quote_ccy=Currency.TWD,
                                    sector="Financials", name="玉山金"))
    yield c
    c.close()


_DRAFT = json.dumps({"rows": [{
    "kind": "txn", "account_id": "tw_broker", "symbol": "2884", "side": "BUY",
    "date": "2026-09-29", "shares": "100", "price": "46",
}], "unparsed": []})


def _provider(
    monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]], *,
    refuse_nonportable: frozenset[str] = frozenset(), fail: frozenset[str] = frozenset(),
) -> None:
    """A scripted provider. Models in *refuse_nonportable* behave like Anthropic behind
    OpenRouter: HTTP 400 for any schema holding a construct outside the subset (the old
    code's raw schema), a reply otherwise. Models in *fail* answer 400 whatever is sent."""
    monkeypatch.setattr(llm_mod.litellm, "supports_response_schema", lambda **kw: True)

    def completion(**kw: Any) -> _Resp:
        sent.append(kw)
        alias = str(kw["model"]).rsplit("/", 1)[-1]
        schema = kw["response_format"]["json_schema"]["schema"]
        if alias in fail or (alias in refuse_nonportable and schema_violations(schema)):
            raise litellm_errors.BadRequestError(
                message="Invalid regex in pattern field", model=alias, llm_provider="openrouter")
        return _Resp(_DRAFT)

    monkeypatch.setattr(llm_mod.litellm, "completion", completion)


def _pool(account_id: str, ccy: Currency, **kw: object) -> CashPool:
    return CashPool(balance=Decimal("999999999"), low=Decimal("999999999"))


def test_a_picked_model_that_compiles_the_schema_answers_itself(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#520 replayed: haiku picked by hand refuses any non-portable schema. It now answers."""
    sent: list[dict[str, Any]] = []
    _provider(monkeypatch, sent, refuse_nonportable=frozenset({"haiku-m"}))
    res = ai_agents_input(conn, "台灣券商 2026-09-29 買進 2884 100股 成交價 46", pool=_pool,
                          today=date(2026, 9, 29), model_alias="haiku-m")
    assert res.error is None
    assert [kw["model"] for kw in sent] == ["openrouter/vendor/haiku-m"]
    assert res.meta.model == "vendor/haiku-m" and res.meta.fallback_note is None
    assert fail_log.list_rows(conn) == []


def test_a_failover_is_named_on_the_ai_door(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a model DOES fail, the page is told which, why, and who answered instead."""
    sent: list[dict[str, Any]] = []
    _provider(monkeypatch, sent, fail=frozenset({"haiku-m"}))
    res = ai_agents_input(conn, "買進 2884", pool=_pool, today=date(2026, 9, 29),
                          model_alias="haiku-m")
    assert res.meta.model == "vendor/gemini-m"
    assert res.meta.fallback_note == (
        "指定模型 haiku-m 失敗：請求內容被拒（HTTP 400）。本次改由 gemini-m 產出。")

    # No pick, and the role primary answers: nothing to say.
    res = ai_agents_input(conn, "買進 2884", pool=_pool, today=date(2026, 9, 29))
    assert res.meta.fallback_note is None

    # No pick, and the role primary fails: named by its role.
    _provider(monkeypatch, sent, fail=frozenset({"gemini-m"}))
    res = ai_agents_input(conn, "買進 2884", pool=_pool, today=date(2026, 9, 29))
    assert res.meta.fallback_note == (
        "主模型 gemini-m 失敗：請求內容被拒（HTTP 400）。本次改由 haiku-m 產出。")


def test_the_prompt_still_carries_the_full_schema(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The division of labour: the provider gets the portable schema, the MODEL still reads
    every constraint in the prompt, and the reply is validated against the full model."""
    from portfolio_dash.llm_insight.cards import InsightCard

    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(llm_mod.litellm, "supports_response_schema", lambda **kw: True)

    def completion(**kw: Any) -> _Resp:
        sent.append(kw)
        return _Resp(json.dumps({"title": "t", "summary": "s", "body_md": "b"}))

    monkeypatch.setattr(llm_mod.litellm, "completion", completion)
    out = complete_structured_meta("p", InsightCard, agent="t", conn=conn)
    assert out.value.title == "t"
    assert schema_violations(sent[0]["response_format"]["json_schema"]["schema"]) == []
    prompt = sent[0]["messages"][0]["content"]
    assert json.dumps(InsightCard.model_json_schema(), ensure_ascii=False) in prompt
