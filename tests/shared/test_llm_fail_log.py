"""The LLM failure capture: the store's bounds, and the seam that feeds it (AI-D64).

Every test here was written to be RED before the change it guards. The two that matter
most are the classification test (`invalid_json` vs `schema_mismatch` were one outcome
before) and the budget test (clearing this log must never move remaining budget).
"""

import sqlite3
from collections.abc import Iterator
from decimal import Decimal

import pytest
from pydantic import BaseModel

from portfolio_dash.shared import llm as llm_mod
from portfolio_dash.shared import llm_fail_log as fl
from portfolio_dash.shared.llm import complete_structured, complete_text
from portfolio_dash.shared.llm_config import (
    LLMRole,
    LLMUnavailable,
    ModelConfig,
    add_topup,
    budget_remaining,
    ensure_llm_seeded,
    set_role,
    upsert_model,
)

# --- doubles ------------------------------------------------------------------------


class _Msg:
    def __init__(self, content: str) -> None:
        self.message = type("M", (), {"content": content})()


class _Usage:
    def __init__(self, pt: int, ct: int) -> None:
        self.prompt_tokens = pt
        self.completion_tokens = ct


class _Resp:
    def __init__(self, content: str, pt: int = 10, ct: int = 5) -> None:
        self.choices = [_Msg(content)]
        self.usage = _Usage(pt, ct)


class _EmptyEnvelope:
    """What a provider returns when it answers 200 with a body we cannot read."""

    def __init__(self) -> None:
        self.choices: list[object] = []
        self.usage = _Usage(1, 1)


class Out(BaseModel):
    x: int


def _model(model_id: str = "a", **kw: object) -> ModelConfig:
    base: dict[str, object] = dict(
        id=model_id, model_alias=model_id, provider="openai", model_name=model_id,
        input_price_per_mtok=Decimal("1"), output_price_per_mtok=Decimal("2"),
    )
    base.update(kw)
    return ModelConfig(**base)  # type: ignore[arg-type]


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_llm_seeded(c)
    fl.ensure_table(c)
    upsert_model(c, _model("a"))
    set_role(c, LLMRole.DEFAULT, "a")
    add_topup(c, Decimal("100"))
    yield c
    c.close()


# --- the store's bounds --------------------------------------------------------------


def test_ensure_table_is_idempotent(conn: sqlite3.Connection) -> None:
    fl.ensure_table(conn)
    fl.ensure_table(conn)
    assert fl.total_count(conn) == 0


def test_prune_keeps_the_newest_rows(conn: sqlite3.Connection) -> None:
    """The ring is bounded on INSERT, so the table cannot grow without limit."""
    for i in range(fl._KEEP + 20):
        fl.record(conn, agent="a", outcome="invalid_json", raw_output=f"r{i}")
    assert fl.total_count(conn) == fl._KEEP
    newest = fl.list_rows(conn, limit=1)[0]
    assert newest["raw_output"] == f"r{fl._KEEP + 19}"  # the newest survived, not the oldest


def test_long_text_is_capped_and_flagged(conn: sqlite3.Connection) -> None:
    fl.record(conn, agent="a", outcome="invalid_json", prompt="x" * (fl._MAX_TEXT + 500))
    row = fl.list_rows(conn)[0]
    assert len(str(row["prompt"])) == fl._MAX_TEXT
    assert row["truncated"] == 1


def test_short_text_is_not_flagged(conn: sqlite3.Connection) -> None:
    fl.record(conn, agent="a", outcome="invalid_json", prompt="short")
    assert fl.list_rows(conn)[0]["truncated"] == 0


def test_absent_table_reads_as_no_rows_never_raises() -> None:
    """A ledger-only database must degrade, not blow up (cross-layer obligation)."""
    bare = sqlite3.connect(":memory:")
    assert fl.total_count(bare) == 0
    assert fl.counts_by_agent(bare) == []
    assert fl.list_rows(bare) == []
    assert fl.delete_all(bare) == 0
    fl.record(bare, agent="a", outcome="invalid_json")  # must not raise
    bare.close()


def test_delete_all_can_be_scoped_to_one_agent(conn: sqlite3.Connection) -> None:
    fl.record(conn, agent="keep_me", outcome="invalid_json")
    fl.record(conn, agent="drop_me", outcome="invalid_json")
    assert fl.delete_all(conn, agent="drop_me") == 1
    assert [r["agent"] for r in fl.list_rows(conn)] == ["keep_me"]


def test_jsonl_is_one_complete_record_per_line(conn: sqlite3.Connection) -> None:
    import json

    fl.record(conn, agent="a", outcome="invalid_json", raw_output="第一列")
    fl.record(conn, agent="b", outcome="provider_error", error_reason="boom")
    text = fl.to_jsonl(fl.list_rows(conn))
    lines = text.splitlines()
    assert len(lines) == 2
    for line in lines:
        rec = json.loads(line)
        assert set(fl._COLUMNS) <= set(rec)  # every column present on every line
    assert "第一列" in text  # not ASCII-escaped


# --- the seam ------------------------------------------------------------------------


def test_provider_error_is_captured_with_its_prompt(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    def boom(**kw: object) -> object:
        raise RuntimeError("connection reset")

    monkeypatch.setattr(llm_mod.litellm, "completion", boom)
    with pytest.raises(LLMUnavailable):
        complete_structured("EXTRACT THIS", Out, agent="ai_agents_input", conn=conn)
    rows = fl.list_rows(conn)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "provider_error"
    assert rows[0]["agent"] == "ai_agents_input"
    assert "EXTRACT THIS" in str(rows[0]["prompt"])
    assert "connection reset" in str(rows[0]["error_reason"])


def test_invalid_json_and_schema_mismatch_are_DIFFERENT_outcomes(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    """The load-bearing one.

    Before this change both shapes raised one generic "invalid structured output" and
    were indistinguishable. They are different defects: garbage out vs the wrong fields.
    Note pydantic v2 reports BOTH as ValidationError, so the classifier reads the error's
    own ``type`` — an ``isinstance`` version would call everything a schema mismatch.
    """
    monkeypatch.setattr(llm_mod.litellm, "completion", lambda **kw: _Resp("hello world"))
    with pytest.raises(LLMUnavailable):
        complete_structured("p", Out, agent="t", conn=conn)
    assert {r["outcome"] for r in fl.list_rows(conn)} == {"invalid_json"}

    fl.delete_all(conn)
    monkeypatch.setattr(llm_mod.litellm, "completion", lambda **kw: _Resp('{"y": 1}'))
    with pytest.raises(LLMUnavailable):
        complete_structured("p", Out, agent="t", conn=conn)
    assert {r["outcome"] for r in fl.list_rows(conn)} == {"schema_mismatch"}


def test_both_billed_attempts_are_recorded(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    """One logical failure is TWO provider calls, so it must be two rows.

    A per-call row would hide half of what the user was billed for. Each row carries the
    ``llm_usage`` id of its own attempt, so the two tables reconcile.
    """
    monkeypatch.setattr(llm_mod.litellm, "completion", lambda **kw: _Resp("nope"))
    with pytest.raises(LLMUnavailable):
        complete_structured("p", Out, agent="t", conn=conn)
    rows = fl.list_rows(conn)
    assert [r["attempt"] for r in rows] == [2, 1]  # newest first
    usage_ids = {r["usage_id"] for r in rows}
    assert None not in usage_ids and len(usage_ids) == 2
    billed = conn.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0]
    assert billed == 2  # and the capture matches the billing


def test_malformed_envelope_degrades_instead_of_escaping_as_a_500(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    """`choices`/`usage` used to be read OUTSIDE the try.

    A provider that answers 200 with an unreadable body then raised a bare IndexError
    that never became LLMUnavailable — it reached the global catch-all as HTTP 500
    instead of the intended 503 degrade.
    """
    monkeypatch.setattr(llm_mod.litellm, "completion", lambda **kw: _EmptyEnvelope())
    with pytest.raises(LLMUnavailable, match="malformed response"):
        complete_structured("p", Out, agent="t", conn=conn)
    assert fl.list_rows(conn)[0]["outcome"] == "provider_error"


def test_malformed_envelope_degrades_on_the_text_path_too(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    monkeypatch.setattr(llm_mod.litellm, "completion", lambda **kw: _EmptyEnvelope())
    with pytest.raises(LLMUnavailable, match="malformed response"):
        complete_text("p", agent="t", conn=conn)


def test_image_payloads_never_reach_the_log(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    """A vision call carries megabytes of base64; only the text and a count are kept."""
    upsert_model(conn, _model("v", vision=True))
    set_role(conn, LLMRole.VISION, "v")
    monkeypatch.setattr(llm_mod.litellm, "completion", lambda **kw: _Resp("nope"))
    png = b"\x89PNG\r\n\x1a\n" + b"A" * 4000
    with pytest.raises(LLMUnavailable):
        complete_structured("look", Out, agent="t", conn=conn, images=[png, png])
    row = fl.list_rows(conn)[0]
    assert row["image_count"] == 2
    assert "base64" not in str(row["prompt"])
    assert "QUFB" not in str(row["prompt"])  # no encoded payload smuggled in
    assert "look" in str(row["prompt"])


def test_budget_refusal_is_captured(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    conn.execute("DELETE FROM llm_budget_events")
    conn.commit()
    with pytest.raises(Exception):  # noqa: B017 - LLMBudgetExceeded via the gate
        complete_structured("p", Out, agent="t", conn=conn)
    assert fl.list_rows(conn)[0]["outcome"] == "budget_exceeded"


# --- the red line --------------------------------------------------------------------


def test_clearing_the_fail_log_does_not_move_remaining_budget(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    """AI-D64's red line, pinned rather than commented.

    Remaining budget is `sum(topups) - sum(llm_usage.cost)`. If the clear button ever
    reached `llm_usage` it would hand the user free budget — a silent accounting error
    dressed up as housekeeping.
    """
    monkeypatch.setattr(llm_mod.litellm, "completion", lambda **kw: _Resp("nope"))
    with pytest.raises(LLMUnavailable):
        complete_structured("p", Out, agent="t", conn=conn)
    before = budget_remaining(conn)
    usage_before = conn.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0]

    assert fl.delete_all(conn) > 0

    assert budget_remaining(conn) == before
    assert conn.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0] == usage_before


# --- capture mode --------------------------------------------------------------------


def test_capture_mode_is_off_by_default() -> None:
    """Production records failures only; capture-all is an eval-harness switch."""
    assert fl.capture_all() is False


def test_capture_all_records_successful_attempts(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    """What the corpus runner needs: the raw reply for a case that parsed but was wrong."""
    monkeypatch.setattr(llm_mod.litellm, "completion", lambda **kw: _Resp('{"x": 7}'))
    complete_structured("p", Out, agent="t", conn=conn)
    assert fl.list_rows(conn) == []  # nothing captured while off

    fl.set_capture_mode(True)
    try:
        complete_structured("p", Out, agent="t", conn=conn)
    finally:
        fl.set_capture_mode(False)
    row = fl.list_rows(conn)[0]
    assert row["outcome"] == "ok"
    assert row["raw_output"] == '{"x": 7}'
    assert fl.capture_all() is False  # restored


# --- credentials never reach the table ------------------------------------------------


def test_a_provider_key_in_an_error_is_redacted(conn: sqlite3.Connection) -> None:
    """The seam calls the provider with `api_key=`; an auth error may echo it back.

    Without redaction that key would land in the table AND ride out through the one-click
    .jsonl download. Written after verifying a real key had NOT leaked -- the hole was
    reachable, not yet taken.
    """
    leaked = "sk-or-v1-9531cb0cAAAABBBBCCCCDDDD"
    fl.record(
        conn, agent="a", outcome="provider_error",
        error_reason=f"AuthenticationError(api_key='{leaked}')",
        prompt=f"Authorization: Bearer {leaked}",
        raw_output=f'{{"error":"bad key {leaked}"}}',
    )
    row = fl.list_rows(conn)[0]
    blob = " ".join(str(row[f]) for f in ("prompt", "raw_output", "error_reason"))
    assert leaked not in blob
    assert "9531cb0c" not in blob
    assert "[REDACTED]" in blob


def test_redaction_happens_before_truncation(conn: sqlite3.Connection) -> None:
    """A secret must not survive by sitting past the 16 KiB boundary."""
    leaked = "sk-proj-ZZZZYYYYXXXXWWWW"
    fl.record(conn, agent="a", outcome="provider_error",
              prompt="x" * (fl._MAX_TEXT - 5) + " " + leaked)
    assert leaked not in str(fl.list_rows(conn)[0]["prompt"])


def test_ordinary_text_is_left_alone(conn: sqlite3.Connection) -> None:
    """Redaction must not eat the diagnostic content it exists to protect."""
    fl.record(conn, agent="a", outcome="invalid_json",
              prompt="8/5 moomoo 馬股 1155 股息淨額入帳 88.50 馬幣",
              raw_output='{"rows":[{"kind":"div","symbol":"1155"}]}')
    row = fl.list_rows(conn)[0]
    assert "88.50" in str(row["prompt"])
    assert "1155" in str(row["raw_output"])
    assert "[REDACTED]" not in str(row["prompt"])
