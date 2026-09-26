"""DEF-066: the LLM seam's retry + fallback must actually work, and say why when they do not.

Root cause (3be67db): ``shared/llm.py`` passed ``num_retries=model.max_retries or 0`` to
``litellm.completion``. On ANY ``openai.APIError`` litellm's wrapper then routed the call to
``litellm.completion_with_retries``, which does ``import tenacity`` at run time — a package
this project never installed. The provider's real error was replaced by ``Exception("tenacity
import failed …")``, the retry never happened, and the fallback model failed the same way.

Why no test caught it: every LLM test replaced ``litellm.completion`` WHOLESALE, so litellm's
own exception branch — the only place the missing import lived — never executed in-process.
The two tests marked "REAL litellm" below drive the genuine ``litellm.completion`` wrapper
through its exception path with ``mock_response=<Exception>`` (no network: litellm raises
the instance before any provider call). On the old code they fail with the tenacity message.

The fix owns the retry in ``shared/llm.py`` (``num_retries=0`` to litellm, always): only a
TRANSIENT failure is retried (timeout, connection error, 408/425/429, 5xx), bounded by the
model's ``max_retries``; a 4xx (bad key, bad request) is never retried.
"""

import ast
import sqlite3
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import litellm
import pytest
from litellm import exceptions as litellm_errors
from pydantic import BaseModel

from portfolio_dash.shared import llm as llm_mod
from portfolio_dash.shared.llm import complete_structured, complete_text
from portfolio_dash.shared.llm_config import (
    LLMRole,
    LLMUnavailable,
    ModelConfig,
    add_topup,
    ensure_llm_seeded,
    set_role,
    upsert_model,
)

_REPO = Path(__file__).resolve().parents[2]


class Out(BaseModel):
    x: int


class _Msg:
    def __init__(self, content: str) -> None:
        self.message = type("M", (), {"content": content})()


class _Usage:
    prompt_tokens = 10
    completion_tokens = 5


class _Resp:
    def __init__(self, content: str) -> None:
        self.choices = [_Msg(content)]
        self.usage = _Usage()


def _model(alias: str, **kw: object) -> ModelConfig:
    base: dict[str, object] = dict(
        id=alias, model_alias=alias, provider="openai", model_name=alias,
        api_key="test-key-not-a-credential",
        input_price_per_mtok=Decimal("1"), output_price_per_mtok=Decimal("2"),
        max_retries=2,
    )
    base.update(kw)
    return ModelConfig(**base)  # type: ignore[arg-type]


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_llm_seeded(c)
    upsert_model(c, _model("primary-m"))
    upsert_model(c, _model("backup-m"))
    set_role(c, LLMRole.DEFAULT, "primary-m")
    set_role(c, LLMRole.DEFAULT_FALLBACK, "backup-m")
    add_topup(c, Decimal("100"))
    yield c
    c.close()


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Zero the backoff and record it. ``raising=False``: on the pre-fix code the attribute
    does not exist, and the test must then fail on the behaviour, not on the fixture."""
    record: list[float] = []
    monkeypatch.setattr(llm_mod, "_sleep", record.append, raising=False)
    return record


def _transient(model: str = "primary-m") -> Exception:
    return litellm_errors.InternalServerError(
        message="upstream overloaded", llm_provider="openai", model=model
    )


def _auth(model: str = "primary-m") -> Exception:
    return litellm_errors.AuthenticationError(
        message="invalid api key", llm_provider="openai", model=model
    )


def _real_litellm_with(
    script: dict[str, list[object]], calls: list[str]
) -> Callable[..., Any]:
    """Wrap the REAL ``litellm.completion``: inject the next scripted ``mock_response``.

    ``script`` maps the litellm model string to a queue; an Exception instance makes the
    real wrapper raise it (and run its own exception branch), a str is the reply content.
    """
    real = litellm.completion

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        model = str(kwargs["model"])
        calls.append(model)
        queue = script[model]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        return real(*args, mock_response=item, **kwargs)

    return wrapper


# --- the guard for the class: litellm's OWN error path, run in-process ------------------


def test_real_litellm_transient_failure_is_retried_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection, sleeps: list[float]
) -> None:
    """REAL litellm: a 500 then a good reply → one retry, the reply is used.

    Primary only (no fallback), so the pre-fix failure surfaces as itself — the tenacity
    message — rather than as a fallback that happens to mask it."""
    set_role(conn, LLMRole.DEFAULT_FALLBACK, None)
    calls: list[str] = []
    monkeypatch.setattr(llm_mod.litellm, "completion", _real_litellm_with(
        {"openai/primary-m": [_transient(), '{"x": 7}']}, calls))
    out = complete_structured("hi", Out, agent="test", conn=conn)
    assert out.x == 7
    assert calls == ["openai/primary-m", "openai/primary-m"]
    assert litellm.num_retries is None  # nothing re-armed litellm's own retry globally


def test_real_litellm_permanent_primary_failure_falls_back(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection, sleeps: list[float]
) -> None:
    """REAL litellm: a 401 on the primary is NOT retried; the fallback model answers."""
    calls: list[str] = []
    monkeypatch.setattr(llm_mod.litellm, "completion", _real_litellm_with(
        {"openai/primary-m": [_auth()], "openai/backup-m": ['{"x": 3}']}, calls))
    out = complete_structured("hi", Out, agent="test", conn=conn)
    assert out.x == 3
    assert calls == ["openai/primary-m", "openai/backup-m"]
    assert sleeps == []


# --- unit behaviour of the owned retry ------------------------------------------------


def test_transient_error_retried_with_backoff_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection, sleeps: list[float]
) -> None:
    calls: list[dict[str, Any]] = []

    def completion(**kw: Any) -> _Resp:
        calls.append(kw)
        if len(calls) == 1:
            raise litellm_errors.RateLimitError(
                message="slow down", llm_provider="openai", model="primary-m")
        return _Resp('{"x": 1}')

    monkeypatch.setattr(llm_mod.litellm, "completion", completion)
    assert complete_structured("hi", Out, agent="test", conn=conn).x == 1
    assert len(calls) == 2
    assert all(c["num_retries"] == 0 for c in calls)  # litellm's own retry stays off
    assert sleeps == [0.5]


@pytest.mark.parametrize("make", [
    lambda: litellm_errors.Timeout(message="t", model="primary-m", llm_provider="openai"),
    lambda: litellm_errors.APIConnectionError(
        message="refused", llm_provider="openai", model="primary-m"),
    lambda: litellm_errors.ServiceUnavailableError(
        message="503", llm_provider="openai", model="primary-m"),
], ids=["timeout", "connection", "503"])
def test_every_transient_class_is_retried(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection, sleeps: list[float],
    make: Callable[[], Exception],
) -> None:
    calls: list[str] = []

    def completion(**kw: Any) -> _Resp:
        calls.append(str(kw["model"]))
        if len(calls) == 1:
            raise make()
        return _Resp('{"x": 2}')

    monkeypatch.setattr(llm_mod.litellm, "completion", completion)
    assert complete_structured("hi", Out, agent="test", conn=conn).x == 2
    assert calls == ["openai/primary-m", "openai/primary-m"]  # retried, not failed over


@pytest.mark.parametrize("make", [
    lambda: litellm_errors.AuthenticationError(
        message="bad key", llm_provider="openai", model="m"),
    lambda: litellm_errors.BadRequestError(message="bad", model="m", llm_provider="openai"),
    lambda: litellm_errors.NotFoundError(message="no model", model="m", llm_provider="openai"),
    lambda: RuntimeError("config error"),
], ids=["401", "400", "404", "non-provider"])
def test_permanent_errors_are_not_retried(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection, sleeps: list[float],
    make: Callable[[], Exception],
) -> None:
    calls: list[str] = []

    def completion(**kw: Any) -> _Resp:
        calls.append(str(kw["model"]))
        raise make()

    monkeypatch.setattr(llm_mod.litellm, "completion", completion)
    with pytest.raises(LLMUnavailable):
        complete_structured("hi", Out, agent="test", conn=conn)
    assert calls == ["openai/primary-m", "openai/backup-m"]  # once each, then give up
    assert sleeps == []


def test_retries_are_bounded_by_the_models_max_retries(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection, sleeps: list[float]
) -> None:
    upsert_model(conn, _model("primary-m", max_retries=None))  # None → no retry at all
    calls: list[str] = []

    def completion(**kw: Any) -> _Resp:
        calls.append(str(kw["model"]))
        raise _transient()

    monkeypatch.setattr(llm_mod.litellm, "completion", completion)
    with pytest.raises(LLMUnavailable):
        complete_structured("hi", Out, agent="test", conn=conn)
    assert calls.count("openai/primary-m") == 1
    assert calls.count("openai/backup-m") == 3  # max_retries=2 → 1 call + 2 retries
    assert sleeps == [0.5, 1.0]


def test_both_candidates_fail_the_message_names_each_in_chinese(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection, sleeps: list[float]
) -> None:
    """The DEF-066 evidence shape: primary returns truncated JSON twice, backup errors."""

    def completion(**kw: Any) -> _Resp:
        if kw["model"] == "openai/primary-m":
            return _Resp('{"x": ')
        raise _transient("backup-m")

    monkeypatch.setattr(llm_mod.litellm, "completion", completion)
    with pytest.raises(LLMUnavailable) as info:
        complete_structured("hi", Out, agent="test", conn=conn)
    assert str(info.value) == (
        "主模型 primary-m：回應不是完整 JSON（2 次）；"
        "備援 backup-m：供應商服務異常（HTTP 500，已重試 2 次）"
    )
    assert info.value.kind == "llm_unavailable"
    assert "tenacity" not in str(info.value)


def test_a_bad_key_is_named_without_the_provider_text(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection, sleeps: list[float]
) -> None:
    """The owner-facing sentence names the failure class; the provider's raw text (which may
    echo a key) goes to the redacting fail log, never into the sentence."""

    def completion(**kw: Any) -> _Resp:
        raise litellm_errors.AuthenticationError(
            message="Incorrect API key provided: sk-abcdefghijklmnop", llm_provider="openai",
            model="m")

    monkeypatch.setattr(llm_mod.litellm, "completion", completion)
    with pytest.raises(LLMUnavailable) as info:
        complete_structured("hi", Out, agent="test", conn=conn)
    msg = str(info.value)
    assert msg == (
        "主模型 primary-m：金鑰無效或未授權（HTTP 401）；"
        "備援 backup-m：金鑰無效或未授權（HTTP 401）"
    )
    assert "sk-" not in msg


def test_complete_text_retries_and_falls_back_too(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection, sleeps: list[float]
) -> None:
    calls: list[str] = []

    def completion(**kw: Any) -> _Resp:
        calls.append(str(kw["model"]))
        assert kw["num_retries"] == 0
        if kw["model"] == "openai/primary-m":
            raise _auth()
        if calls.count("openai/backup-m") == 1:
            raise _transient("backup-m")
        return _Resp("hello")

    monkeypatch.setattr(llm_mod.litellm, "completion", completion)
    out = complete_text("hi", agent="test", conn=conn)
    assert out.reply == "hello" and out.model == "backup-m"
    assert calls == ["openai/primary-m", "openai/backup-m", "openai/backup-m"]


# --- static guard: nothing can re-arm litellm's own retry -----------------------------


def _litellm_completion_calls() -> list[tuple[Path, ast.Call]]:
    found: list[tuple[Path, ast.Call]] = []
    for path in sorted((_REPO / "portfolio_dash").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"completion", "acompletion"}
                    and "litellm" in ast.unparse(node.func.value)):
                found.append((path, node))
    return found


def test_every_litellm_completion_call_disables_litellms_own_retry() -> None:
    calls = _litellm_completion_calls()
    assert len(calls) >= 2, calls  # the seam + the settings ping — a scan that finds none is blind
    bad = []
    for path, call in calls:
        kw = {k.arg: k.value for k in call.keywords if k.arg}
        value = kw.get("num_retries")
        if not (isinstance(value, ast.Constant) and value.value == 0):
            bad.append(f"{path.relative_to(_REPO)}:{call.lineno}")
    assert not bad, f"litellm.completion without num_retries=0 (DEF-066): {bad}"


def test_nothing_sets_litellms_global_retry_or_calls_its_retry_helper() -> None:
    """``num_retries=0`` is falsy: litellm reads ``0 or litellm.num_retries``, so a global
    set anywhere would silently re-enable the tenacity path. Nor may anything call it."""
    offenders = []
    for path in sorted((_REPO / "portfolio_dash").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign | ast.AugAssign | ast.AnnAssign):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Attribute) and t.attr in {
                            "num_retries", "num_retries_per_request"}:
                        offenders.append(f"{path.relative_to(_REPO)}:{node.lineno}")
            if isinstance(node, ast.Attribute) and node.attr in {
                    "completion_with_retries", "acompletion_with_retries"}:
                offenders.append(f"{path.relative_to(_REPO)}:{node.lineno}")
    assert not offenders, offenders
